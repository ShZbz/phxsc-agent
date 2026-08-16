"""MoA 核心协调层测试（共享防重登记表 + worker 池并行 + 结果协议 + 失败降级）。

fake client + monkeypatch build_client 驱动，不发真实网络请求。
覆盖：SharedSeenSet 线程安全 / _run_worker mini-loop 与结果协议 /
tool 结果 source_id 登记与 [SKIP] 标注 / 异常与超时失败降级 /
MoaRunner 并行同序返回与参数校验。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from phxsc.agent import moa
from phxsc.agent.moa import (
    MAX_WORKERS,
    MINI_LOOP_MAX_ROUNDS,
    MoaRunner,
    SharedSeenSet,
    _aggregate,
    _failed,
    _plan_decompose,
    _run_worker,
    run_moa,
)
from phxsc.agent.tools import ToolRegistry, tool

ARXIV_ID = "arXiv:2405.12345"
DOI_ID = "10.1038/s41578-023-00582-w"
CFG = {"name": "deepseek", "model": "deepseek-v4-flash"}


def make_message(content=None, tool_calls=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )


def make_response(message, prompt_tokens=10, completion_tokens=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeCompletions:
    """按预置响应序列逐个弹出；记录每次 create 的 kwargs。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class ExplodingCompletions:
    """create 直接抛异常（worker 失败降级用）。"""

    def create(self, **kwargs):
        raise RuntimeError("boom")


class SlowCompletions:
    """create 睡眠超过 runner timeout（超时降级用）。"""

    def __init__(self, delay):
        self.delay = delay

    def create(self, **kwargs):
        time.sleep(self.delay)
        return make_response(make_message(content="慢"))


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def fake_build_client(client, name="deepseek", model="deepseek-v4-flash"):
    """模拟 providers.build_client 的真实 3-tuple 返回。"""

    def build(provider_name, model_arg=None):
        return client, name, model_arg or model

    return build


def make_dispatcher(clients):
    """按 provider name 分发不同假 client（并行 worker 各用各的）。"""

    def build(provider_name, model_arg=None):
        return clients[provider_name], provider_name, model_arg or "deepseek-v4-flash"

    return build


@pytest.fixture
def registry():
    """真实 ToolRegistry 注册假 arxiv_search（investigate 模式，返回带 source_id 的文本）。"""
    reg = ToolRegistry()

    @tool(name="arxiv_search", description="搜索文献", mode="investigate")
    def arxiv_search(query: str) -> str:
        return f"搜索结果：{ARXIV_ID} 《钙钛矿》 DOI {DOI_ID}"

    reg.register(arxiv_search)
    return reg


def tool_call_response(call_id="call_1"):
    return make_response(
        make_message(
            tool_calls=[
                make_tool_call(call_id, "arxiv_search", json.dumps({"query": "钙钛矿"}))
            ]
        )
    )


class TestSharedSeenSet:
    def test_register_first_true_duplicate_false(self):
        seen = SharedSeenSet()
        assert seen.register("arXiv:2405.12345") is True
        assert seen.register("arXiv:2405.12345") is False

    def test_contains_and_snapshot(self):
        seen = SharedSeenSet()
        seen.register("a")
        seen.register("b")
        assert seen.contains("a") is True
        assert seen.contains("missing") is False
        assert sorted(seen.snapshot()) == ["a", "b"]

    def test_concurrent_registration_unique(self):
        seen = SharedSeenSet()

        def worker(tid):
            for j in range(100):
                seen.register(f"t{tid}-{j}")

        with ThreadPoolExecutor(max_workers=8) as ex:
            for tid in range(8):
                ex.submit(worker, tid)
        assert len(seen.snapshot()) == 800


class TestRunWorker:
    def test_ok_protocol_and_registration(self, registry, monkeypatch):
        seen = SharedSeenSet()
        client = FakeClient(
            [
                tool_call_response(),
                make_response(make_message(content="结论：钙钛矿可行"), 10, 5),
            ]
        )
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))

        result = _run_worker(CFG, "搜索钙钛矿并总结", registry, seen, timeout=10.0)

        assert result["status"] == "ok"
        assert result["provider"] == "deepseek"
        assert result["answer"] == "结论：钙钛矿可行"
        assert ARXIV_ID in result["sources"]
        assert DOI_ID in result["sources"]
        assert result["prompt_tokens"] == 20
        assert result["completion_tokens"] == 10
        assert result["error"] == ""
        assert seen.contains(ARXIV_ID)
        assert seen.contains(DOI_ID)
        completions = client.chat.completions
        assert len(completions.calls) == 2
        first = completions.calls[0]
        assert first["max_tokens"] == 2000
        tool_names = [t["function"]["name"] for t in first["tools"]]
        assert "arxiv_search" in tool_names
        tool_msg = completions.calls[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert ARXIV_ID in tool_msg["content"]

    def test_skip_annotation_for_duplicate_ids(self, registry, monkeypatch):
        seen = SharedSeenSet()
        seen.register(ARXIV_ID)
        client = FakeClient(
            [
                tool_call_response(),
                make_response(make_message(content="完成")),
            ]
        )
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))

        result = _run_worker(CFG, "搜索钙钛矿", registry, seen, timeout=10.0)

        assert result["status"] == "ok"
        assert result["sources"] == [DOI_ID]
        tool_msg = client.chat.completions.calls[1]["messages"][-1]
        assert f"{ARXIV_ID} [SKIP]" in tool_msg["content"]
        assert "（注意：1 条文献已被其他 worker 收录，跳过）" in tool_msg["content"]
        assert seen.contains(DOI_ID)

    def test_no_tool_calls_single_round(self, registry, monkeypatch):
        seen = SharedSeenSet()
        client = FakeClient([make_response(make_message(content="直接回答"))])
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))

        result = _run_worker(CFG, "简单问题", registry, seen, timeout=10.0)

        assert result["status"] == "ok"
        assert result["answer"] == "直接回答"
        assert result["sources"] == []
        assert len(client.chat.completions.calls) == 1

    def test_mini_loop_stops_after_3_rounds(self, registry, monkeypatch):
        seen = SharedSeenSet()
        responses = [tool_call_response(f"call_{i}") for i in range(4)]
        responses[2] = make_response(
            make_message(content="部分结论", tool_calls=[make_tool_call("call_2", "arxiv_search", "{}")])
        )
        client = FakeClient(responses)
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))

        result = _run_worker(CFG, "反复搜", registry, seen, timeout=10.0)

        assert result["status"] == "ok"
        assert result["answer"] == "部分结论"
        assert len(client.chat.completions.calls) == MINI_LOOP_MAX_ROUNDS

    def test_non_str_tool_result_handled(self, registry, monkeypatch):
        """工具返回 list[dict]（真实 arxiv_search 形状）→ json 文本后仍能识别登记。"""
        seen = SharedSeenSet()

        @tool(name="arxiv_search_list", description="搜索文献", mode="investigate")
        def arxiv_search_list(query: str) -> list:
            return [{"arxiv_id": "2405.12345", "title": "钙钛矿"}]

        registry.register(arxiv_search_list)
        client = FakeClient(
            [
                make_response(
                    make_message(
                        tool_calls=[
                            make_tool_call(
                                "call_1", "arxiv_search_list", json.dumps({"query": "钙钛矿"})
                            )
                        ]
                    )
                ),
                make_response(make_message(content="完成")),
            ]
        )
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))

        result = _run_worker(CFG, "搜列表", registry, seen, timeout=10.0)

        assert result["status"] == "ok"
        assert "2405.12345" in result["sources"]
        assert seen.contains("2405.12345")

    def test_failed_protocol_on_client_exception(self, registry, monkeypatch):
        seen = SharedSeenSet()
        client = SimpleNamespace(chat=SimpleNamespace(completions=ExplodingCompletions()))
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))

        result = _run_worker(CFG, "会炸的任务", registry, seen, timeout=10.0)

        assert result["status"] == "failed"
        assert result["answer"] == ""
        assert result["sources"] == []
        assert result["error"] == "boom"

    def test_failed_protocol_on_build_failure(self, registry, monkeypatch):
        def build(*args, **kwargs):
            raise RuntimeError("key 缺失")

        monkeypatch.setattr(moa, "build_client", build)

        result = _run_worker(CFG, "任意任务", registry, SharedSeenSet(), timeout=10.0)

        assert result["status"] == "failed"
        assert result["provider"] == "deepseek"
        assert result["answer"] == ""
        assert result["error"] == "provider 构建失败: key 缺失"

    def test_plain_text_mode_single_round_no_tools(self, registry, monkeypatch):
        """tools_enabled=False：单轮纯文本调用，create 不带 tools，无 seen 登记。"""
        seen = SharedSeenSet()
        client = FakeClient(
            [make_response(make_message(content="第一章正文内容"), 10, 5)]
        )
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))

        result = _run_worker(
            CFG, "撰写第一章", registry, seen, timeout=10.0, tools_enabled=False
        )

        assert result["status"] == "ok"
        assert result["provider"] == "deepseek"
        assert result["answer"] == "第一章正文内容"
        assert result["sources"] == []
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 5
        assert result["error"] == ""
        assert seen.snapshot() == []
        completions = client.chat.completions
        assert len(completions.calls) == 1
        kwargs = completions.calls[0]
        assert "tools" not in kwargs
        assert kwargs["max_tokens"] == 2000

    def test_plain_text_mode_default_true_unchanged(self, registry, monkeypatch):
        """tools_enabled 默认 True：行为与 batch69 一致（带 tools 的 mini-loop）。"""
        seen = SharedSeenSet()
        client = FakeClient([make_response(make_message(content="直接回答"))])
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))

        result = _run_worker(CFG, "简单问题", registry, seen, timeout=10.0)

        assert result["status"] == "ok"
        kwargs = client.chat.completions.calls[0]
        assert "tools" in kwargs
        assert "arxiv_search" in [t["function"]["name"] for t in kwargs["tools"]]


class TestMoaRunner:
    def _two_step_client(self, final_answer, tool_name="arxiv_search", tool_args=None):
        return FakeClient(
            [
                make_response(
                    make_message(
                        tool_calls=[
                            make_tool_call(
                                "call_1",
                                tool_name,
                                json.dumps(tool_args or {"query": "钙钛矿"}),
                            )
                        ]
                    )
                ),
                make_response(make_message(content=final_answer)),
            ]
        )

    def test_run_parallel_results_in_order(self, registry, monkeypatch):
        clients = {
            "p1": self._two_step_client("答案一"),
            "p2": self._two_step_client("答案二"),
            "p3": SimpleNamespace(chat=SimpleNamespace(completions=ExplodingCompletions())),
        }
        monkeypatch.setattr(moa, "build_client", make_dispatcher(clients))
        runner = MoaRunner(registry)
        cfgs = [
            {"name": "p1", "model": "m1"},
            {"name": "p2", "model": "m2"},
            {"name": "p3", "model": "m3"},
        ]

        results = runner.run(cfgs, ["任务一", "任务二", "任务三"])

        assert [r["provider"] for r in results] == ["p1", "p2", "p3"]
        assert results[0]["status"] == "ok" and results[0]["answer"] == "答案一"
        assert results[1]["status"] == "ok" and results[1]["answer"] == "答案二"
        assert results[2]["status"] == "failed"
        assert "boom" in results[2]["error"]

    def test_run_shared_seen_dedup_across_workers(self, registry, monkeypatch):
        """两个 worker 检索到同一 arXiv id：只登记一次，后到者标注 [SKIP]。"""
        clients = {
            "p1": self._two_step_client("一"),
            "p2": self._two_step_client("二"),
        }
        monkeypatch.setattr(moa, "build_client", make_dispatcher(clients))
        seen = SharedSeenSet()
        runner = MoaRunner(registry)

        results = runner.run(
            [
                {"name": "p1", "model": "m1"},
                {"name": "p2", "model": "m2"},
            ],
            ["任务一", "任务二"],
            seen=seen,
        )

        assert all(r["status"] == "ok" for r in results)
        assert seen.contains(ARXIV_ID)
        combined_sources = results[0]["sources"] + results[1]["sources"]
        assert combined_sources.count(ARXIV_ID) == 1
        skip_marks = []
        for client in clients.values():
            tool_msg = client.chat.completions.calls[1]["messages"][-1]
            skip_marks.append("[SKIP]" in tool_msg["content"])
        assert sum(skip_marks) == 1

    def test_run_timeout_produces_failed(self, registry, monkeypatch):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SlowCompletions(0.5)))
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))
        runner = MoaRunner(registry, timeout=0.1, max_workers=1)

        results = runner.run([CFG], ["慢任务"])

        assert results[0]["status"] == "failed"
        assert "超时" in results[0]["error"]
        assert results[0]["provider"] == "deepseek"

    def test_run_length_mismatch_raises(self, registry):
        runner = MoaRunner(registry)
        with pytest.raises(ValueError):
            runner.run([CFG, CFG], ["只有一个任务"])

    def test_run_too_many_workers_raises(self, registry):
        runner = MoaRunner(registry)
        with pytest.raises(ValueError):
            runner.run([CFG] * (MAX_WORKERS + 1), ["任务"] * (MAX_WORKERS + 1))

    def test_run_passes_tools_enabled_from_cfg(self, registry, monkeypatch):
        """cfg 带 tools_enabled=False → worker 纯文本模式（create 无 tools）。"""
        client = FakeClient([make_response(make_message(content="纯文本章节"))])
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))
        runner = MoaRunner(registry)

        results = runner.run([{**CFG, "tools_enabled": False}], ["撰写章节"])

        assert results[0]["status"] == "ok"
        assert results[0]["answer"] == "纯文本章节"
        assert "tools" not in client.chat.completions.calls[0]

    def test_run_defaults_tools_enabled_true(self, registry, monkeypatch):
        """cfg 不带 tools_enabled → 默认带 tools（batch69 行为不变）。"""
        client = FakeClient([make_response(make_message(content="带工具回答"))])
        monkeypatch.setattr(moa, "build_client", fake_build_client(client))
        runner = MoaRunner(registry)

        results = runner.run([CFG], ["任务"])

        assert results[0]["status"] == "ok"
        assert "tools" in client.chat.completions.calls[0]


def ok_result(provider, answer, sources=(), p=10, c=5):
    return {
        "status": "ok",
        "provider": provider,
        "answer": answer,
        "sources": list(sources),
        "prompt_tokens": p,
        "completion_tokens": c,
        "error": "",
    }


class TestPlanDecompose:
    def _master_client(self, content):
        return FakeClient([make_response(make_message(content=content))])

    def test_parse_json_array(self):
        client = self._master_client('["搜索钙钛矿最新进展", "检索稳定性的文献"]')

        subtasks = _plan_decompose(client, "master-model", "survey", "调研钙钛矿", 2)

        assert subtasks == ["搜索钙钛矿最新进展", "检索稳定性的文献"]
        kwargs = client.chat.completions.calls[0]
        assert kwargs["model"] == "master-model"
        assert kwargs["max_tokens"] == 800
        prompt = kwargs["messages"][0]["content"]
        assert "survey" in prompt
        assert "调研钙钛矿" in prompt
        assert "2 个互不重叠的子任务" in prompt

    def test_parse_json_array_with_code_fences(self):
        client = self._master_client('```json\n["子任务A", "子任务B"]\n```')

        subtasks = _plan_decompose(client, "m", "qa", "问题", 2)

        assert subtasks == ["子任务A", "子任务B"]

    def test_invalid_response_falls_back_to_n_duplicates(self):
        client = self._master_client("抱歉，无法拆解该任务。")

        subtasks = _plan_decompose(client, "m", "qa", "问题X", 3)

        assert subtasks == ["问题X（角度1）", "问题X（角度2）", "问题X（角度3）"]

    def test_truncate_to_n_workers(self):
        client = self._master_client('["一", "二", "三", "四", "五"]')

        subtasks = _plan_decompose(client, "m", "qa", "问题", 3)

        assert subtasks == ["一", "二", "三"]

    def test_short_list_padded_to_n_workers(self):
        client = self._master_client('["唯一任务"]')

        subtasks = _plan_decompose(client, "m", "qa", "问题X", 3)

        assert subtasks == ["唯一任务", "问题X（角度2）", "问题X（角度3）"]

    def test_generate_uses_gen_template_with_chapter_instructions(self):
        client = self._master_client(
            '["第一章：绪论，介绍背景", "第二章：方法，说明流程"]'
        )

        subtasks = _plan_decompose(client, "m", "generate", "做一份钙钛矿PPT", 2)

        assert subtasks == ["第一章：绪论，介绍背景", "第二章：方法，说明流程"]
        prompt = client.chat.completions.calls[0]["messages"][0]["content"]
        assert "generate" in prompt
        assert "章节" in prompt
        assert "2 个章节的撰写指令" in prompt


class TestAggregate:
    def test_prompt_contains_numbered_answers_and_failed_marked(self):
        results = [
            ok_result("deepseek", "答案A"),
            _failed("zhipu", "boom"),
        ]
        client = FakeClient([make_response(make_message(content="综合结论：钙钛矿前景良好"))])

        text = _aggregate(client, "master-model", "问题？", results)

        assert text == "综合结论：钙钛矿前景良好"
        kwargs = client.chat.completions.calls[0]
        assert kwargs["model"] == "master-model"
        assert kwargs["max_tokens"] == 1500
        prompt = kwargs["messages"][0]["content"]
        assert "问题？" in prompt
        assert "2 个助手的回答" in prompt
        assert "1. [deepseek] 答案A" in prompt
        assert "2. [zhipu] 未响应" in prompt


class TestAggregateGen:
    def test_prompt_keeps_chapter_answers_in_order(self):
        results = [
            ok_result("deepseek", "第一章内容"),
            ok_result("zhipu", "第二章内容"),
        ]
        client = FakeClient(
            [make_response(make_message(content="# 文档\n\n## 第一章\n第一章内容\n\n## 第二章\n第二章内容"))]
        )

        text = moa._aggregate_gen(client, "master-model", "做一份PPT", results)

        assert text.startswith("# 文档")
        kwargs = client.chat.completions.calls[0]
        assert kwargs["model"] == "master-model"
        assert kwargs["max_tokens"] == 1500
        prompt = kwargs["messages"][0]["content"]
        assert "做一份PPT" in prompt
        assert "2 个助手" in prompt
        assert "1. [deepseek] 第一章内容" in prompt
        assert "2. [zhipu] 第二章内容" in prompt


class FakeRunner:
    """替代 MoaRunner：捕获 run 参数并返回预置结果。"""

    def __init__(self, results):
        self.results = results
        self.run_kwargs = None

    def run(self, worker_cfgs, subtasks, seen=None):
        self.run_kwargs = {
            "worker_cfgs": worker_cfgs,
            "subtasks": subtasks,
            "seen": seen,
        }
        return self.results


class TestRunMoa:
    CFGS = [
        {"name": "deepseek", "model": "d1"},
        {"name": "zhipu", "model": "z1"},
        {"name": "deepseek", "model": "d2"},
    ]

    def _patch(self, monkeypatch, results):
        decompose_calls = {}
        aggregate_calls = {}
        fake_runner = FakeRunner(results)
        monkeypatch.setattr(
            moa, "_plan_decompose",
            lambda client, model, task_type, question, n: decompose_calls.update(
                question=question, task_type=task_type, n=n
            ) or [f"子{i}" for i in range(1, n + 1)],
        )
        monkeypatch.setattr(
            moa, "_aggregate",
            lambda client, model, question, results: aggregate_calls.update(
                question=question, results=results
            ) or "聚合文本",
        )
        monkeypatch.setattr(moa, "MoaRunner", lambda registry: fake_runner)
        return decompose_calls, aggregate_calls, fake_runner

    def test_full_chain_assembles_final_text(self, monkeypatch):
        results = [
            ok_result("deepseek", "A", ["arXiv:1"]),
            ok_result("zhipu", "B", ["arXiv:1", "arXiv:2"], p=20, c=10),
            _failed("deepseek", "超时", sources=["arXiv:3"], prompt_tokens=5),
        ]
        decompose_calls, aggregate_calls, fake_runner = self._patch(monkeypatch, results)

        text = run_moa(
            FakeClient([]), "master-model", object(), "survey", "调研钙钛矿", self.CFGS
        )

        assert "## MoA 综合结果（3 助手：deepseek、zhipu、deepseek）" in text
        assert "聚合文本" in text
        assert "各助手已登记文献源：arXiv:1、arXiv:2、arXiv:3" in text
        assert "用量：" in text
        assert "deepseek: prompt_tokens=15, completion_tokens=5" in text
        assert "zhipu: prompt_tokens=20, completion_tokens=10" in text
        assert decompose_calls == {"question": "调研钙钛矿", "task_type": "survey", "n": 3}
        assert aggregate_calls["question"] == "调研钙钛矿"
        assert aggregate_calls["results"] is results
        assert fake_runner.run_kwargs["worker_cfgs"] == self.CFGS
        assert fake_runner.run_kwargs["subtasks"] == ["子1", "子2", "子3"]
        assert fake_runner.run_kwargs["seen"] is not None

    def test_sources_truncated_when_over_10(self, monkeypatch):
        results = [
            ok_result("p1", "A", [f"arXiv:{i}" for i in range(1, 13)]),
        ]
        self._patch(monkeypatch, results)

        text = run_moa(FakeClient([]), "m", object(), "qa", "问题", [self.CFGS[0]])

        assert "arXiv:1" in text and "arXiv:10" in text
        assert "arXiv:11" not in text
        assert "共 12 条" in text

    def test_all_failed_returns_failure_text_without_aggregate(self, monkeypatch):
        failed = [_failed("a", "超时"), _failed("b", "boom")]
        self._patch(monkeypatch, failed)
        aggregate_guard = []
        monkeypatch.setattr(moa, "_aggregate", lambda *a, **k: aggregate_guard.append(1))

        text = run_moa(FakeClient([]), "m", object(), "qa", "问题", self.CFGS[:2])

        assert text.startswith("MoA 执行失败：所有助手未响应")
        assert "超时" in text and "boom" in text
        assert aggregate_guard == []

    def test_generate_chain_writes_markdown_with_preview(self, monkeypatch, tmp_path):
        """generate 全链路：拆解 → 纯文本并行（cfg 带 tools_enabled=False）→ 章节聚合 →
        workspace/notes/moa_*.md 落盘，返回路径+预览。"""
        document = "# 钙钛矿PPT\n\n## 第一章\n第一章正文内容\n\n## 第二章\n第二章正文内容"
        results = [
            ok_result("deepseek", "第一章正文内容"),
            ok_result("zhipu", "第二章正文内容"),
        ]
        fake_runner = FakeRunner(results)
        monkeypatch.setattr(
            moa, "_plan_decompose",
            lambda client, model, task_type, question, n: [
                "第一章撰写指令", "第二章撰写指令"
            ],
        )
        monkeypatch.setattr(moa, "MoaRunner", lambda registry: fake_runner)
        monkeypatch.setattr(
            moa, "_aggregate_gen",
            lambda client, model, question, results: document,
        )
        monkeypatch.setattr(moa, "_workdir", lambda: str(tmp_path))

        text = run_moa(
            FakeClient([]), "master-model", object(), "generate", "做一份钙钛矿PPT",
            self.CFGS[:2],
        )

        assert text.startswith("已生成：")
        assert "notes/moa_" in text
        assert text.endswith(document)
        assert fake_runner.run_kwargs["subtasks"] == ["第一章撰写指令", "第二章撰写指令"]
        for cfg in fake_runner.run_kwargs["worker_cfgs"]:
            assert cfg["tools_enabled"] is False
        files = list((tmp_path / "notes").glob("moa_*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert content == document
        assert "第一章正文内容" in content
        assert "第二章正文内容" in content

"""长任务 plan-then-execute 两阶段测试（PLAN.md §4.11）。

fake client 驱动，不发真实网络请求。覆盖：
触发启发式（显式词/多目标/长输入/禁用开关）/
两阶段流程（计划落 plans/、最终回答、进度写回）/
阶段1 工具集只读限制 / 阶段2 重建上下文首条消息含计划 /
进度追加不覆盖原计划 / 路径穿越拒绝 / 缓存 / 非触发走原单阶段。
"""

import json
import os
from types import SimpleNamespace

import pytest

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.longtask import (
    PLAN_PROMPT_TEMPLATE,
    PLANS_DIR,
    append_progress,
    is_long_task,
    longtask_enabled,
    save_plan,
)
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool


@pytest.fixture(autouse=True)
def _longtask_on(monkeypatch):
    monkeypatch.setenv("PHXSC_LONGTASK", "1")


# batch77：触发词短输入被简单任务豁免，两阶段流程测试统一用多目标长任务输入
LONGTASK_INPUT = "帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记"


def make_message(content=None, tool_calls=None, reasoning_content=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


def make_response(message, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeLLM:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class FakeCache:
    def __init__(self):
        self.data = {}
        self.get_calls = 0
        self.set_calls = 0

    def get(self, key):
        self.get_calls += 1
        return self.data.get(key)

    def set(self, key, value):
        self.set_calls += 1
        self.data[key] = value


def make_env(responses, tmp_path, mode="test"):
    """注册读工具（* 模式）与写工具（当前模式），cm.workdir 指向 tmp_path。"""
    executed = []

    @tool(name="notes_write", description="写笔记", mode="test")
    def notes_write(title: str, content: str) -> str:
        executed.append(("notes_write", title))
        return f"已写入 {title}"

    @tool(name="paper_download", description="下载论文", mode="test")
    def paper_download(url: str) -> str:
        executed.append(("paper_download", url))
        return "下载完成"

    @tool(name="arxiv_search", description="搜索文献", mode="*")
    def arxiv_search(q: str) -> str:
        executed.append(("arxiv_search", q))
        return "论文列表"

    reg = ToolRegistry()
    reg.register_all([notes_write, paper_download, arxiv_search])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    cm.workdir = str(tmp_path)
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode=mode,
    )
    return loop, llm, executed, cm


def _tool_names(call_kwargs):
    return [t["function"]["name"] for t in call_kwargs["tools"]]


class TestTriggerHeuristics:
    def test_enabled_by_default(self):
        assert longtask_enabled()

    def test_explicit_trigger_word_triggers(self):
        assert is_long_task(LONGTASK_INPUT)

    def test_organize_alone_does_not_trigger(self):
        assert not is_long_task("帮我整理笔记")

    def test_review_triggers(self):
        assert is_long_task("综述这个方向，梳理研究现状，总结技术路线")

    def test_multi_goal_triggers(self):
        assert is_long_task("搜索钙钛矿+总结+整理笔记+生成PPT")

    def test_long_input_triggers(self):
        assert is_long_task("钙钛矿稳定性" * 40)

    def test_single_short_goal_does_not_trigger(self):
        assert not is_long_task("帮我搜索钙钛矿文献")

    def test_empty_input_does_not_trigger(self):
        assert not is_long_task("")

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("PHXSC_LONGTASK", "0")
        assert not is_long_task("帮我规划钙钛矿调研")

    def test_filler_only_exempt(self):
        """触发词 + 虚词组合（无实质内容）→ 简单任务豁免。"""
        assert not is_long_task("帮我规划一下")

    def test_investigate_word_triggers(self):
        """扩展触发词「调研」命中且实质内容足够 → 触发两阶段。"""
        assert is_long_task("调研钙钛矿热降解机理")

    def test_research_word_triggers(self):
        """扩展触发词「研究」命中且实质内容足够 → 触发两阶段。"""
        assert is_long_task("研究 Mn3Ga 反铁磁材料")

    def test_short_substance_exempt(self):
        """触发词命中但去除后实质内容 <6 字符 → 简单任务豁免。"""
        assert not is_long_task("规划钙钛矿")

    def test_plan_prompt_requires_full_checklist(self):
        """防回归：清单 prompt 强制 3-10 条，不再允许「简短计划」降级。"""
        assert "3 到 10 条" in PLAN_PROMPT_TEMPLATE
        assert "简短计划" not in PLAN_PROMPT_TEMPLATE


class TestTwoPhaseFlow:
    def test_plan_file_written_and_final_answer_returned(self, tmp_path):
        plan_text = "1. 第一步搜文献\n2. 第二步总结\n3. 第三步整理笔记"
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content=plan_text), "stop"),
                make_response(make_message(content="最终答案"), "stop"),
            ],
            tmp_path,
        )
        result = loop.run(LONGTASK_INPUT)
        assert result.startswith("最终答案")
        assert f"执行进度已记录：{PLANS_DIR}/" in result
        files = list((tmp_path / PLANS_DIR).glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "执行计划" in content
        assert plan_text in content
        assert "执行进度" in content
        assert len(llm.chat.completions.calls) == 2

    def test_phase1_uses_full_schema_permission_enforced(self, tmp_path):
        """阶段1 工具 schema 全量，只读靠 plan 模式 can_call 在调用时强制。"""
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content="1. 检索文献\n2. 阅读资料\n3. 总结机理"), "stop"),
                make_response(make_message(content="完成"), "stop"),
            ],
            tmp_path,
        )
        loop.run(LONGTASK_INPUT)
        calls = llm.chat.completions.calls
        phase1 = _tool_names(calls[0])
        phase2 = _tool_names(calls[1])
        assert set(phase1) == {"arxiv_search", "notes_write", "paper_download"}
        assert set(phase2) == {"arxiv_search", "notes_write", "paper_download"}
        plan_loop = loop._build_plan_loop()
        assert plan_loop.registry.can_call("plan", "notes_write") is False
        assert plan_loop.registry.can_call("plan", "paper_download") is False
        assert plan_loop.registry.can_call("plan", "arxiv_search") is True

    def test_phase2_first_user_message_contains_plan_and_task(self, tmp_path):
        plan_text = "1. 搜索文献\n2. 总结机理\n3. 整理笔记"
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content=plan_text), "stop"),
                make_response(make_message(content="完成"), "stop"),
            ],
            tmp_path,
        )
        loop.run(LONGTASK_INPUT)
        first_user = llm.chat.completions.calls[1]["messages"][1]
        assert first_user["role"] == "user"
        assert "【执行计划】" in first_user["content"]
        assert plan_text in first_user["content"]
        assert "【原始任务】" in first_user["content"]

    def test_phase2_first_user_message_has_mode_prefix(self, tmp_path):
        plan_text = "1. 搜索文献\n2. 总结机理\n3. 整理笔记"
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content=plan_text), "stop"),
                make_response(make_message(content="完成"), "stop"),
            ],
            tmp_path,
            mode="investigate",
        )
        loop.run(LONGTASK_INPUT)
        first_user = llm.chat.completions.calls[1]["messages"][1]
        assert first_user["content"].startswith("[mode: investigate]\n")
        assert "【执行计划】" in first_user["content"]

    def test_phase2_runs_write_tools_and_writes_progress(self, tmp_path):
        plan_text = "1. 写笔记\n2. 搜文献\n3. 写笔记"
        nw = make_tool_call("call_1", "notes_write", '{"title": "a", "content": "b"}')
        ar = make_tool_call("call_2", "arxiv_search", '{"q": "钙钛矿"}')
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content=plan_text), "stop"),
                make_response(make_message(content=None, tool_calls=[nw]), "tool_calls"),
                make_response(make_message(content=None, tool_calls=[ar]), "tool_calls"),
                make_response(make_message(content=None, tool_calls=[nw]), "tool_calls"),
                make_response(make_message(content="最终答案"), "stop"),
            ],
            tmp_path,
        )
        result = loop.run(LONGTASK_INPUT)
        assert result.startswith("最终答案")
        assert ("notes_write", "a") in executed
        assert ("arxiv_search", "钙钛矿") in executed
        plan_file = list((tmp_path / PLANS_DIR).glob("*.md"))[0]
        content = plan_file.read_text(encoding="utf-8")
        assert "步骤 3" in content


class TestProgressAppend:
    def test_append_progress_keeps_original_plan(self, tmp_path):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "# 执行计划\n\n## 计划\n原始计划内容\n\n## 执行进度\n",
            encoding="utf-8",
        )
        append_progress(str(plan_path), "步骤 3：完成搜索")
        append_progress(str(plan_path), "步骤 6：完成总结")
        content = plan_path.read_text(encoding="utf-8")
        assert "原始计划内容" in content
        assert "步骤 3：完成搜索" in content
        assert "步骤 6：完成总结" in content


class TestPathSafety:
    def test_save_plan_rejects_traversal_title(self, tmp_path):
        with pytest.raises(ValueError):
            save_plan(str(tmp_path), "../../etc/passwd", "plan")

    def test_run_with_traversal_title_rejects_and_cleans_context(self, tmp_path):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="1. 一\n2. 二\n3. 三"), "stop")],
            tmp_path,
        )
        with pytest.raises(ValueError):
            loop.run("../../etc/规划钙钛矿调研，先检索文献，再总结机理")
        assert list((tmp_path / PLANS_DIR).glob("*.md")) == []
        assert [m["role"] for m in cm.build_messages()] == ["system"]


class FlakyCompletions:
    """第 2 次 create 起抛 ConnectionError（模拟阶段2 网络抖动）；首次走内层。"""

    def __init__(self, responses):
        self._inner = FakeCompletions(responses)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls >= 2:
            raise ConnectionError("模拟 DeepSeek 网络抖动")
        return self._inner.create(**kwargs)


class TestAmnesiaFix:
    def test_exception_in_stage2_keeps_history(self, tmp_path):
        """阶段2 LLM 异常 → 上下文回退到本轮前（历史保留，失忆修复）。"""
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="计划文本"), "stop")],
            tmp_path,
        )
        # 构造历史：一轮普通对话
        cm.append("user", "第一轮问题")
        cm.append("assistant", "第一轮回答")
        before = len(cm.build_messages())
        # 替换 completions：阶段1 消耗第 1 次调用（返回计划文本），阶段2 第 1 步抛异常
        loop.llm_client.chat.completions = FlakyCompletions(
            [make_response(make_message(content="计划文本"), "stop")]
        )
        with pytest.raises(ConnectionError):
            loop.run(LONGTASK_INPUT)
        # 历史保留（system + 第一轮两条），只丢本轮临时消息
        assert len(cm.build_messages()) == before
        assert [m["role"] for m in cm.build_messages()] == ["system", "user", "assistant"]


class TestCache:
    def test_longtask_result_cached_under_original_key(self, tmp_path):
        cache = FakeCache()
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content="1. 一\n2. 二\n3. 三"), "stop"),
                make_response(make_message(content="最终答案"), "stop"),
            ],
            tmp_path,
        )
        loop.cache = cache
        first = loop.run(LONGTASK_INPUT)
        assert "最终答案" in first
        assert cache.set_calls == 1
        second = loop.run(LONGTASK_INPUT)
        assert second == "最终答案"
        assert len(llm.chat.completions.calls) == 2
        assert loop.cache_hit is True


class TestNonTriggeringKeepsSinglePhase:
    def test_simple_input_uses_full_toolset_single_call(self, tmp_path):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="答案"), "stop")],
            tmp_path,
        )
        result = loop.run("帮我搜索钙钛矿文献")
        assert result == "答案"
        assert len(llm.chat.completions.calls) == 1
        tools = _tool_names(llm.chat.completions.calls[0])
        assert "notes_write" in tools
        assert "arxiv_search" in tools
        assert list((tmp_path / PLANS_DIR).glob("*.md")) == []

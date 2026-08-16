"""/moa CLI 接线与 settings.moa_workers 测试（全 monkeypatch，不发真实网络请求）。

覆盖：_parse_moa 各形态 / _handle_moa 接线（task_type 判定、worker_cfgs 转换、
ThinkingLLM 底层 client 透传、异常包装、空问题用法提示）/
_handle_moa_rich（worker + 输入轮询进度：非 tty 不阻塞、/stop 提示不可中断、
异常包装）/ settings.load_moa_workers（默认 / JSON 字符串 / 非法回退 / 元素跳过 / 截断）。
"""

import json
import time
from types import SimpleNamespace

from phxsc import cli, settings
from phxsc.cli import _handle_moa, _handle_moa_rich, _parse_moa

DEFAULT_WORKERS = [
    "deepseek:deepseek-v4-flash",
    "zhipu:glm-4.5-air",
    "deepseek:deepseek-v4-flash",
]


class TestParseMoa:
    def test_basic(self):
        assert _parse_moa("/moa 调研钙钛矿电池最新进展") == "调研钙钛矿电池最新进展"

    def test_bare_or_empty_returns_none(self):
        assert _parse_moa("/moa") is None
        assert _parse_moa("/moa ") is None
        assert _parse_moa("/moa   ") is None

    def test_non_moa_lines_return_none(self):
        assert _parse_moa("/gate 问题") is None
        assert _parse_moa("/mosa 问题") is None
        assert _parse_moa("普通问题") is None


class TestHandleMoa:
    def _patch(self, monkeypatch, workers=None):
        calls = {}

        def fake_run_moa(client, model, registry, task_type, question, worker_cfgs):
            calls.update(
                client=client, model=model, registry=registry,
                task_type=task_type, question=question, worker_cfgs=worker_cfgs,
            )
            return "## MoA 综合结果（2 助手）\n聚合文本"

        monkeypatch.setattr(cli, "run_moa", fake_run_moa)
        monkeypatch.setattr(
            cli, "load_moa_workers",
            lambda path=None: workers if workers is not None else list(DEFAULT_WORKERS),
        )
        return calls

    def test_survey_question_dispatches_with_inner_client(self, monkeypatch, capsys):
        loop = SimpleNamespace(model="cur-model", registry="REG")
        inner = SimpleNamespace()
        client = SimpleNamespace(_inner=inner)
        calls = self._patch(
            monkeypatch, workers=["deepseek:d1", "zhipu:z1"]
        )

        _handle_moa(loop, client, "/moa 检索钙钛矿最新进展")

        assert calls["client"] is inner
        assert calls["model"] == "cur-model"
        assert calls["registry"] == "REG"
        assert calls["task_type"] == "survey"
        assert calls["question"] == "检索钙钛矿最新进展"
        assert calls["worker_cfgs"] == [
            {"name": "deepseek", "model": "d1"},
            {"name": "zhipu", "model": "z1"},
        ]
        out = capsys.readouterr().out
        assert "MoA 启动：2 助手并行（task_type=survey）" in out
        assert "MoA 综合结果" in out

    def test_qa_question_task_type(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        _handle_moa(
            SimpleNamespace(model="m", registry=None),
            SimpleNamespace(_inner=SimpleNamespace()),
            "/moa 什么是钙钛矿",
        )
        assert calls["task_type"] == "qa"
        assert "task_type=qa" in capsys.readouterr().out

    def test_survey_keywords(self, monkeypatch):
        calls = self._patch(monkeypatch)
        for q in ("调研钙钛矿", "综述钙钛矿", "查钙钛矿", "帮我找文献", "钙钛矿文献综述"):
            _handle_moa(
                SimpleNamespace(model="m", registry=None),
                SimpleNamespace(_inner=SimpleNamespace()),
                f"/moa {q}",
            )
            assert calls["task_type"] == "survey", q

    def test_generate_keywords(self, monkeypatch):
        calls = self._patch(monkeypatch)
        for q in ("做一份钙钛矿PPT", "写个幻灯片", "生成一份文档", "写个报告", "按章节展开"):
            _handle_moa(
                SimpleNamespace(model="m", registry=None),
                SimpleNamespace(_inner=SimpleNamespace()),
                f"/moa {q}",
            )
            assert calls["task_type"] == "generate", q

    def test_survey_takes_precedence_over_generate(self, monkeypatch):
        calls = self._patch(monkeypatch)
        _handle_moa(
            SimpleNamespace(model="m", registry=None),
            SimpleNamespace(_inner=SimpleNamespace()),
            "/moa 检索钙钛矿的PPT",
        )
        assert calls["task_type"] == "survey"

    def test_empty_question_prints_usage_without_run(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch)
        _handle_moa(
            SimpleNamespace(model="m", registry=None),
            SimpleNamespace(_inner=SimpleNamespace()),
            "/moa",
        )
        assert calls == {}
        assert "用法" in capsys.readouterr().out

    def test_exception_wrapped_as_failure_text(self, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise RuntimeError("网络炸了")

        monkeypatch.setattr(cli, "run_moa", boom)
        monkeypatch.setattr(cli, "load_moa_workers", lambda path=None: ["deepseek:d1"])

        _handle_moa(
            SimpleNamespace(model="m", registry=None),
            SimpleNamespace(_inner=SimpleNamespace()),
            "/moa 问题",
        )

        out = capsys.readouterr().out
        assert "MoA 执行失败" in out
        assert "网络炸了" in out


class TestMoaRichProgress:
    """U4：Rich 路径 /moa worker + 输入轮询——阻塞期间不静默，不阻塞主线程。"""

    def _state(self):
        loop = SimpleNamespace(model="m", registry=None)
        return cli._UIState(loop, time.perf_counter())

    def test_nonblocking_prints_result(self, monkeypatch, capsys):
        def slow_run_moa(client, model, registry, task_type, question, worker_cfgs):
            time.sleep(0.2)
            return "## MoA 综合结果"

        monkeypatch.setattr(cli, "run_moa", slow_run_moa)
        monkeypatch.setattr(cli, "load_moa_workers", lambda path=None: ["deepseek:d1"])
        monkeypatch.setattr(
            cli, "_input_line",
            lambda session, prompt: (_ for _ in ()).throw(EOFError),
        )
        state = self._state()

        _handle_moa_rich(
            SimpleNamespace(model="m", registry=None),
            SimpleNamespace(_inner=SimpleNamespace()),
            "/moa 问题", state, None, "",
        )

        out = capsys.readouterr().out
        assert state.busy is False
        assert state.last_turn_seconds is not None
        assert state.last_turn_seconds >= 0.19
        assert "MoA 启动" in out
        assert "MoA 综合结果" in out

    def test_stop_input_prints_not_interruptible(self, monkeypatch, capsys):
        def slow_run_moa(client, model, registry, task_type, question, worker_cfgs):
            time.sleep(0.1)
            return "## 结果"

        monkeypatch.setattr(cli, "run_moa", slow_run_moa)
        monkeypatch.setattr(cli, "load_moa_workers", lambda path=None: ["deepseek:d1"])
        inputs = iter(["/stop", "hi"])
        monkeypatch.setattr(cli, "_input_line", lambda session, prompt: next(inputs, "hi"))
        state = self._state()

        _handle_moa_rich(
            SimpleNamespace(model="m", registry=None),
            SimpleNamespace(_inner=SimpleNamespace()),
            "/moa 问题", state, None, "",
        )

        out = capsys.readouterr().out
        assert "MoA 暂不支持中断，请等待完成" in out
        assert "MoA 处理中…" in out
        assert state.busy is False

    def test_exception_wrapped(self, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise RuntimeError("网络炸了")

        monkeypatch.setattr(cli, "run_moa", boom)
        monkeypatch.setattr(cli, "load_moa_workers", lambda path=None: ["deepseek:d1"])
        monkeypatch.setattr(
            cli, "_input_line",
            lambda session, prompt: (_ for _ in ()).throw(EOFError),
        )
        state = self._state()

        _handle_moa_rich(
            SimpleNamespace(model="m", registry=None),
            SimpleNamespace(_inner=SimpleNamespace()),
            "/moa 问题", state, None, "",
        )

        out = capsys.readouterr().out
        assert "MoA 执行失败" in out
        assert "网络炸了" in out
        assert state.busy is False


class TestLoadMoaWorkers:
    def _write(self, tmp_path, value):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"moa_workers": value}), encoding="utf-8")
        return str(p)

    def test_default_when_missing(self, tmp_path):
        assert settings.load_moa_workers(str(tmp_path / "nope.json")) == DEFAULT_WORKERS

    def test_json_string_form(self, tmp_path):
        p = self._write(tmp_path, '["zhipu:glm-4.5-air", "deepseek:d"]')
        assert settings.load_moa_workers(p) == ["zhipu:glm-4.5-air", "deepseek:d"]

    def test_list_form_accepted(self, tmp_path):
        p = self._write(tmp_path, ["a:m1"])
        assert settings.load_moa_workers(p) == ["a:m1"]

    def test_invalid_values_fall_back(self, tmp_path):
        for bad in ("not-json", 42, {"a": 1}, []):
            p = self._write(tmp_path, bad)
            assert settings.load_moa_workers(p) == DEFAULT_WORKERS, bad

    def test_invalid_elements_skipped(self, tmp_path):
        p = self._write(tmp_path, '["a:m1", "bad", 7, "c:m2", "", ":m3", "x:"]')
        assert settings.load_moa_workers(p) == ["a:m1", "c:m2"]

    def test_truncates_to_4(self, tmp_path):
        p = self._write(tmp_path, '["a:1", "b:2", "c:3", "d:4", "e:5", "f:6"]')
        assert settings.load_moa_workers(p) == ["a:1", "b:2", "c:3", "d:4"]

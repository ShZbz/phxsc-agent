"""telemetry 统计单元 + AgentLoop 集成测试。

用 FakeLLM / FakeCache 注入 AgentLoop，验证：
- record 追加写、行 JSON 可解析
- daily_summary 当日聚合 / 空文件 / 日期过滤
- 成本与 MODEL_PRICES 手动计算一致
- loop 每轮写 telemetry（含 cache_hit/miss tokens、exact cache 命中行）
- 无 telemetry 时行为不变
- 写失败静默降级
"""

import json
from types import SimpleNamespace

import pytest

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.telemetry import MODEL_PRICES, Telemetry

TODAY_ROW = {
    "ts": "2026-01-01T10:00:00+08:00",
    "model": "deepseek-v4-flash",
    "mode": "test",
    "step": 1,
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "prompt_cache_hit_tokens": 80,
    "prompt_cache_miss_tokens": 20,
    "cache_hit": False,
}


def make_message(content=None, tool_calls=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
    )


def make_response(message, finish_reason="stop", usage=None):
    if usage is None:
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
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

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


def make_env(responses, cache=None, telemetry=None):
    @tool(name="add", description="整数加法", mode="test")
    def add(a: int, b: int) -> int:
        return a + b

    reg = ToolRegistry()
    reg.register_all([add])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode="test",
        cache=cache,
        telemetry=telemetry,
    )
    return loop, llm


class TestRecord:
    def test_record_appends_parsable_lines(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record({"ts": "2026-01-01T00:00:00+00:00", "model": "m", "a": 1})
        tel.record({"ts": "2026-01-01T00:01:00+00:00", "model": "m", "b": 2})
        lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first, second = (json.loads(line) for line in lines)
        assert first["a"] == 1
        assert second["b"] == 2

    def test_record_creates_parent_dirs(self, tmp_path):
        tel = Telemetry(str(tmp_path / "a" / "b" / "t.jsonl"))
        tel.record({"ts": "x", "model": "m"})
        assert (tmp_path / "a" / "b" / "t.jsonl").exists()

    def test_env_path_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHXSC_TELEMETRY_PATH", str(tmp_path / "env.jsonl"))
        tel = Telemetry()
        assert tel.path == str(tmp_path / "env.jsonl")
        tel.record({"ts": "x", "model": "m"})
        assert (tmp_path / "env.jsonl").exists()


class TestDailySummary:
    def test_same_day_rows_aggregated(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record(TODAY_ROW)
        tel.record(
            {
                **TODAY_ROW,
                "ts": "2026-01-01T11:00:00+08:00",
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 50,
                "cache_hit": True,
            }
        )
        tel.record(
            {
                **TODAY_ROW,
                "ts": "2026-01-02T00:00:00+08:00",
                "cache_hit": False,
            }
        )
        s = tel.daily_summary("2026-01-01")
        assert s["calls"] == 1  # 命中轮（cache_hit=True，0 token）不计入 LLM 调用
        assert s["total_prompt_tokens"] == 150
        assert s["total_completion_tokens"] == 30
        assert s["total_tokens"] == 180
        assert s["cache_hits"] == 1
        assert s["cache_misses"] == 1
        assert s["cache_hit_rate"] == 0.5

    def test_empty_file_returns_zeros(self, tmp_path):
        tel = Telemetry(str(tmp_path / "empty.jsonl"))
        s = tel.daily_summary("2026-01-01")
        assert s["calls"] == 0
        assert s["total_prompt_tokens"] == 0
        assert s["total_completion_tokens"] == 0
        assert s["total_tokens"] == 0
        assert s["cache_hits"] == 0
        assert s["cache_misses"] == 0
        assert s["cache_hit_rate"] == 0.0
        assert s["prefix_cache_hit_rate"] == 0.0
        assert s["estimated_cost_usd"] == 0.0

    def test_cache_hit_entries_not_counted_as_calls(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record({**TODAY_ROW, "cache_hit": True})
        tel.record({**TODAY_ROW, "cache_hit": False})
        s = tel.daily_summary("2026-01-01")
        assert s["calls"] == 1  # 命中轮（0 token）不计入 LLM 调用

    def test_all_non_hit_entries_count_each_call(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        for _ in range(3):
            tel.record({**TODAY_ROW, "cache_hit": False})
        s = tel.daily_summary("2026-01-01")
        assert s["calls"] == 3  # 回归：全非命中 → calls == 行数

    def test_missing_file_returns_zeros(self, tmp_path):
        tel = Telemetry(str(tmp_path / "nope.jsonl"))
        s = tel.daily_summary("2026-01-01")
        assert s["calls"] == 0 and s["cache_hit_rate"] == 0.0
        assert s["prefix_cache_hit_rate"] == 0.0
        assert s["estimated_cost_usd"] == 0.0

    def test_cost_matches_manual_calculation(self, tmp_path):
        p = MODEL_PRICES["deepseek-v4-flash"]
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "model": "deepseek-v4-flash",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "prompt_cache_hit_tokens": 600,
                "prompt_cache_miss_tokens": 400,
                "cache_hit": False,
            }
        )
        expected = (
            600 / 1e6 * p["cache_hit_input"]
            + 400 / 1e6 * p["input"]
            + 500 / 1e6 * p["output"]
        )
        s = tel.daily_summary("2026-01-01")
        assert s["estimated_cost_usd"] == pytest.approx(expected, abs=1e-9)

    def test_prefix_cache_hit_rate_by_tokens(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record(
            {
                **TODAY_ROW,
                "prompt_tokens": 1000,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 200,
            }
        )
        s = tel.daily_summary("2026-01-01")
        assert s["prefix_cache_hit_tokens"] == 800
        assert s["prefix_cache_miss_tokens"] == 200
        assert s["prefix_cache_hit_rate"] == 0.8

    def test_prefix_cache_hit_rate_aggregates_rows(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record({**TODAY_ROW, "prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 0})
        tel.record({**TODAY_ROW, "prompt_cache_hit_tokens": 200, "prompt_cache_miss_tokens": 400})
        s = tel.daily_summary("2026-01-01")
        assert s["prefix_cache_hit_tokens"] == 800
        assert s["prefix_cache_miss_tokens"] == 400
        assert s["prefix_cache_hit_rate"] == pytest.approx(2 / 3)

    def test_prefix_cache_hit_rate_zero_when_no_tokens(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record({**TODAY_ROW, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0})
        s = tel.daily_summary("2026-01-01")
        assert s["prefix_cache_hit_rate"] == 0.0


class TestPricing:
    """U9：MODEL_PRICES 含 glm 免费模型条目 + pricing_for 未定价判定。"""

    def test_glm_models_prices(self):
        # glm-4.5-air 有真实价格（2026-08 智谱官方核：输入 0.8/输出 2.0/缓存 0.16，元/百万 tokens）
        p = MODEL_PRICES["glm-4.5-air"]
        assert p["cache_hit_input"] == 0.16
        assert p["input"] == 0.8
        assert p["output"] == 2.0
        # glm-4-flash 老款长期免费
        p = MODEL_PRICES["glm-4-flash"]
        assert p["cache_hit_input"] == 0.0
        assert p["input"] == 0.0
        assert p["output"] == 0.0

    def test_glm_cost_computation_real(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "model": "glm-4.5-air",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "prompt_cache_hit_tokens": 600,
                "prompt_cache_miss_tokens": 400,
                "cache_hit": False,
            }
        )
        # 600/1e6×0.16 + 400/1e6×0.8 + 500/1e6×2.0 = 0.001416
        assert tel.daily_summary("2026-01-01")["estimated_cost_usd"] == pytest.approx(0.001416)

    def test_unknown_model_estimate_zero_but_pricing_none(self):
        assert Telemetry.pricing_for("no-such-model") is None
        assert (
            Telemetry._estimate_cost(
                {
                    "model": "no-such-model",
                    "prompt_cache_hit_tokens": 100,
                    "prompt_cache_miss_tokens": 100,
                    "completion_tokens": 100,
                }
            )
            == 0.0
        )

    def test_pricing_for_known_model_returns_dict(self):
        assert Telemetry.pricing_for("deepseek-v4-flash") == MODEL_PRICES["deepseek-v4-flash"]
        assert Telemetry.pricing_for("glm-4.5-air") == MODEL_PRICES["glm-4.5-air"]


class TestLoopIntegration:
    def test_loop_records_usage_with_cache_fields(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            prompt_cache_hit_tokens=80,
            prompt_cache_miss_tokens=20,
        )
        loop, llm = make_env(
            [make_response(make_message(content="答"), "stop", usage=usage)],
            telemetry=tel,
        )
        loop.run("你好")
        lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["prompt_tokens"] == 100
        assert row["completion_tokens"] == 20
        assert row["prompt_cache_hit_tokens"] == 80
        assert row["prompt_cache_miss_tokens"] == 20
        assert row["cache_hit"] is False
        assert row["model"] == "deepseek-v4-flash"
        assert row["mode"] == "test"
        assert row["step"] == 1
        assert row["ts"].startswith("2026") or row["ts"]  # 非空时间戳

    def test_usage_without_cache_fields_defaults_zero(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        loop, llm = make_env(
            [make_response(make_message(content="x"), "stop", usage=usage)],
            telemetry=tel,
        )
        loop.run("hi")
        row = json.loads(
            (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert row["prompt_cache_hit_tokens"] == 0
        assert row["prompt_cache_miss_tokens"] == 0

    def test_loop_records_reasoning_tokens(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=778),
        )
        loop, llm = make_env(
            [make_response(make_message(content="答"), "stop", usage=usage)],
            telemetry=tel,
        )
        loop.run("你好")
        row = json.loads(
            (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert row["reasoning_tokens"] == 778

    def test_loop_records_reasoning_tokens_none_when_absent(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        loop, llm = make_env(
            [make_response(make_message(content="答"), "stop", usage=usage)],
            telemetry=tel,
        )
        loop.run("你好")
        row = json.loads(
            (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert row["reasoning_tokens"] is None

    def test_multi_step_records_each_round(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tc = SimpleNamespace(
            id="c1",
            type="function",
            function=SimpleNamespace(name="add", arguments='{"a": 1, "b": 2}'),
        )
        loop, llm = make_env(
            [
                make_response(make_message(content=None, tool_calls=[tc]), "tool_calls"),
                make_response(make_message(content="3"), "stop"),
            ],
            telemetry=tel,
        )
        loop.run("算")
        lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert [json.loads(line)["step"] for line in lines] == [1, 2]

    def test_exact_cache_hit_records_true_row(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        cache = FakeCache()
        loop, llm = make_env(
            [make_response(make_message(content="答案"))], cache=cache, telemetry=tel
        )
        loop.run("问题")
        loop.run("问题")
        rows = [
            json.loads(line)
            for line in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 2
        assert rows[0]["cache_hit"] is False
        assert rows[0]["prompt_tokens"] == 10
        assert rows[1]["cache_hit"] is True
        assert rows[1]["prompt_tokens"] == 0
        assert rows[1]["completion_tokens"] == 0
        assert len(llm.chat.completions.calls) == 1

    def test_without_telemetry_behavior_unchanged(self, tmp_path):
        loop, llm = make_env([make_response(make_message(content="42"))])
        assert loop.telemetry is None
        assert loop.run("你好") == "42"
        assert len(llm.chat.completions.calls) == 1
        assert not (tmp_path / "t.jsonl").exists()

    def test_misbehaving_telemetry_does_not_break_loop(self, tmp_path):
        class Boom:
            def record(self, entry):
                raise RuntimeError("boom")

        loop, llm = make_env(
            [make_response(make_message(content="42"))], telemetry=Boom()
        )
        assert loop.run("你好") == "42"


class TestDegrade:
    def test_record_failure_degrades_gracefully(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("file, not dir", encoding="utf-8")
        tel = Telemetry(str(blocker / "t.jsonl"))
        tel.record({"ts": "x", "model": "m"})  # 必须不抛异常

    def test_init_unwritable_parent_degrades(self, tmp_path):
        blocker = tmp_path / "blocker2"
        blocker.write_text("file", encoding="utf-8")
        tel = Telemetry(str(blocker / "sub" / "t.jsonl"))
        tel.record({"ts": "x", "model": "m"})

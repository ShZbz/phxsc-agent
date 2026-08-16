"""settings.py 持久化测试：读写/损坏兜底/非法档位回退/保留其他键。"""

import json

import pytest

from phxsc import settings
from phxsc.agent.thinking import ThinkingLevel


def test_load_default_when_missing(tmp_path):
    assert settings.load_settings(str(tmp_path / "nope.json")) == {
        "thinking_level": "high",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "mem_gate": "strict",
        "mem_sim_threshold": 0.92,
        "mem_merge": "off",
        "moa_workers": '["deepseek:deepseek-v4-flash", "zhipu:glm-4.5-air", "deepseek:deepseek-v4-flash"]',
    }


def test_load_corrupt_json_falls_back(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{broken", encoding="utf-8")
    assert settings.load_settings(str(p))["thinking_level"] == "high"


def test_load_non_dict_falls_back(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert settings.load_settings(str(p))["thinking_level"] == "high"


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "settings.json"  # 目录不存在也要能建
    settings.save_settings({"thinking_level": "low"}, str(p))
    assert settings.load_settings(str(p))["thinking_level"] == "low"


def test_load_merges_missing_keys_with_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert settings.load_settings(str(p)) == {
        "thinking_level": "high",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "mem_gate": "strict",
        "mem_sim_threshold": 0.92,
        "mem_merge": "off",
        "moa_workers": '["deepseek:deepseek-v4-flash", "zhipu:glm-4.5-air", "deepseek:deepseek-v4-flash"]',
        "other": 1,
    }


def test_load_thinking_level_invalid_value_falls_back(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"thinking_level": "turbo"}), encoding="utf-8")
    assert settings.load_thinking_level(str(p)) == "high"


def test_load_thinking_level_non_string_falls_back(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"thinking_level": 42}), encoding="utf-8")
    assert settings.load_thinking_level(str(p)) == "high"


def test_save_thinking_level_preserves_other_keys(tmp_path):
    p = tmp_path / "settings.json"
    settings.save_settings({"thinking_level": "high", "other": "x"}, str(p))
    settings.save_thinking_level(ThinkingLevel.LOW, str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data == {
        "thinking_level": "low",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "mem_gate": "strict",
        "mem_sim_threshold": 0.92,
        "mem_merge": "off",
        "moa_workers": '["deepseek:deepseek-v4-flash", "zhipu:glm-4.5-air", "deepseek:deepseek-v4-flash"]',
        "other": "x",
    }


def test_load_provider_default(tmp_path):
    assert settings.load_provider(str(tmp_path / "nope.json")) == "deepseek"


def test_load_model_default(tmp_path):
    assert settings.load_model(str(tmp_path / "nope.json")) == "deepseek-v4-flash"


def test_load_provider_invalid_falls_back(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"provider": 42}), encoding="utf-8")
    assert settings.load_provider(str(p)) == "deepseek"


def test_load_model_roundtrip(tmp_path):
    p = tmp_path / "settings.json"
    settings.save_settings({"provider": "zhipu", "model": "glm-4.5-air"}, str(p))
    assert settings.load_provider(str(p)) == "zhipu"
    assert settings.load_model(str(p)) == "glm-4.5-air"

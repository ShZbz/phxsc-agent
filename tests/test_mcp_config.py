"""phxsc.mcp.json 配置解析与校验测试（v0.0.14）。

load_config：无配置文件 → {"servers": {}}；合法配置解析；JSON 损坏不抛；
validate_config：返回错误列表（server 名非法 / type 非法 / stdio 缺 command /
http 缺 url），空列表 = 合法。
"""

import json

from phxsc.mcp.config import load_config, validate_config


class TestLoadConfig:
    def test_missing_file_returns_empty_servers(self, tmp_path):
        cfg = load_config(str(tmp_path / "no_such.json"))
        assert cfg == {"servers": {}}

    def test_valid_config_parsed(self, tmp_path):
        path = tmp_path / "phxsc.mcp.json"
        path.write_text(
            json.dumps(
                {
                    "servers": {
                        "fixture": {
                            "type": "stdio",
                            "command": ["python", "server.py"],
                            "allowed_modes": ["plan", "investigate"],
                        },
                        "remote": {"type": "http", "url": "http://localhost:8000/mcp"},
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(str(path))
        assert set(cfg["servers"]) == {"fixture", "remote"}
        assert cfg["servers"]["remote"]["url"].startswith("http://")

    def test_corrupt_json_returns_empty_servers(self, tmp_path):
        path = tmp_path / "phxsc.mcp.json"
        path.write_text("{ not valid json !!!", encoding="utf-8")
        assert load_config(str(path)) == {"servers": {}}


class TestValidateConfig:
    def test_valid_config_returns_empty_list(self):
        cfg = {"servers": {"fixture": {"type": "stdio", "command": ["x"]}}}
        assert validate_config(cfg) == []

    def test_invalid_type(self):
        cfg = {"servers": {"fixture": {"type": "ssh", "command": ["x"]}}}
        errors = validate_config(cfg)
        assert any("type" in e for e in errors)

    def test_stdio_missing_command(self):
        cfg = {"servers": {"fixture": {"type": "stdio"}}}
        errors = validate_config(cfg)
        assert any("command" in e for e in errors)

    def test_http_missing_url(self):
        cfg = {"servers": {"fixture": {"type": "http"}}}
        errors = validate_config(cfg)
        assert any("url" in e for e in errors)

    def test_invalid_server_name(self):
        cfg = {"servers": {"bad-name!": {"type": "stdio", "command": ["x"]}}}
        errors = validate_config(cfg)
        assert any("名" in e for e in errors)

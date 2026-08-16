"""phxsc.mcp.json 配置解析与校验（v0.0.14）。

配置文件位于项目根（与 cli._project_root() 同一逻辑）。load_config 容忍缺失/
损坏文件返回空配置，保证 CLI 启动不因配置问题崩溃；validate_config 返回错误列表。
"""

import json
import re
from pathlib import Path

CONFIG_NAME = "phxsc.mcp.json"
_VALID_TYPES = ("stdio", "http")
_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")


def _project_root() -> Path:
    """项目根目录（src/phxsc/mcp/config.py 向上四级）。"""
    return Path(__file__).resolve().parents[3]


def _default_config_path() -> Path:
    return _project_root() / CONFIG_NAME


def load_config(path: str | None = None) -> dict:
    """读取 phxsc.mcp.json；缺失或 JSON 损坏 → {"servers": {}}（不抛错）。

    非 dict 顶层 / 无 servers 键同样回落空配置。
    """
    cfg_path = Path(path) if path else _default_config_path()
    if not cfg_path.is_file():
        return {"servers": {}}
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"servers": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("servers"), dict):
        return {"servers": {}}
    return raw


def validate_config(cfg: dict) -> list[str]:
    """校验配置，返回错误列表；空列表 = 合法。

    检查项：server 名合法（字母数字下划线）、type 在 stdio|http、
    stdio 缺 command、http 缺 url。
    """
    errors: list[str] = []
    servers = cfg.get("servers")
    if not isinstance(servers, dict):
        return errors
    for name, server in servers.items():
        if not _SERVER_NAME_RE.match(name):
            errors.append(f"server 名非法: {name!r}（仅允许字母/数字/下划线）")
        if not isinstance(server, dict):
            errors.append(f"server {name!r}: 配置必须是对象")
            continue
        stype = server.get("type")
        if stype not in _VALID_TYPES:
            errors.append(
                f"server {name!r}: type 必须是 {'/'.join(_VALID_TYPES)}（当前 {stype!r}）"
            )
        if stype == "stdio" and not server.get("command"):
            errors.append(f"server {name!r}: stdio 类型缺少 command")
        if stype == "http" and not server.get("url"):
            errors.append(f"server {name!r}: http 类型缺少 url")
    return errors

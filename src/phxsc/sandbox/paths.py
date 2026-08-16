"""PhySc-agent 文件沙箱路径校验。

白名单 + 黑名单双机制（借鉴 Hermes agent/file_safety.py）：
- safe_read_path：realpath 解析后必须落在 workdir 内（等于 workdir 也允许）
- safe_write_path：白名单 + home 下敏感路径黑名单（永远拒绝写入）
所有文件工具统一过闸，无例外。只用 stdlib（os）。
"""

import os

# home 目录下永远拒绝写入的敏感路径（相对 home 的名称/相对子路径）
_SENSITIVE_HOME_NAMES = (
    ".ssh",
    ".hermes",
    ".config",
    ".local/share",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".netrc",
    ".pypirc",
    ".aws",
    ".gnupg",
)


def _denied(error: str, reason: str, fix_hint: str) -> ValueError:
    """结构化错误：{error, reason, fix_hint} 风格。"""
    return ValueError(f"{error} | reason: {reason} | fix_hint: {fix_hint}")


def _resolve(path: str, workdir: str) -> str:
    """白名单解析：相对路径按 workdir 解析，realpath 后必须落在 workdir 内。"""
    workdir_real = os.path.realpath(workdir)
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        target = expanded
    else:
        target = os.path.join(workdir, expanded)
    target_real = os.path.realpath(target)
    if target_real != workdir_real and not target_real.startswith(workdir_real + os.sep):
        raise _denied(
            f"拒绝访问 workdir 外路径 {path!r}（解析到 {target_real}）",
            "path escapes the sandbox workdir",
            f"使用 workdir（{workdir_real}）内的路径",
        )
    return target_real


def _home_blacklist() -> tuple[str, ...]:
    home = os.path.realpath(os.path.expanduser("~"))
    return tuple(os.path.realpath(os.path.join(home, name)) for name in _SENSITIVE_HOME_NAMES)


def _check_blacklist(target_real: str) -> None:
    for denied in _home_blacklist():
        if target_real == denied or target_real.startswith(denied + os.sep):
            raise _denied(
                f"拒绝写入敏感路径 {target_real}",
                "path is on the home-directory blacklist",
                "敏感路径不可写入；请使用 workdir 内的路径",
            )


def safe_read_path(path: str, workdir: str) -> str:
    """白名单校验（只读）。通过返回 realpath，失败 raise ValueError。"""
    return _resolve(path, workdir)


def safe_write_path(path: str, workdir: str) -> str:
    """白名单 + 黑名单校验（写入）。通过返回 realpath，失败 raise ValueError。"""
    target_real = _resolve(path, workdir)
    _check_blacklist(target_real)
    return target_real

"""PhySc-agent 文件沙箱路径校验测试。

约定：workdir 使用项目根目录（避免 /tmp 与 /mnt 的 realpath 差异）；
symlink 等临时产物创建在项目目录内的临时子目录（tempfile），测完清理。
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from phxsc.sandbox.paths import safe_read_path, safe_write_path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = PROJECT_ROOT


@pytest.fixture()
def scratch():
    """项目目录内的临时目录（drvfs 同一文件系统，realpath 行为一致），测完清理。"""
    d = Path(tempfile.mkdtemp(prefix="phxsc_sandbox_", dir=PROJECT_ROOT))
    yield d
    for child in d.iterdir():
        if child.is_symlink():
            child.unlink()
    shutil.rmtree(d, ignore_errors=True)


class TestSafeReadPath:
    def test_relative_path_inside_workdir_passes(self):
        resolved = safe_read_path("pyproject.toml", WORKDIR)
        assert resolved == os.path.realpath(os.path.join(WORKDIR, "pyproject.toml"))

    def test_nested_relative_path_passes(self):
        resolved = safe_read_path(os.path.join("src", "phxsc", "__init__.py"), WORKDIR)
        assert resolved == os.path.realpath(
            os.path.join(WORKDIR, "src", "phxsc", "__init__.py")
        )

    def test_workdir_itself_passes(self):
        assert safe_read_path(str(WORKDIR), WORKDIR) == os.path.realpath(WORKDIR)

    def test_absolute_path_inside_workdir_passes(self):
        target = os.path.join(WORKDIR, "pyproject.toml")
        assert safe_read_path(target, WORKDIR) == os.path.realpath(target)

    def test_absolute_path_outside_workdir_rejected(self):
        with pytest.raises(ValueError):
            safe_read_path(str(PROJECT_ROOT.parent), WORKDIR)

    def test_dotdot_escape_rejected(self):
        with pytest.raises(ValueError):
            safe_read_path("../pyproject.toml", WORKDIR)

    def test_symlink_escaping_workdir_rejected(self, scratch):
        link = scratch / "escape_link"
        link.symlink_to(PROJECT_ROOT.parent)
        with pytest.raises(ValueError):
            safe_read_path(str(link), WORKDIR)

    def test_error_message_contains_reason_fields(self):
        with pytest.raises(ValueError) as exc:
            safe_read_path("../pyproject.toml", WORKDIR)
        msg = str(exc.value)
        assert "reason" in msg
        assert "fix_hint" in msg


class TestSafeWritePath:
    def test_write_inside_workdir_allowed(self):
        target = os.path.join("workspace", "tmp", "x.txt")
        resolved = safe_write_path(target, WORKDIR)
        assert resolved == os.path.realpath(os.path.join(WORKDIR, target))

    def test_write_workdir_itself_allowed(self):
        assert safe_write_path(str(WORKDIR), WORKDIR) == os.path.realpath(WORKDIR)

    def test_write_dotdot_escape_rejected(self):
        with pytest.raises(ValueError):
            safe_write_path("../evil.py", WORKDIR)

    def test_write_absolute_outside_workdir_rejected(self):
        with pytest.raises(ValueError):
            safe_write_path(os.path.join(str(PROJECT_ROOT.parent), "evil.py"), WORKDIR)

    @pytest.mark.parametrize(
        "bad",
        [
            "~/.ssh/id_rsa",
            "~/.hermes/token.json",
            "~/.config/app/config.toml",
            "~/.local/share/notes.db",
            "~/.bashrc",
        ],
    )
    def test_blacklist_sensitive_paths_rejected(self, bad):
        with pytest.raises(ValueError):
            safe_write_path(bad, WORKDIR)

    def test_blacklist_still_denied_when_workdir_is_home(self):
        home = os.path.expanduser("~")
        with pytest.raises(ValueError):
            safe_write_path("~/.ssh/id_rsa", home)

    def test_blacklist_rejects_within_blacklisted_dir_even_inside_workdir(self, scratch):
        home = os.path.expanduser("~")
        workdir = home
        with pytest.raises(ValueError):
            safe_write_path(".config/sub/file.txt", workdir)

    def test_write_error_message_contains_reason_fields(self):
        with pytest.raises(ValueError) as exc:
            safe_write_path("~/.bashrc", WORKDIR)
        msg = str(exc.value)
        assert "reason" in msg
        assert "fix_hint" in msg

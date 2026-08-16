"""_load_dotenv 测试：.env 文件解析、已有环境变量优先、引号剥离。"""

import os

from phxsc.cli import _default_env_path, _load_dotenv, _project_root


def test_load_dotenv_basic(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-test123\n"
        "ZHIPU_API_KEY=zhipu-abc\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHXSC_ENV_FILE", str(env_file))
    # 确保干净
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    _load_dotenv()
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-test123"
    assert os.environ["ZHIPU_API_KEY"] == "zhipu-abc"


def test_load_dotenv_existing_env_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-fromfile\n", encoding="utf-8")
    monkeypatch.setenv("PHXSC_ENV_FILE", str(env_file))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fromshell")
    _load_dotenv()
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-fromshell"


def test_load_dotenv_quotes_stripped(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('DEEPSEEK_API_KEY="sk-quoted"\n', encoding="utf-8")
    monkeypatch.setenv("PHXSC_ENV_FILE", str(env_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _load_dotenv()
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-quoted"


def test_load_dotenv_comments_and_blank(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n\n\nDEEPSEEK_API_KEY=sk-x\n# 尾部注释\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHXSC_ENV_FILE", str(env_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _load_dotenv()
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-x"


def test_load_dotenv_missing_file_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("PHXSC_ENV_FILE", str(tmp_path / "nope.env"))
    _load_dotenv()  # 不抛异常


def test_load_dotenv_no_equals_ignored(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("INVALID_LINE_NO_EQUALS\n", encoding="utf-8")
    monkeypatch.setenv("PHXSC_ENV_FILE", str(env_file))
    _load_dotenv()  # 不抛异常


def test_default_env_path_is_project_root():
    assert _default_env_path() == str(_project_root() / ".env")


def test_load_dotenv_default_path_reads_project_root(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-root123\n", encoding="utf-8")
    monkeypatch.delenv("PHXSC_ENV_FILE", raising=False)
    monkeypatch.setattr("phxsc.cli._default_env_path", lambda: str(env_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _load_dotenv()
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-root123"

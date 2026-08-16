"""PhySc skill 体系核心测试（v0.0.13）。

覆盖：parse_skill_md / scan_skills / skill_dirs / build_metadata_table /
load_skill_body / AgentLoop._decorate_user 的 [skills] 段 / skill_load 工具 /
/skill list|load|unload 命令 / SLASH_COMMANDS 补全表 / 启动组装。
全部用 tmp_path 构造 SKILL.md，不碰真实目录。
"""

from types import SimpleNamespace

from rich.console import Console

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry
from phxsc.cli import SLASH_COMMANDS, _build_loop, _handle_skill, _register_tools
from phxsc.skills.loader import load_skill_body
from phxsc.skills.scan import (
    SkillMeta,
    build_metadata_table,
    parse_skill_md,
    scan_skills,
    skill_dirs,
)


def write_skill(root, name, description="测试技能", version="0.0.1", body=""):
    """在 root/<name>/SKILL.md 写一个合法 frontmatter 的技能文件，返回其路径。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    path = d / "SKILL.md"
    content = (
        f"---\nname: {name}\ndescription: {description}\nversion: {version}\n"
        f"---\n{body}"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _make_loop(loaded_skills=None, mode="investigate", voice="academic"):
    """最小 AgentLoop（不 run，只测 _decorate_user）。"""
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    return AgentLoop(
        llm_client=None,
        registry=ToolRegistry(),
        context=cm,
        model="deepseek-v4-flash",
        mode=mode,
        voice=voice,
        loaded_skills=loaded_skills,
    )


class TestParseSkillMd:
    """parse_skill_md：合法解析 / 缺 description / frontmatter 损坏 / 非法 name。"""

    def test_valid_frontmatter_parsed(self, tmp_path):
        path = write_skill(tmp_path, "my-skill", "一个测试技能", "1.2.3", "正文")
        meta = parse_skill_md(path)
        assert meta is not None
        assert meta.name == "my-skill"
        assert meta.description == "一个测试技能"
        assert meta.version == "1.2.3"
        assert meta.path == str(path.resolve())

    def test_missing_description_returns_none(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("---\nname: foo\nversion: 0.1.0\n---\n正文", encoding="utf-8")
        assert parse_skill_md(path) is None

    def test_no_frontmatter_delimiters_returns_none(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("name: foo\ndescription: bar\n", encoding="utf-8")
        assert parse_skill_md(path) is None

    def test_truncated_frontmatter_returns_none(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("---\nname: foo\ndescription: bar\n", encoding="utf-8")
        assert parse_skill_md(path) is None

    def test_invalid_name_returns_none(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text(
            "---\nname: Foo Bar!\ndescription: 技能\nversion: 0.1.0\n---\n",
            encoding="utf-8",
        )
        assert parse_skill_md(path) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert parse_skill_md(tmp_path / "nope" / "SKILL.md") is None


class TestSkillDirs:
    """skill_dirs：显式目录优先、PHXSC_SKILLS 覆盖用户级、目录不存在跳过。"""

    def test_env_overrides_user_dir(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        user = tmp_path / "user"
        proj.mkdir()
        user.mkdir()
        monkeypatch.setenv("PHXSC_SKILLS", str(user))
        assert skill_dirs(project_dir=str(proj)) == [str(proj), str(user)]

    def test_missing_dirs_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHXSC_SKILLS", str(tmp_path / "nope"))
        assert skill_dirs(project_dir=str(tmp_path / "also_nope")) == []


class TestScanSkills:
    """scan_skills：两目录扫描 / 目录不存在跳过 / 同名去重用户级优先。"""

    def test_scans_both_dirs(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        user = tmp_path / "user"
        write_skill(proj, "alpha", "项目技能")
        write_skill(user, "beta", "用户技能")
        monkeypatch.setattr(
            "phxsc.skills.scan.skill_dirs", lambda: [str(proj), str(user)]
        )
        names = {m.name for m in scan_skills()}
        assert names == {"alpha", "beta"}

    def test_missing_dirs_return_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "phxsc.skills.scan.skill_dirs",
            lambda: [str(tmp_path / "nope1"), str(tmp_path / "nope2")],
        )
        assert scan_skills() == []

    def test_user_overrides_project_same_name(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        user = tmp_path / "user"
        write_skill(proj, "shared", "项目版")
        write_skill(user, "shared", "用户版")
        monkeypatch.setattr(
            "phxsc.skills.scan.skill_dirs", lambda: [str(proj), str(user)]
        )
        metas = scan_skills()
        assert len(metas) == 1
        assert metas[0].name == "shared"
        assert metas[0].description == "用户版"

    def test_user_skills_first_in_list(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        user = tmp_path / "user"
        write_skill(proj, "alpha", "项目技能")
        write_skill(user, "beta", "用户技能")
        monkeypatch.setattr(
            "phxsc.skills.scan.skill_dirs", lambda: [str(proj), str(user)]
        )
        assert [m.name for m in scan_skills()] == ["beta", "alpha"]


class TestBuildMetadataTable:
    """build_metadata_table：格式正确；description 超长截断 ≤160 字符。"""

    def test_format_lines(self):
        metas = [
            SkillMeta(name="foo", description="描述一", version="0.1.0", path="/x/foo"),
            SkillMeta(name="bar", description="描述二", version="0.2.0", path="/x/bar"),
        ]
        text = build_metadata_table(metas)
        assert text.startswith("可用技能（任务匹配时可用 skill_load 工具加载全文）：")
        assert "- foo: 描述一" in text
        assert "- bar: 描述二" in text

    def test_empty_returns_empty_string(self):
        assert build_metadata_table([]) == ""

    def test_long_description_truncated(self):
        long_desc = "长" * 200
        metas = [SkillMeta(name="foo", description=long_desc, version="0.1.0", path="/x")]
        text = build_metadata_table(metas)
        line = next(l for l in text.splitlines() if l.startswith("- foo:"))
        assert len(line) <= 160
        assert line.endswith("...")


class TestLoadSkillBody:
    """load_skill_body：命中返回正文+资源清单 / 未命中 None / 超长返回全文不截断。"""

    def test_hit_returns_content_and_resources(self, tmp_path):
        for sub, name in (("references", "ref.md"), ("scripts", "run.sh"),
                          ("templates", "t.md")):
            d = tmp_path / "foo" / sub
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text("x", encoding="utf-8")
        path = write_skill(tmp_path, "foo", "描述", "0.1.0", "正文内容")
        metas = [parse_skill_md(path)]
        body = load_skill_body("foo", metas)
        assert body is not None
        assert body.name == "foo"
        assert "正文内容" in body.content
        assert body.resources == ["ref.md", "run.sh", "t.md"]

    def test_miss_returns_none(self):
        metas = [SkillMeta(name="a", description="d", version="", path="/x")]
        assert load_skill_body("missing", metas) is None

    def test_oversize_returns_full_body(self, tmp_path):
        body_text = "x" * 3000
        path = write_skill(tmp_path, "big", "描述", "0.1.0", body_text)
        metas = [parse_skill_md(path)]
        body = load_skill_body("big", metas)
        assert body is not None
        assert body.content == path.read_text(encoding="utf-8")
        assert len(body.content) == len(path.read_text(encoding="utf-8"))
        assert "[已截断]" not in body.content

    def test_under_limit_untouched(self, tmp_path):
        path = write_skill(tmp_path, "small", "描述", "0.1.0", "短正文")
        metas = [parse_skill_md(path)]
        body = load_skill_body("small", metas)
        assert body is not None
        assert "[已截断]" not in body.content


class TestDecorateUserSkills:
    """_decorate_user [skills] 段：空不加（字节不变）；非空全文注入不截断。"""

    def test_empty_loaded_skills_no_skills_block(self):
        loop = _make_loop()
        assert loop._decorate_user("你好") == "[mode: investigate]\n你好"
        loop2 = _make_loop(loaded_skills={})
        assert loop2._decorate_user("你好") == "[mode: investigate]\n你好"

    def test_nonempty_injects_skills_block_in_order(self):
        loaded = {"foo": "技能正文A", "bar": "技能正文B"}
        loop = _make_loop(loaded_skills=loaded)
        decorated = loop._decorate_user("你好")
        assert "\n[skills]\n" in decorated
        assert "### foo\n技能正文A" in decorated
        assert "### bar\n技能正文B" in decorated
        assert decorated.endswith("技能正文B\n你好")

    def test_inject_under_limit_full(self):
        loaded = {"foo": "短正文"}
        loop = _make_loop(loaded_skills=loaded)
        decorated = loop._decorate_user("你好")
        assert "### foo\n短正文" in decorated
        assert "技能内容已截断" not in decorated

    def test_inject_full_no_truncation(self):
        loaded = {"big": "x" * 3000}
        loop = _make_loop(loaded_skills=loaded)
        decorated = loop._decorate_user("你好")
        assert decorated == f"[mode: investigate]\n[skills]\n### big\n{'x' * 3000}\n你好"
        assert "技能内容已截断" not in decorated

    def test_holds_cli_dict_reference(self):
        loaded = {}
        loop = _make_loop(loaded_skills=loaded)
        loop._decorate_user("x")
        loaded["foo"] = "正文"
        assert "### foo" in loop._decorate_user("x")


class TestSkillLoadTool:
    """skill_load 工具：命中返回正文（含资源清单）；未命中结构化错误。"""

    def test_hit_returns_content_with_resources(self, tmp_path):
        d = tmp_path / "foo" / "references"
        d.mkdir(parents=True)
        (d / "ref.md").write_text("r", encoding="utf-8")
        path = write_skill(tmp_path, "foo", "描述", "0.1.0", "正文")
        metas = [parse_skill_md(path)]
        reg = _register_tools(ToolRegistry(), skill_metas=metas)
        result = reg.call("skill_load", {"name": "foo"})
        assert isinstance(result, str)
        assert "正文" in result
        assert "资源文件：ref.md" in result

    def test_miss_structured_error_with_fix_hint(self):
        metas = [SkillMeta(name="a", description="d", version="", path="/x")]
        reg = _register_tools(ToolRegistry(), skill_metas=metas)
        result = reg.call("skill_load", {"name": "nope"})
        assert isinstance(result, dict)
        assert "error" in result
        assert result["fix_hint"] == "先 /skill list 查看可用技能"


class TestSkillCommand:
    """/skill list|load|unload 三个子命令。"""

    @staticmethod
    def _metas_from(tmp_path):
        return [parse_skill_md(p) for p in sorted(tmp_path.glob("*/SKILL.md"))]

    def test_list_shows_all_skill_names(self, tmp_path):
        write_skill(tmp_path, "alpha", "技能A", "0.1.0")
        write_skill(tmp_path, "beta", "技能B", "0.2.0")
        metas = self._metas_from(tmp_path)
        console = Console(record=True)
        _handle_skill(console, metas, {}, "/skill list")
        text = console.export_text()
        assert "alpha" in text
        assert "beta" in text
        assert "技能A" in text
        assert "技能B" in text

    def test_list_marks_loaded(self, tmp_path):
        write_skill(tmp_path, "alpha", "技能A", "0.1.0", "aaaaa")
        write_skill(tmp_path, "beta", "技能B", "0.2.0", "bb")
        metas = self._metas_from(tmp_path)
        loaded = {"alpha": "aaaaa"}
        console = Console(record=True)
        _handle_skill(console, metas, loaded, "/skill list")
        text = console.export_text()
        assert "★ 已加载" in text
        assert text.count("★ 已加载") == 1
        assert "★ 已加载 5 字符" in text
        assert "beta" in text

    def test_load_adds_to_loaded(self, tmp_path):
        write_skill(tmp_path, "foo", "描述", "0.1.0", "正文内容")
        metas = self._metas_from(tmp_path)
        loaded = {}
        console = Console(record=True)
        _handle_skill(console, metas, loaded, "/skill load foo")
        assert "foo" in loaded
        assert "正文内容" in loaded["foo"]
        assert "已加载 foo" in console.export_text()

    def test_reload_updates_no_duplicate(self, tmp_path):
        write_skill(tmp_path, "foo", "描述", "0.1.0", "正文一")
        metas = self._metas_from(tmp_path)
        loaded = {}
        console = Console(record=True)
        _handle_skill(console, metas, loaded, "/skill load foo")
        (tmp_path / "foo" / "SKILL.md").write_text(
            "---\nname: foo\ndescription: 描述\nversion: 0.1.0\n---\n正文二",
            encoding="utf-8",
        )
        _handle_skill(console, metas, loaded, "/skill load foo")
        assert len(loaded) == 1
        assert "正文二" in loaded["foo"]
        assert "正文一" not in loaded["foo"]

    def test_capacity_eight_rejects_ninth(self, tmp_path):
        for i in range(9):
            write_skill(tmp_path, f"s{i}", "描述", "0.1.0", f"正文{i}")
        metas = self._metas_from(tmp_path)
        loaded = {m.name: "x" for m in metas[:8]}
        console = Console(record=True)
        _handle_skill(console, metas, loaded, "/skill load s8")
        assert "s8" not in loaded
        assert "上限" in console.export_text()

    def test_unload_removes(self):
        loaded = {"foo": "正文"}
        console = Console(record=True)
        _handle_skill(console, [], loaded, "/skill unload foo")
        assert loaded == {}
        assert "已卸载 foo" in console.export_text()

    def test_unload_missing_prints_message(self):
        console = Console(record=True)
        _handle_skill(console, [], {}, "/skill unload foo")
        assert "未加载" in console.export_text()

    def test_loaded_empty(self):
        console = Console(record=True)
        _handle_skill(console, [], {}, "/skill loaded")
        text = console.export_text()
        assert "注入轨为空" in text
        assert "load" in text

    def test_loaded_lists(self):
        loaded = {"foo": "12345", "bar": "x" * 10}
        console = Console(record=True)
        _handle_skill(console, [], loaded, "/skill loaded")
        text = console.export_text()
        assert "- foo（5 字符）" in text
        assert "- bar（10 字符）" in text
        assert "注入轨总量 15 字符" in text

    def test_load_warns_over_8kb(self, monkeypatch):
        monkeypatch.setattr(
            "phxsc.cli.load_skill_body",
            lambda name, metas: SimpleNamespace(name=name, content="x" * 9000, resources=[]),
        )
        loaded = {}
        console = Console(record=True)
        _handle_skill(console, [], loaded, "/skill load big")
        text = console.export_text()
        assert "big" in loaded
        assert "已加载 big" in text
        assert "⚠️ 注入轨当前总量" in text

    def test_load_no_warn_under_8kb(self, tmp_path):
        write_skill(tmp_path, "small", "描述", "0.1.0", "短正文")
        metas = self._metas_from(tmp_path)
        loaded = {}
        console = Console(record=True)
        _handle_skill(console, metas, loaded, "/skill load small")
        text = console.export_text()
        assert "small" in loaded
        assert "已加载 small" in text
        assert "⚠️ 注入轨当前总量" not in text

    def test_bad_usage(self):
        console = Console(record=True)
        _handle_skill(console, [], {}, "/skill")
        assert "用法" in console.export_text()


def test_slash_commands_include_skill():
    assert "/skill" in SLASH_COMMANDS


class TestStartupAssembly:
    """启动组装：元数据表进 system prompt 一次；loaded_skills 引用共享。"""

    def test_metadata_table_in_system_prompt_once(self):
        skills_table = build_metadata_table(
            [SkillMeta(name="foo", description="技能", version="0.1.0", path="/x")]
        )
        loop = _build_loop(
            client=object(),
            registry=ToolRegistry(),
            mode="investigate",
            workdir="/tmp/xx",
            model="m",
            skills_table=skills_table,
        )
        prompt = loop.context._config.system_prompt
        assert skills_table in prompt
        assert prompt.count("可用技能（任务匹配时可用 skill_load 工具加载全文）：") == 1

    def test_no_table_no_skill_meta_in_prompt(self):
        loop = _build_loop(
            client=object(),
            registry=ToolRegistry(),
            mode="investigate",
            workdir="/tmp/xx",
            model="m",
        )
        assert "可用技能" not in loop.context._config.system_prompt

    def test_loaded_skills_reference_shared(self):
        loaded = {"foo": "正文"}
        loop = _build_loop(
            client=object(),
            registry=ToolRegistry(),
            mode="investigate",
            workdir="/tmp/xx",
            model="m",
            loaded_skills=loaded,
        )
        assert loop.loaded_skills is loaded

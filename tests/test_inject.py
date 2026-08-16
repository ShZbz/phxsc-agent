"""记忆注入轨测试：build_injection 纯函数 + cli 组装点。

- build_injection：type="important" 记忆进 agent 版 MEMORY.md（# 用户重要偏好
  标题 + "- " 列表），无重要记忆返回空串；超 IMPORTANT_LIMIT 截断并附截断提示。
- cli 组装：store 非空时 system_prompt 末尾追加注入文本（\n\n 分隔），空时不动。
所有 sqlite 文件用 tmp_path，测完清理。
"""

import pytest

from phxsc.agent.tools import ToolRegistry
from phxsc.cli import _build_loop
from phxsc.memory.inject import IMPORTANT_LIMIT, build_injection
from phxsc.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "memory.db"))
    yield s
    s.close()


def _important_md(*contents: str) -> str:
    """按 build_injection 期望的格式组装（供断言用）。"""
    return "# 用户重要偏好（MEMORY）\n" + "\n".join(f"- {c}" for c in contents)


class TestBuildInjection:
    def test_no_important_returns_empty(self, store):
        assert build_injection(store) == ""

    def test_only_important_injected_and_fact_excluded(self, store):
        store.add_memory("fact", "普通事实，不进注入", b"")
        store.add_memory("important", "偏好甲：用中文回答", b"")
        store.add_memory("important", "偏好乙：标注参考文献", b"")
        out = build_injection(store)
        assert out == _important_md("偏好甲：用中文回答", "偏好乙：标注参考文献")
        assert "普通事实" not in out

    def test_order_follows_id_ascending(self, store):
        store.add_memory("important", "第一条", b"")
        store.add_memory("important", "第二条", b"")
        out = build_injection(store)
        assert out.index("第一条") < out.index("第二条")

    def test_truncates_over_limit_with_notice(self, store):
        store.add_memory("important", "长" * 600, b"")
        store.add_memory("important", "中" * 600, b"")
        out = build_injection(store)
        assert len(out) > 0
        assert out.startswith("# 用户重要偏好（MEMORY）")
        assert "（记忆已截断，共 2 条）" in out
        # 截断到 IMPORTANT_LIMIT 后再追加提示
        content_len = len(out) - len("\n（记忆已截断，共 2 条）")
        assert content_len == IMPORTANT_LIMIT

    def test_below_limit_not_truncated(self, store):
        store.add_memory("important", "短记忆", b"")
        out = build_injection(store)
        assert out == _important_md("短记忆")
        assert "截断" not in out

    def test_pure_function_same_input_same_output(self, store):
        store.add_memory("important", "确定性内容", b"")
        assert build_injection(store) == build_injection(store)


class TestCliAssembly:
    def test_nonempty_injection_appended_to_system_prompt(self, tmp_path):
        s = MemoryStore(str(tmp_path / "m.db"))
        try:
            s.add_memory("important", "偏好：研究 Mn3Ga 反铁磁", b"")
            loop = _build_loop(
                client=object(),
                registry=ToolRegistry(),
                mode="investigate",
                workdir=str(tmp_path),
                model="m",
                store=s,
            )
        finally:
            s.close()
        prompt = loop.context._config.system_prompt
        assert "用户重要偏好" in prompt
        assert "偏好：研究 Mn3Ga 反铁磁" in prompt
        assert prompt.count("用户重要偏好") == 1
        assert "\n\n# 用户重要偏好" in prompt

    def test_empty_injection_leaves_prompt_unchanged(self, tmp_path):
        s = MemoryStore(str(tmp_path / "m.db"))
        try:
            loop = _build_loop(
                client=object(),
                registry=ToolRegistry(),
                mode="investigate",
                workdir=str(tmp_path),
                model="m",
                store=s,
            )
        finally:
            s.close()
        prompt = loop.context._config.system_prompt
        assert "用户重要偏好" not in prompt

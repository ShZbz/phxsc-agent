"""AgentLoop 精确缓存集成测试。

用 fake cache（内存 dict）注入 AgentLoop，验证：
- 第一次 run 命中 miss → 调 LLM 并 cache.set
- 第二次同 query → 直接返回缓存值，不调 LLM（fake client 计数验证）
- cache_hit 标志正确
- 无 cache 时行为不变（与既有 test_loop.py 的 21 个用例一致）
"""

from types import SimpleNamespace

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.cache.exact import ExactCache


class FakeCache:
    """内存版 ExactCache 近似：get/set/stats/close。"""

    def __init__(self):
        self.data = {}
        self.misses = 0
        self.get_calls = 0
        self.set_calls = 0

    def get(self, key):
        self.get_calls += 1
        if key in self.data:
            return self.data[key]
        self.misses += 1
        return None

    def set(self, key, value):
        self.set_calls += 1
        self.data[key] = value

    def stats(self):
        hits = len(self.data)
        return {
            "entries": len(self.data),
            "total_hits": self.get_calls - self.misses,
            "hit_rate": (self.get_calls - self.misses) / self.get_calls
            if self.get_calls
            else 0.0,
        }

    def close(self):
        pass


def make_message(content=None, tool_calls=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
    )


def make_response(message, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
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


def make_env(responses, cache=None):
    executed = []

    @tool(name="add", description="整数加法", mode="test")
    def add(a: int, b: int) -> int:
        executed.append((a, b))
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
    )
    return loop, llm, executed


class TestCacheHit:
    def test_first_run_calls_llm_and_sets_cache(self):
        cache = FakeCache()
        loop, llm, executed = make_env(
            [make_response(make_message(content="答案A"))], cache=cache
        )
        result = loop.run("问题A")
        assert result == "答案A"
        assert len(llm.chat.completions.calls) == 1
        assert cache.set_calls == 1
        assert executed == []

    def test_second_same_query_returns_cached_without_llm(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [
                make_response(make_message(content="答案A")),
                make_response(make_message(content="不该被调用")),
            ],
            cache=cache,
        )
        first = loop.run("问题A")
        second = loop.run("问题A")
        assert first == second == "答案A"
        assert len(llm.chat.completions.calls) == 1
        assert cache.set_calls == 1

    def test_cache_hit_flag_true_on_second_call(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答案A"))], cache=cache
        )
        assert loop.cache_hit is False
        loop.run("问题A")
        assert loop.cache_hit is False
        loop.run("问题A")
        assert loop.cache_hit is True

    def test_different_query_not_cached(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答案A"))], cache=cache
        )
        assert loop.run("问题A") == "答案A"
        loop2, llm2, _ = make_env(
            [make_response(make_message(content="答案B"))], cache=cache
        )
        assert loop2.run("问题B") == "答案B"
        assert len(llm.chat.completions.calls) == 1
        assert len(llm2.chat.completions.calls) == 1

    def test_tool_loop_result_also_cached(self):
        cache = FakeCache()
        loop, llm, executed = make_env(
            [
                make_response(
                    make_message(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call_1",
                                type="function",
                                function=SimpleNamespace(name="add", arguments='{"a": 1, "b": 2}'),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                ),
                make_response(make_message(content="工具算完 3")),
            ],
            cache=cache,
        )
        assert loop.run("算一下") == "工具算完 3"
        assert executed == [(1, 2)]
        assert cache.set_calls == 1


class TestCacheHitContextWriteback:
    """P2-6：exact 命中轮 Q&A 写回 context，追问轮 LLM 能看到历史。"""

    def test_exact_hit_appends_user_assistant_pair(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答案A"))], cache=cache
        )
        loop.run("问题A")  # miss → LLM + cache.set
        loop.run("问题A")  # hit → context 写入 Q&A
        msgs = loop.context.build_messages()
        assert [m["role"] for m in msgs] == [
            "system", "user", "assistant", "user", "assistant",
        ]
        assert msgs[-2]["content"].startswith("[mode: test]\n问题A")
        assert msgs[-1]["content"] == "答案A"

    def test_exact_hit_followup_sees_history(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [
                make_response(make_message(content="答案A")),
                make_response(make_message(content="追问答案")),
            ],
            cache=cache,
        )
        loop.run("问题A")  # miss
        loop.run("问题A")  # hit
        assert loop.run("刚才那个再详细点") == "追问答案"  # miss → LLM 见历史
        sent = llm.chat.completions.calls[1]["messages"]
        roles = [m["role"] for m in sent]
        assert roles[-3:] == ["user", "assistant", "user"]  # 缓存对 + 本轮追问
        assert sent[-3]["content"].startswith("[mode: test]\n问题A")
        assert sent[-2]["content"] == "答案A"
        assert sent[-1]["content"].startswith("[mode: test]\n刚才那个再详细点")

    def test_exact_hit_does_not_increment_set_calls(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答案A"))], cache=cache
        )
        loop.run("问题A")  # miss → set_calls=1
        assert cache.set_calls == 1
        loop.run("问题A")  # hit → set 不增加
        assert cache.set_calls == 1


class TestCacheSalt:
    """P3-2：exact key 掺模型/技能 salt——换配置后旧缓存不命中。"""

    def test_different_loaded_skills_do_not_hit_each_others_cache(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [
                make_response(make_message(content="旧答案")),
                make_response(make_message(content="新答案")),
            ],
            cache=cache,
        )
        loop.run("问题A")  # 无技能 → LLM 写缓存
        assert cache.set_calls == 1
        loop.loaded_skills = {"skill_x": "内容"}
        result = loop.run("问题A")  # 换技能 → 新 salt，不命中旧缓存
        assert result == "新答案"
        assert cache.set_calls == 2
        assert len(llm.chat.completions.calls) == 2

    def test_different_model_do_not_hit_each_others_cache(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [
                make_response(make_message(content="旧答案")),
                make_response(make_message(content="新答案")),
            ],
            cache=cache,
        )
        loop.run("问题A")
        loop.model = "another-model"
        result = loop.run("问题A")
        assert result == "新答案"
        assert len(llm.chat.completions.calls) == 2
        assert cache.set_calls == 2

    def test_same_config_hits_cache(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答案A"))], cache=cache
        )
        loop.run("问题A")
        assert loop.run("问题A") == "答案A"
        assert len(llm.chat.completions.calls) == 1
        assert loop.cache_hit is True


class TestNoCache:
    def test_no_cache_keeps_original_behavior(self):
        loop, llm, executed = make_env([make_response(make_message(content="42"))])
        assert loop.cache is None
        assert loop.cache_hit is False
        assert loop.run("你好") == "42"
        assert len(llm.chat.completions.calls) == 1
        assert executed == []

    def test_cache_hit_attribute_defaults_false(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="x"))], cache=cache
        )
        assert loop.cache_hit is False


class TestRealExactCacheIntegration:
    def test_roundtrip_with_real_cache(self, tmp_path):
        cache = ExactCache(str(tmp_path / "cache.db"))
        loop, llm, _ = make_env(
            [make_response(make_message(content="答案A"))], cache=cache
        )
        assert loop.run("问题A") == "答案A"
        assert loop.run("问题A") == "答案A"
        assert len(llm.chat.completions.calls) == 1
        assert cache.stats()["entries"] == 1
        assert loop.cache_hit is True
        cache.close()

    def test_key_uses_mode_and_query(self, tmp_path):
        cache = ExactCache(str(tmp_path / "cache.db"))
        loop, llm, _ = make_env(
            [make_response(make_message(content="m答案"))], cache=cache
        )
        loop.run("问题")
        stored_key = next(iter(cache._conn.execute("SELECT key FROM cache").fetchall()))[0]
        assert stored_key == ExactCache.key_for("问题", "test", salt=loop._cache_salt())
        cache.close()

    def test_same_query_different_mode_gives_different_key(self):
        assert ExactCache.key_for("问题", "plan") != ExactCache.key_for("问题", "investigate")

    def test_user_message_gets_mode_prefix_in_cache_miss_path(self):
        cache = FakeCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答案A"))], cache=cache
        )
        loop.run("问题A")
        assert len(llm.chat.completions.calls) == 1
        sent = llm.chat.completions.calls[0]["messages"]
        assert sent[1]["role"] == "user"
        assert sent[1]["content"].startswith("[mode: test]\n")


class TestRoleAlternation:
    """回归：纯文本回答（无工具调用）后，连续对话不触发 user→user 违规。

    历史 bug：run() 的最终回答从不写回 context，纯文本回答后 context
    停在 [system, user]，下一轮 append user 必炸。修复：run() 出口把
    最终回答 append 为 assistant 消息。
    """

    def test_two_rounds_plain_text_chat(self):
        loop, llm, _ = make_env(
            [
                make_response(SimpleNamespace(content="你好，我在", tool_calls=None)),
                make_response(SimpleNamespace(content="我很好，谢谢", tool_calls=None)),
            ]
        )
        ans1 = loop.run("你好")
        assert ans1 == "你好，我在"
        ans2 = loop.run("最近怎么样")
        assert ans2 == "我很好，谢谢"  # 第二轮不再抛 role 交替违规

    def test_assistant_message_written_back_to_context(self):
        loop, llm, _ = make_env(
            [make_response(SimpleNamespace(content="回答内容", tool_calls=None))]
        )
        loop.run("问题")
        roles = [m["role"] for m in loop.context.build_messages()]
        assert roles == ["system", "user", "assistant"]  # 最终回答已写回

    def test_longtask_result_also_written_back(self):
        """长任务路径（内部 reset 后 append user）→ run 出口 append assistant 仍合法。"""
        loop, llm, _ = make_env(
            [
                make_response(SimpleNamespace(content="1. 检索文献\n2. 阅读论文\n3. 总结机理", tool_calls=None)),  # 阶段1 规划
                make_response(SimpleNamespace(content="阶段2 执行完成", tool_calls=None)),  # 阶段2
            ]
        )
        # 触发长任务（"综述" 触发词）
        ans = loop.run("综述一下钙钛矿稳定性研究现状，整理研究脉络，写成笔记")
        roles = [m["role"] for m in loop.context.build_messages()]
        # 阶段1 结束后 reset，阶段2 首条是 user（计划+任务），出口 append assistant
        assert roles[-2:] == ["user", "assistant"]
        assert ans.startswith("阶段2 执行完成")
        assert "执行进度已记录：plans/" in ans
        assert "综述一下钙钛矿稳定性研究现状_整理研究脉络_写成笔记.md" in ans  # 文件名含时间戳前缀（分隔符清洗为 _）

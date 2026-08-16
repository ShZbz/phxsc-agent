"""测试基建（batch93 flaky 三件套之基建件）：统一 run_test 封装。

10 个 run_test 系 UI 测试文件此前各自定义/跨文件 import `_run_app`（headless
run_test + pilot.pause），test_ui_bugfix_audit 另有复制实现。统一收口到本
conftest：`run_test` fixture 三职责——headless（Textual run_test 默认）、
pilot.pause（首帧就绪）、退出前停 App 状态栏定时器（治本 shutdown 窗口竞态，
_textual _close_messages 停表晚于 _close_all 清 DOM）。

用法：
    def test_xxx(self, run_test):
        async def drive(app, pilot):
            ...
        run_test(app, drive=drive)
"""

import asyncio

import pytest


@pytest.fixture
def run_test():
    def _run(app, size=(120, 40), drive=None):
        async def _inner():
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                if drive is not None:
                    await drive(app, pilot)
                timer = getattr(app, "_tick_timer", None)
                if timer is not None:
                    timer.stop()

        asyncio.run(_inner())

    return _run

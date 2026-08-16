"""Sci-Hub / Chrome 集成测试（真实 Chrome + 网络，默认跳过）。

标记 @pytest.mark.integration（marker 已在 pyproject.toml 注册）。默认跳过：
设置 PHXSC_RUN_INTEGRATION=1 后由总控手动验收运行（需本机 Chrome 与代理）。
"""

import os
from pathlib import Path

import pytest

from phxsc.cdp import chrome as chrome_tools

pytestmark = pytest.mark.integration

if os.environ.get("PHXSC_RUN_INTEGRATION") != "1":
    pytest.skip(
        "集成测试默认跳过；设置 PHXSC_RUN_INTEGRATION=1 运行", allow_module_level=True
    )


class TestChromeLifecycle:
    def test_chrome_lifecycle(self):
        chrome = chrome_tools.find_chrome()
        assert chrome, "未找到 Chrome"
        port = chrome_tools.pick_free_port()
        proc = chrome_tools.start_chrome(
            "data:text/html,<title>phxsc-test</title>", port
        )
        user_data_dir = getattr(proc, "user_data_dir", "")
        try:
            targets = chrome_tools.list_targets(port)
            assert any(t.get("title") == "phxsc-test" for t in targets)
            assert proc.poll() is None
        finally:
            chrome_tools.stop_chrome(proc, user_data_dir)
        assert proc.poll() is not None
        if user_data_dir:
            assert not Path(user_data_dir).exists()


class TestSciHubRealFlow:
    def test_scihub_real_flow(self):
        """级1 真实链路：sci-net.xyz 直连免验证 → 下载 → 魔数 %PDF（总控验收时手动跑）。"""
        from phxsc.tools import scihub as scihub_tools

        doi = "10.1038/s41578-023-00582-w"
        out = scihub_tools.scihub_download.fn(doi=doi, timeout=120)
        assert isinstance(out, str)
        assert out.startswith("已下载")
        pdf = Path(scihub_tools._workdir()) / "papers" / "10.1038_s41578-023-00582-w.pdf"
        assert pdf.read_bytes()[:4] == b"%PDF"

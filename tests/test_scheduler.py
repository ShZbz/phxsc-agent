"""定时任务调度器测试：JobStore / cron 解析 / _run_job 落盘与去重 / 调度器注册。

全部用 tmp_path + fake/monkeypatch，不发真实网络请求、不启动真实后台线程。
"""

import pytest

from phxsc.scheduler.jobs import JobStore, SchedulerService, create_scheduler, parse_cron


def _fake_entries():
    return [
        {
            "arxiv_id": "2401.11111",
            "title": "Stability of Perovskite Solar Cells",
            "authors": ["Alice", "Bob"],
            "published": "2024-01-15T00:00:00Z",
            "summary": "We study long-term stability. The results are promising. Extra detail here.",
            "url": "https://arxiv.org/abs/2401.11111",
        }
    ]


def _read_daily(tmp_path) -> str:
    files = list((tmp_path / "notes" / "daily").glob("*.md"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


def _patch_search(monkeypatch, fn):
    monkeypatch.setattr("phxsc.scheduler.jobs.arxiv_search", fn)


class FakeScheduler:
    """记录 add/remove 的最小 fake，模拟 APScheduler 接口子集。"""

    def __init__(self):
        self.jobs = {}
        self.running = False

    def add_job(self, func, trigger, args=None, id=None, replace_existing=False, **kwargs):
        self.jobs[id] = {"func": func, "trigger": trigger, "args": args}

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)

    def start(self):
        self.running = True

    def shutdown(self, wait=False):
        self.running = False


# ---------- JobStore ----------


def test_jobstore_add_list(tmp_path):
    store = JobStore(str(tmp_path / "scheduler.db"))
    jid = store.add("perovskite", "0 9 * * *", "perovskite stability")
    rows = store.list()
    assert len(rows) == 1
    assert rows[0]["id"] == jid
    assert rows[0]["name"] == "perovskite"
    assert rows[0]["cron"] == "0 9 * * *"
    assert rows[0]["topic"] == "perovskite stability"
    assert rows[0]["enabled"] == 1
    assert rows[0]["last_run"] is None
    store.close()


def test_jobstore_remove(tmp_path):
    store = JobStore(str(tmp_path / "scheduler.db"))
    jid = store.add("a", "0 9 * * *", "t")
    assert store.remove(jid) is True
    assert store.list() == []
    store.close()


def test_jobstore_remove_missing_returns_false(tmp_path):
    store = JobStore(str(tmp_path / "scheduler.db"))
    assert store.remove(999) is False
    store.close()


def test_jobstore_set_last_run(tmp_path):
    store = JobStore(str(tmp_path / "scheduler.db"))
    jid = store.add("a", "0 9 * * *", "t")
    store.set_last_run(jid, "2026-08-10T09:00:00+00:00")
    assert store.list()[0]["last_run"] == "2026-08-10T09:00:00+00:00"
    store.close()


# ---------- cron 解析 ----------


def test_parse_cron_chinese_shorthand():
    assert parse_cron("每天 9:00") == "0 9 * * *"
    assert parse_cron("每天 08:05") == "5 8 * * *"


def test_parse_cron_crontab_passthrough():
    assert parse_cron("0 9 * * *") == "0 9 * * *"
    assert parse_cron("*/15 * * * *") == "*/15 * * * *"


def test_parse_cron_invalid_raises():
    with pytest.raises(ValueError):
        parse_cron("每天 25:00")
    with pytest.raises(ValueError):
        parse_cron("not a crontab")
    with pytest.raises(ValueError):
        parse_cron("")


# ---------- SchedulerService：注册逻辑（注入 fake scheduler） ----------


def test_service_start_registers_enabled_jobs(tmp_path):
    store = JobStore(str(tmp_path / "scheduler.db"))
    store.add("a", "0 9 * * *", "topic a")
    fake = FakeScheduler()
    svc = SchedulerService(store, tmp_path, scheduler=fake)
    svc.start()
    assert len(fake.jobs) == 1
    svc.stop()


def test_service_add_registers_scheduler_job(tmp_path):
    fake = FakeScheduler()
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path, scheduler=fake)
    svc.start()
    job_id = svc.add("p", "每天 9:00", "perovskite stability")
    assert str(job_id) in fake.jobs
    assert fake.jobs[str(job_id)]["args"][0]["topic"] == "perovskite stability"
    assert fake.jobs[str(job_id)]["args"][0]["cron"] == "0 9 * * *"
    svc.stop()


def test_service_add_invalid_cron_rejected(tmp_path):
    fake = FakeScheduler()
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path, scheduler=fake)
    svc.start()
    with pytest.raises(ValueError):
        svc.add("p", "bad cron", "perovskite")
    assert fake.jobs == {}
    assert svc.list() == []
    svc.stop()


def test_service_remove_removes_scheduler_job(tmp_path):
    fake = FakeScheduler()
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path, scheduler=fake)
    svc.start()
    job_id = svc.add("p", "0 9 * * *", "perovskite")
    assert str(job_id) in fake.jobs
    assert svc.remove(job_id) is True
    assert str(job_id) not in fake.jobs
    assert svc.remove(999) is False
    svc.stop()


# ---------- _run_job：落盘 + 去重 + 异常不崩溃 ----------


def test_run_job_writes_daily_note(tmp_path, monkeypatch):
    _patch_search(monkeypatch, lambda query, max_results=10: _fake_entries())
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path)
    job_id = svc.add("p", "0 9 * * *", "perovskite stability")
    job = svc.list()[0]
    svc._run_job(job)
    content = _read_daily(tmp_path)
    assert "Stability of Perovskite Solar Cells" in content
    assert "Alice, Bob" in content
    assert "2024" in content
    assert "https://arxiv.org/abs/2401.11111" in content
    assert svc.list()[0]["last_run"] is not None


def test_run_job_dedup_same_arxiv_id(tmp_path, monkeypatch):
    _patch_search(monkeypatch, lambda query, max_results=10: _fake_entries())
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path)
    svc.add("p", "0 9 * * *", "perovskite stability")
    job = svc.list()[0]
    svc._run_job(job)
    svc._run_job(job)
    content = _read_daily(tmp_path)
    assert content.count("Stability of Perovskite Solar Cells") == 1


def test_run_job_handles_search_exception(tmp_path, monkeypatch):
    def boom(query, max_results=10):
        raise RuntimeError("network down")

    _patch_search(monkeypatch, boom)
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path)
    svc.add("p", "0 9 * * *", "perovskite stability")
    job = svc.list()[0]
    svc._run_job(job)  # 绝不能抛异常炸掉调度器线程
    content = _read_daily(tmp_path)
    assert "失败" in content
    assert "RuntimeError" in content


def test_run_job_handles_search_error_dict(tmp_path, monkeypatch):
    _patch_search(
        monkeypatch,
        lambda query, max_results=10: {"error": "arXiv 网络请求失败：boom", "reason": "URLError"},
    )
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path)
    svc.add("p", "0 9 * * *", "perovskite stability")
    job = svc.list()[0]
    svc._run_job(job)
    content = _read_daily(tmp_path)
    assert "失败" in content


# ---------- 异常逃逸（batch2 #12）：daily 路径被目录占位时绝不向调度器线程抛异常 ----------


def test_run_job_daily_dir_placeholder_no_raise(tmp_path, monkeypatch):
    """daily 文件位置被目录占位：_run_job 不抛异常且 last_run 已更新。"""
    _patch_search(monkeypatch, lambda query, max_results=10: _fake_entries())
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path)
    svc.add("p", "0 9 * * *", "perovskite stability")
    job = svc.list()[0]
    (tmp_path / "notes" / "daily" / f"{SchedulerService._today()}.md").mkdir(parents=True)
    svc._run_job(job)  # 修复前：_append_failure 的 OSError 逃逸到测试
    assert svc.list()[0]["last_run"] is not None


def test_append_failure_dir_placeholder_no_raise(tmp_path):
    """"_append_failure 单测：落盘失败只吞掉，不抛。"""
    svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path)
    job = {"id": 1, "name": "p", "cron": "0 9 * * *", "topic": "perovskite stability"}
    (tmp_path / "notes" / "daily" / f"{SchedulerService._today()}.md").mkdir(parents=True)
    svc._append_failure(job, "boom")  # 修复前：IsADirectoryError 逃逸


# ---------- create_scheduler ----------


def test_create_scheduler(tmp_path):
    svc = create_scheduler(tmp_path / "scheduler.db", tmp_path)
    assert isinstance(svc, SchedulerService)
    svc.stop()


# ---------- 跨线程安全（回归：APScheduler 后台线程调用 set_last_run） ----------


def test_jobstore_cross_thread_access(tmp_path):
    """JobStore 连接必须能跨线程使用（APScheduler 后台线程会调 set_last_run）。"""
    import threading

    store = JobStore(str(tmp_path / "scheduler.db"))
    store.add("t", "0 9 * * *", "perovskite")

    errors = []

    def worker():
        try:
            store.set_last_run(1, "2026-08-10T00:00:00+00:00")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == [], f"跨线程访问失败: {errors}"
    assert store.list()[0]["last_run"] == "2026-08-10T00:00:00+00:00"

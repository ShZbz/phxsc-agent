"""APScheduler 定时任务：/schedule 命令的存储与调度服务。

cron 表达式支持两种格式：
- 标准五段 crontab，如 "0 9 * * *"（由 CronTrigger.from_crontab 解析并校验）
- 中文简写 "每天 HH:MM"，映射为 "MM HH * * *"（目前仅支持这一种简写）

jobs 存 SQLite（独立 scheduler.db，与 memory.db 同目录）；每次触发按 topic
检索 arXiv 新论文，把摘要追加写入 <workdir>/notes/daily/YYYY-MM-DD.md。
调度器线程内的执行体 _run_job 全程 try/except，任何失败只落盘标注，不向
APScheduler 线程抛异常（否则任务失败无感知）。
"""

import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from phxsc.sandbox.paths import safe_write_path
from phxsc.tools.arxiv import arxiv_search

DAILY_DIR = "notes/daily"
MAX_RESULTS = 5
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_DAILY_SHORTHAND = re.compile(r"^每天\s*(\d{1,2}):(\d{2})$")


def _now() -> str:
    """ISO8601 时间戳（UTC），风格与 memory/store.py 一致。"""
    return datetime.now(timezone.utc).isoformat()


def parse_cron(expr: str) -> str:
    """把 cron 表达式归一化为标准五段 crontab；非法输入 raise ValueError。

    中文简写只支持 "每天 HH:MM" 一种；其余格式一律按 crontab 解析校验。
    """
    expr = expr.strip()
    match = _DAILY_SHORTHAND.match(expr)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"非法时间：{expr!r}（小时 ≤ 23，分钟 ≤ 59）")
        return f"{minute} {hour} * * *"
    try:
        CronTrigger.from_crontab(expr)
    except ValueError as exc:
        raise ValueError(f"非法 cron 表达式：{expr!r}（{exc}）") from exc
    return expr


class JobStore:
    """定时任务的 SQLite 存储（独立 scheduler.db）。"""

    def __init__(self, db_path: str) -> None:
        # check_same_thread=False：APScheduler 后台线程会调用 set_last_run，
        # 连接必须可跨线程；用 _lock 串行化所有访问（SQLite 连接非线程安全）
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        """建表（幂等）。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT,
                        cron TEXT,
                        topic TEXT,
                        enabled INTEGER DEFAULT 1,
                        last_run TEXT NULL,
                        created_at TEXT
                    )"""
                )

    def add(self, name: str, cron: str, topic: str) -> int:
        """新增一条定时任务，返回自增 id（默认 enabled=1）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (name, cron, topic, enabled, last_run, created_at)"
                " VALUES (?, ?, ?, 1, NULL, ?)",
                (name, cron, topic, _now()),
            )
            self._conn.commit()
            return cur.lastrowid

    def list(self) -> list[dict]:
        """列出全部任务（按 id 升序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, cron, topic, enabled, last_run, created_at"
                " FROM jobs ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def remove(self, job_id: int) -> bool:
        """删除任务；id 不存在返回 False。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def set_last_run(self, job_id: int, ts: str) -> None:
        """记录最近一次执行时间。"""
        with self._lock:
            self._conn.execute("UPDATE jobs SET last_run = ? WHERE id = ?", (ts, job_id))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SchedulerService:
    """JobStore + APScheduler 的封装：start/add/remove/stop/_run_job。

    scheduler 参数用于测试注入 fake；为 None 时 start() 创建 BackgroundScheduler。
    """

    def __init__(self, store: JobStore, workdir: Path, scheduler=None) -> None:
        self._store = store
        self._workdir = Path(workdir)
        self._scheduler = scheduler

    def start(self) -> None:
        """启动调度器，并为每个 enabled 任务注册 cron trigger。"""
        if self._scheduler is None:
            self._scheduler = BackgroundScheduler()
        for job in self._store.list():
            if job["enabled"]:
                self._add_job_to_scheduler(job)
        if not self._scheduler.running:
            self._scheduler.start()

    def add(self, name: str, cron: str, topic: str) -> int:
        """新增任务：先解析校验 cron，成功后入库并注册到调度器。"""
        cron_expr = parse_cron(cron)
        job_id = self._store.add(name, cron_expr, topic)
        job = {
            "id": job_id,
            "name": name,
            "cron": cron_expr,
            "topic": topic,
            "enabled": 1,
            "last_run": None,
            "created_at": _now(),
        }
        if self._scheduler is not None:
            self._add_job_to_scheduler(job)
        return job_id

    def remove(self, job_id: int) -> bool:
        """从库和调度器同时移除；id 不存在返回 False。"""
        removed = self._store.remove(job_id)
        if removed and self._scheduler is not None:
            try:
                self._scheduler.remove_job(str(job_id))
            except JobLookupError:
                pass
        return removed

    def list(self) -> list[dict]:
        return self._store.list()

    def stop(self) -> None:
        """停止调度器线程。"""
        if self._scheduler is not None and getattr(self._scheduler, "running", False):
            self._scheduler.shutdown(wait=False)
        self._scheduler = None

    def _add_job_to_scheduler(self, job: dict) -> None:
        self._scheduler.add_job(
            self._run_job,
            CronTrigger.from_crontab(job["cron"]),
            args=[job],
            id=str(job["id"]),
            replace_existing=True,
        )

    def _run_job(self, job: dict) -> None:
        """定时任务执行体：检索 arXiv → 摘要追加到 daily 笔记。

        全程 try/except：检索/落盘失败只写入失败说明，绝不向调度器线程抛异常。
        """
        try:
            entries = arxiv_search(job["topic"], max_results=MAX_RESULTS)
            if isinstance(entries, dict) and "error" in entries:
                self._append_failure(job, entries["error"])
            else:
                self._append_results(job, entries)
        except Exception as exc:
            self._append_failure(job, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                self._store.set_last_run(job["id"], _now())
            except Exception:
                pass

    def _append_results(self, job: dict, entries: list) -> None:
        """把检索结果整理为 Markdown 追加进 daily 文件；同 arxiv id 去重。"""
        try:
            daily = self._daily_path()
            existing = self._read_daily(daily)
            blocks = []
            for entry in entries:
                arxiv_id = entry.get("arxiv_id", "")
                if arxiv_id and arxiv_id in existing:
                    continue
                blocks.append(self._format_entry(entry))
            if not blocks:
                return
            content = self._daily_header(job)
            if existing:
                content = "\n" + "\n".join(blocks) + "\n"
            else:
                content += "\n".join(blocks) + "\n"
            self._write_daily(daily, content, append=bool(existing))
        except Exception:
            pass  # 放弃落盘不抛（batch2 #12）：绝不向调度器线程抛异常

    def _append_failure(self, job: dict, reason: str) -> None:
        """把失败原因标注进 daily 文件（不抛异常）。"""
        try:
            daily = self._daily_path()
            existing = self._read_daily(daily)
            note = f"⚠️ 本次抓取失败：{reason}\n"
            content = self._daily_header(job) if not existing else "\n" + note
            if not existing:
                content += note
            self._write_daily(daily, content, append=bool(existing))
        except Exception:
            pass  # 放弃落盘不抛（batch2 #12）：绝不向调度器线程抛异常

    def _daily_header(self, job: dict) -> str:
        return f"# 每日 arXiv 速报（{self._today()}）\n\n## 主题：{job['topic']}\n\n"

    @staticmethod
    def _format_entry(entry: dict) -> str:
        title = entry.get("title", "未命名论文")
        authors = ", ".join(entry.get("authors") or [])[:120] or "未知"
        year = entry.get("published", "")[:4] or "未知"
        summary = entry.get("summary", "").strip()
        sentences = _SENTENCE_SPLIT.split(summary)
        lead = " ".join(sentences[:2]) or "（无摘要）"
        return "\n".join(
            [
                f"### {title}",
                f"- 作者：{authors}",
                f"- 年份：{year}",
                f"- 摘要：{lead}",
                f"- 链接：{entry.get('url', '')}",
                "",
            ]
        )

    def _daily_path(self) -> Path:
        """daily 笔记绝对路径（过沙箱校验，仅允许 workdir/notes/daily/ 内）。"""
        rel = f"{DAILY_DIR}/{self._today()}.md"
        return Path(safe_write_path(rel, str(self._workdir)))

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _read_daily(daily: Path) -> str:
        if not os.path.isfile(daily):
            return ""
        with open(daily, "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _write_daily(daily: Path, content: str, append: bool) -> None:
        os.makedirs(daily.parent, exist_ok=True)
        mode = "a" if append else "w"
        with open(daily, mode, encoding="utf-8") as f:
            f.write(content)


def create_scheduler(db_path: Path, workdir: Path) -> SchedulerService:
    """模块级工厂：建 JobStore + SchedulerService。"""
    return SchedulerService(JobStore(str(db_path)), workdir)

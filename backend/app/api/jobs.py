"""后台任务的登记表：导入是分钟级的耗时活，接口先回执 job_id，前端轮询看进度。

并发策略（两条结论都是本机实测出来的，2026-10）：
- LanceDB 在同一进程里多线程写**不同数据源**是安全的：8 个线程各写 300 行，
  0 报错、总数正确。所以不搞「全局串行」，多个目录可以同时导入。
- 但**同一个 source_id** 被两路「先删旧块 → 再写新块」会留下两代分块（实测同一个源
  变成 400 块 = 两份内容并存，正是 app/core/ingest.py 注释里要避免的情况）。
  所以按 source_id 去重：同一个目录已经在跑或排队时直接拒绝，而不是让它并发写两次。

规则：
- 最多 `limit` 个任务同时在跑，超出的排队（state=queued），接口照样立刻回 job_id；
- 任务只存在内存里，进程重启即消失（自用场景够用，也好排查）。
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable

from app.core.schema import utc_now


class DuplicateError(RuntimeError):
    """同一个数据源已经在跑或排队了。"""


@dataclass
class Job:
    """一个后台任务。`result` 由具体任务自己填（导入任务放 IngestResult 的关键字段）。"""

    id: str
    kind: str
    state: str = "queued"  # queued / running / done / failed
    total: int = 0
    done: int = 0
    message: str = ""
    error: str = ""
    result: dict | None = None
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "message": self.message,
            "error": self.error,
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobRegistry:
    """任务的创建/查询，外加「最多同时跑几个」的调度。线程安全：进度回调会从工作线程里改 Job。"""

    def __init__(self, *, limit: int = 3) -> None:
        self.limit = max(1, int(limit))  # 至少 1，免得配成 0 之后什么都不跑
        self._jobs: dict[str, Job] = {}
        self._queue: list[tuple[Job, Callable[[Job], dict | None], str | None]] = []
        self._active: dict[str, str] = {}  # 去重键（数据源 id）→ job_id，任务结束就腾出来
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(
        self,
        kind: str,
        work: Callable[[Job], dict | None],
        *,
        dedup_key: str | None = None,
    ) -> Job:
        """登记一个任务：有空位就立刻开跑，否则排队。

        `dedup_key` 是「同一个东西只允许跑一个」的标识（导入任务传数据源 id）；
        已经在跑或排队时抛 DuplicateError。
        """
        with self._lock:
            if dedup_key and dedup_key in self._active:
                other = self._jobs.get(self._active[dedup_key])
                where = f"状态 {other.state}，任务 {other.id}" if other else "进行中"
                raise DuplicateError(f"这个数据源已经在导入队列里了（{where}）")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind)
            self._jobs[job.id] = job
            if dedup_key:
                self._active[dedup_key] = job.id
            self._queue.append((job, work, dedup_key))
            self._pump_locked()
        return job

    def _running_locked(self) -> int:
        """正在跑的任务数。直接从状态里数：比维护计数器少一处可能对不上的地方。"""
        return sum(1 for job in self._jobs.values() if job.state == "running")

    def _pump_locked(self) -> None:
        """把排队里的任务顶上去，直到跑满 limit。调用方必须持有 self._lock。"""
        while self._queue and self._running_locked() < self.limit:
            job, work, dedup_key = self._queue.pop(0)
            job.state = "running"
            threading.Thread(
                target=self._run,
                args=(job, work, dedup_key),
                name=f"job-{job.kind}-{job.id}",
                daemon=True,
            ).start()

    def _run(self, job: Job, work: Callable[[Job], dict | None], dedup_key: str | None) -> None:
        try:
            job.result = work(job) or None
            job.state = "done"
            job.message = job.message or "完成"
        except Exception as exc:  # 任何异常都记进任务里，别让线程静默死掉
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = "失败"
            traceback.print_exc()
        finally:
            job.finished_at = utc_now()
            with self._lock:
                if dedup_key:
                    self._active.pop(dedup_key, None)
                self._pump_locked()  # 腾出位置，排队里的下一个顶上

    def progress(self, job: Job) -> Callable[[int, int, str], None]:
        """给耗时任务用的进度回调：写进 Job，前端轮询即可看到。"""

        def update(done: int, total: int, message: str) -> None:
            job.done = done
            job.total = total
            job.message = message

        return update

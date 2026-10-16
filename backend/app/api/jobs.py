"""后台任务的登记表：导入是分钟级的耗时活，接口先回执 job_id，前端轮询看进度。

规则（刻意保守）：
- 同一时刻只允许一个任务在跑。导入会写 LanceDB，也会打 OpenRouter，串行最省心；
- 任务只存在内存里，进程重启即消失（自用场景够用，也好排查）。
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable

from app.core.schema import utc_now


class BusyError(RuntimeError):
    """已经有任务在跑了。"""


@dataclass
class Job:
    """一个后台任务。`result` 由具体任务自己填（导入任务放 IngestResult 的关键字段）。"""

    id: str
    kind: str
    state: str = "running"  # running / done / failed
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
    """任务的创建/查询。线程安全：进度回调会从工作线程里改 Job。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._running: str | None = None

    def running_job(self) -> Job | None:
        with self._lock:
            return self._jobs.get(self._running) if self._running else None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, kind: str, work: Callable[[Job], dict | None]) -> Job:
        """开一个后台线程跑 `work`；已有任务在跑时抛 BusyError。"""
        with self._lock:
            if self._running and self._jobs[self._running].state == "running":
                raise BusyError(f"已有任务在跑：{self._running}")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind)
            self._jobs[job.id] = job
            self._running = job.id

        def run() -> None:
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
                    self._running = None

        threading.Thread(target=run, name=f"job-{kind}-{job.id}", daemon=True).start()
        return job

    def progress(self, job: Job) -> Callable[[int, int, str], None]:
        """给耗时任务用的进度回调：写进 Job，前端轮询即可看到。"""

        def update(done: int, total: int, message: str) -> None:
            job.done = done
            job.total = total
            job.message = message

        return update

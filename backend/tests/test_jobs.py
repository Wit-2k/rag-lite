"""任务调度（并发上限 + 排队 + 按数据源去重）的测试。

    cd backend && uv run pytest -q tests/test_jobs.py

这些是纯内存的逻辑测试：不开 HTTP、不碰向量库，用 threading.Event 把任务「卡住」，
好观察「谁在跑、谁在排队」。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.api.jobs import DuplicateError, JobRegistry


def wait_until(predicate, *, timeout: float = 5.0) -> bool:
    """等某个条件成立（调度是后台线程干的，得给它一点时间）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def states(registry: JobRegistry, job_ids: list[str]) -> list[str]:
    return [registry.get(job_id).state for job_id in job_ids]


def test_runs_up_to_limit_and_queues_the_rest() -> None:
    """上限 2：前两个立刻在跑，后两个排队；放行后队列会自己顶上。"""
    gate = threading.Event()
    registry = JobRegistry(limit=2)
    started: list[str] = []

    def work(job):
        started.append(job.id)
        gate.wait(timeout=5)
        return {"ok": True}

    jobs = [registry.start("ingest", work) for _ in range(4)]
    ids = [job.id for job in jobs]

    assert wait_until(lambda: len(started) == 2), "应有两个任务开跑"
    assert states(registry, ids) == ["running", "running", "queued", "queued"]

    gate.set()
    assert wait_until(lambda: all(state == "done" for state in states(registry, ids)))
    assert len(started) == 4, "排队的两个最后都要跑到"


def test_queue_drains_one_slot_at_a_time() -> None:
    """上限 1：任务严格一个接一个，不会因为释放一次就同时开跑。"""
    registry = JobRegistry(limit=1)
    order: list[str] = []
    first_gate = threading.Event()

    def first(job):
        order.append("第一")
        first_gate.wait(timeout=5)
        return None

    def rest(job):
        order.append("后")
        return None

    a = registry.start("ingest", first)
    b = registry.start("ingest", rest)
    c = registry.start("ingest", rest)

    assert wait_until(lambda: a.state == "running")
    assert (b.state, c.state) == ("queued", "queued")

    first_gate.set()
    assert wait_until(lambda: states(registry, [a.id, b.id, c.id]) == ["done", "done", "done"])
    assert order == ["第一", "后", "后"]


def test_duplicate_key_rejected_while_active() -> None:
    """同一个数据源在跑或排队时再来一次 → DuplicateError；跑完之后可以再导入。"""
    gate = threading.Event()
    registry = JobRegistry(limit=1)

    def work(job):
        gate.wait(timeout=5)
        return None

    running = registry.start("ingest", work, dedup_key="src_x")
    queued = registry.start("ingest", work, dedup_key="src_y")  # 占住队列，制造「排队中被拒」

    with pytest.raises(DuplicateError):
        registry.start("ingest", work, dedup_key="src_x")  # 正在跑
    with pytest.raises(DuplicateError):
        registry.start("ingest", work, dedup_key="src_y")  # 还在排队

    assert wait_until(lambda: running.state == "running" and queued.state == "queued")

    gate.set()
    assert wait_until(lambda: queued.state == "done")
    # 腾出来之后，同一个键可以重新导入（比如刚改完配置要重跑）
    again = registry.start("ingest", lambda job: None, dedup_key="src_y")
    assert wait_until(lambda: again.state == "done")


def test_failure_frees_slot_and_key() -> None:
    """任务抛异常：状态是 failed、错误记在任务里，位置与去重键都要腾出来。"""
    gate = threading.Event()
    registry = JobRegistry(limit=1)

    def boom(job):
        gate.wait(timeout=5)
        raise RuntimeError("接口挂了")

    def ok(job):
        return {"fine": True}

    bad = registry.start("ingest", boom, dedup_key="src_bad")
    nxt = registry.start("ingest", ok, dedup_key="src_next")
    assert nxt.state == "queued"

    gate.set()
    assert wait_until(lambda: nxt.state == "done"), "前一个失败不该把队列堵死"
    assert bad.state == "failed"
    assert "接口挂了" in bad.error
    assert bad.finished_at, "失败也要记结束时间"

    # 失败的那个键不再占位
    retry = registry.start("ingest", ok, dedup_key="src_bad")
    assert wait_until(lambda: retry.state == "done")


def test_progress_callback_writes_to_job() -> None:
    """进度回调把 (已完成, 总数, 阶段) 写进任务，前端轮询就能看到。"""
    registry = JobRegistry(limit=1)
    seen: list[tuple[int, int, str]] = []

    def work(job):
        update = registry.progress(job)
        update(3, 10, "向量化")
        seen.append((job.done, job.total, job.message))
        return None

    job = registry.start("ingest", work)
    assert wait_until(lambda: job.state == "done")
    assert seen == [(3, 10, "向量化")]

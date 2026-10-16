"""后端看门狗（`--watch-parent`）的测试：界面被硬杀时后端要能自己退出。

    cd backend && uv run pytest -q tests/test_parent_watch.py

会真起一个后端子进程（不联网、临时数据目录、随机端口），所以比其余测试慢几秒。
判据只用「进程有没有退出」，不依赖它的输出格式。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
HANDSHAKE = "RAGLITE_READY"


def start_backend(data_dir: Path, *, watch_parent: bool) -> subprocess.Popen[str]:
    """按界面的方式起后端：stdin 接在管道上（界面就是这么干的）。"""
    args = [sys.executable, "main.py", "--port", "0", "--data-dir", str(data_dir)]
    if watch_parent:
        args.append("--watch-parent")
    process = subprocess.Popen(
        args,
        cwd=BACKEND_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    for _ in range(200):  # 等握手行，最多 20 秒
        line = process.stdout.readline()  # type: ignore[union-attr]
        if line.startswith(HANDSHAKE):
            json.loads(line[len(HANDSHAKE):])  # 握手行必须是合法 JSON，顺便验一下
            return process
        if process.poll() is not None:
            pytest.fail(f"后端没起来就退出了（退出码 {process.returncode}）")
    pytest.fail("等后端握手超时")


def stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)


def test_watch_parent_exits_when_stdin_closes(tmp_path: Path) -> None:
    """带 --watch-parent：stdin（界面的那根管道）一关，后端自己退出。"""
    process = start_backend(tmp_path, watch_parent=True)
    try:
        process.stdin.close()  # 等价于界面进程死掉：它那头的写端关闭
        assert process.wait(timeout=15) == 0, "后端应该在父进程消失后自己退出"
    finally:
        stop(process)


def test_without_flag_keeps_running(tmp_path: Path) -> None:
    """不带 --watch-parent：stdin 关了也继续跑（手动 `uv run python main.py` 不能被误杀）。"""
    process = start_backend(tmp_path, watch_parent=False)
    try:
        process.stdin.close()
        time.sleep(3)
        assert process.poll() is None, "没带参数时后端不该因为 stdin 关闭而退出"
    finally:
        stop(process)

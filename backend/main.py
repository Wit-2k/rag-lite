"""后端入口。

    cd backend && uv run python main.py                # 默认 127.0.0.1:17650
    uv run python main.py --port 0                     # 自动挑一个空闲端口
    uv run python main.py --data-dir D:/raglite-data    # 换数据目录（便携版用得上）

启动后会在 stdout 打印一行握手，前端读到它就知道后端起来了、该连哪个端口：
    RAGLITE_READY {"host": "127.0.0.1", "port": 17650}
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

import uvicorn

from app.api.server import create_app
from app.core.store import StoreError
from app.config import load_settings

HANDSHAKE_PREFIX = "RAGLITE_READY"


def free_port(host: str, start: int, *, tries: int = 50) -> int:
    """从 start 往后找一个能绑上的端口；被占用就顺延，全占满才报错。

    注意：探测用的 socket **不能**设 SO_REUSEADDR——Windows 上设了以后，
    连已经被占用的端口也能绑上，探测就完全失效了（实测踩过）。
    """
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"{host} 上 {start}~{start + tries} 都没有空端口")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rag-lite 后端服务")
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，只对本机开放）")
    parser.add_argument("--port", type=int, default=None, help="监听端口；传 0 表示自动挑一个空闲端口")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 backend/data）")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别")
    args = parser.parse_args(argv)

    overrides: dict[str, object] = {"host": args.host}
    if args.port:  # 0 也走自动挑选
        overrides["port"] = args.port
    if args.data_dir:
        data_dir = Path(args.data_dir)
        overrides["data_dir"] = data_dir
        overrides["db_uri"] = str(data_dir / "lancedb")
    settings = load_settings(**overrides)  # overrides 里的 None 会被忽略

    host = settings.host
    port = free_port(host, settings.port or 17650)

    def announce() -> None:
        # 端口真正绑定成功之后才打印，前端不会抢跑
        print(f"{HANDSHAKE_PREFIX} {json.dumps({'host': host, 'port': port})}", flush=True)

    print(f"数据目录：{settings.data_dir}；向量库：{settings.db_uri}", flush=True)
    print(
        f"已配置密钥：{'是' if settings.configured else '否（导入/问答会报错，请在 backend/.env 填 OPENROUTER_API_KEY）'}",
        flush=True,
    )
    try:
        app = create_app(settings=settings, on_startup=announce)
    except StoreError as exc:
        # 配置类问题（比如换了维度没重建库）直接给一句人能看懂的提示，别甩 traceback
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

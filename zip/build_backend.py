"""用 PyInstaller 把后端打成免 Python 环境的 onedir 目录。

    cd backend && uv run python ../zip/build_backend.py

产物：zip/dist_backend/raglite-backend/{raglite-backend.exe, _internal/}
打包脚本（zip/package.ps1）会把它放进便携包的 backend/ 下。

为什么要 --collect-all：chonkie 带 Rust 扩展（chonkie_core）、lancedb 带 lance_namespace、
pyarrow 有一堆二进制，PyInstaller 的静态分析看不全，宁可多收一点。
环境要求：Python 3.14 → PyInstaller >= 6.15（本项目用 6.22）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
ZIP_DIR = Path(__file__).resolve().parent
DIST_DIR = ZIP_DIR / "dist_backend"
WORK_DIR = ZIP_DIR / "build_backend"
NAME = "raglite-backend"

# 静态分析看不全、需要整包收进来的依赖
COLLECT_ALL = ["chonkie", "chonkie_core", "lancedb", "lance_namespace", "pyarrow"]
# 只在运行时按名字动态导入的子模块
COLLECT_SUBMODULES = ["uvicorn"]
# 运行时用不到的模块：各包自带的测试（含样例数据），排掉能少带几 MB
EXCLUDE_MODULES = ["pyarrow.tests", "lance_namespace.tests"]


def main() -> int:
    for stale in (DIST_DIR / NAME, WORK_DIR):
        shutil.rmtree(stale, ignore_errors=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",  # 不用 onefile：启动快，也免得每次解包
        "--console",  # 保留控制台：握手行要打 stdout，uvicorn 日志也要能看到
        "--name",
        NAME,
        "--paths",
        str(BACKEND_DIR),
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR),
        "--specpath",
        str(ZIP_DIR),
    ]
    for package in COLLECT_ALL:
        command += ["--collect-all", package]
    for module in COLLECT_SUBMODULES:
        command += ["--collect-submodules", module]
    for module in EXCLUDE_MODULES:
        command += ["--exclude-module", module]
    command.append(str(BACKEND_DIR / "main.py"))

    print("执行：", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=BACKEND_DIR)
    if result.returncode != 0:
        return result.returncode

    # --exclude-module 对 --collect-all 收进来的文件不一定生效，顺手清掉漏网的测试目录，
    # 免得体积随依赖升级悄悄长回去
    for leftover in (DIST_DIR / NAME / "_internal").rglob("tests"):
        if leftover.is_dir() and leftover.parent.name in {"pyarrow", "lance_namespace", "numpy"}:
            shutil.rmtree(leftover, ignore_errors=True)
            print(f"清掉漏网的测试目录：{leftover.relative_to(DIST_DIR)}")

    exe = DIST_DIR / NAME / f"{NAME}.exe"
    size = sum(item.stat().st_size for item in (DIST_DIR / NAME).rglob("*") if item.is_file())
    print(f"\n产物：{exe}")
    print(f"体积：{size / 1048576:.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

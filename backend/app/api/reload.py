"""`.env` 热重载：手工改过配置文件就重新读一遍，不用重启后端。

为什么需要它：后端启动时只读一次 `.env`（`main.py` 里 `load_settings`），之后再改文件也不会重新读。
于是「第一次跑还没有 `.env` → 添加目录报『还没有配置密钥』→ 手工补上 `.env` → 再添加还是报同一句」
——包内 `使用说明.txt` 里承诺过「手工改那个文件也一样管用」，所以这是 bug，不是「请重启」就能打发的。

两个原则（都踩过坑）：
1. **只覆盖文件/环境变量里确实写了的那几项**，其余保持当前值不动。
   一开始写成「按当前 root 重新 load_settings 一遍」，结果把不在文件里的值全丢了
   （命令行传进来的、测试里注入的共享密钥都会凭空消失）；测试立刻抓出来了。
2. **监听地址/端口/库位置固定不改**：进程已经绑好端口、开好库了，改这几项没有意义，
   与计划里「root/data_dir/db_uri 属于命令行/文件的活、改完重启」的口径也一致。

判断依据是 `.env` 的 (mtime, size)：没变就直接返回，每个请求只多一次 stat。
界面里改配置走的是 `PUT /api/settings`，那条路本来就会立刻写文件并更新内存，与这里互不冲突。
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI

from app.config import ENV_NAMES, INT_FIELDS, env_file_path, read_env_file
from app.core.store import Store, StoreError

# 用来记住「上次读的是哪个版本的 .env」；文件不存在时是 None
Stamp = tuple[int, int] | None

# 这几项在进程生命周期内固定：端口已经绑好、库已经打开，热改没有意义（见模块开头的说明）
PINNED_FIELDS = frozenset({"host", "port", "data_dir", "db_uri"})


def env_stamp(root: Path) -> Stamp:
    """`.env` 的版本戳：mtime 纳秒 + 大小。文件不存在返回 None。"""
    try:
        stat = env_file_path(root).stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def env_overlay(env_file: Path) -> dict[str, object]:
    """把「`.env` 或环境变量里确实写了的」配置项挑出来，转成 Settings 的字段名。

    没写的项**不进这个字典**，调用方就不会拿默认值把它们顶掉。环境变量优先于文件（与 read_env_value 一致）。
    """
    raw = dict(read_env_file(env_file))
    for env_name in ENV_NAMES.values():
        value = os.environ.get(env_name)
        if value:
            raw[env_name] = value

    overlay: dict[str, object] = {}
    for field, env_name in ENV_NAMES.items():
        if field in PINNED_FIELDS or env_name not in raw:
            continue
        value = raw[env_name]
        if value == "":
            continue
        if field in INT_FIELDS:
            try:
                overlay[field] = int(value)
            except ValueError:
                print(f"[配置] {env_name}={value!r} 不是数字，本次忽略这一项", flush=True)
            continue
        overlay[field] = value
    return overlay


def maybe_reload_settings(app: FastAPI) -> bool:
    """`.env` 变了就重新读一遍并替换 app.state.settings；返回是否真的重载了。"""
    current = app.state.settings
    stamp = env_stamp(current.root)
    if stamp == app.state.env_stamp:
        return False
    app.state.env_stamp = stamp
    if stamp is None:
        # 文件被删了：保留当前配置，别把正在用的密钥「忘掉」（下次启动自然就没了）
        print(f"[配置] {env_file_path(current.root)} 不在了，本次继续用已加载的配置", flush=True)
        return False

    reloaded = replace(current, **env_overlay(env_file_path(current.root)))  # type: ignore[arg-type]
    if reloaded == current:
        return False

    # 维度写进了向量库 schema：改了维度得先能按新维度打开库，否则只忽略这一项
    # （与 PUT /api/settings 的处理保持一致，免得出现「配置说 1024 维、库里是 2048 维」的错位）
    if reloaded.embed_dim != app.state.store.dim:
        try:
            app.state.store = Store(reloaded.db_uri, dim=reloaded.embed_dim)
        except StoreError as exc:
            print(f"[配置] .env 里的 EMBED_DIM={reloaded.embed_dim} 与现有库不符，本次忽略这一项：{exc}", flush=True)
            reloaded = replace(reloaded, embed_dim=current.embed_dim)

    app.state.settings = reloaded
    app.state.chat = None  # 下次提问按新配置重建会话（模型/密钥可能都换了）
    print(f"[配置] 检测到 {env_file_path(current.root)} 有改动，已重新加载（不必重启）", flush=True)
    return True

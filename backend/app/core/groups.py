"""数据源分组的持久化（多对多）：一个数据源可以同时放进多个分组。

为什么放在会话那个 SQLite 文件里（`data/sessions.db`）：
- 会话要「绑定一个分组当检索范围」，这条绑定关系就存在会话表上（`sessions.group_id`）；
  分组本身与成员关系跟它在同一个库里，删分组前「有没有会话在用」就能在一个事务里问清楚。
- 向量库（LanceDB）的 sources 表刻意不掺这件事：给已有表加列要重写表、还会撞上 schema 校验，
  而分组纯属「本机的使用状态」，与向量数据无关。

（文件名叫 sessions.db 没改：改名会丢掉已有会话，不值当。）
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from app.core.schema import utc_now

NAME_MAX_CHARS = 40
# 界面上「全部数据源」与「未分组」是两个虚拟条目；真实分组若重名，用户就分不清哪个是哪个了
RESERVED_NAMES = ("全部数据源", "未分组")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_groups (
    group_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_sources (
    group_id  TEXT NOT NULL,
    source_id TEXT NOT NULL,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (group_id, source_id)   -- 同一个源在同一组里重复加不会变成两行
);

CREATE INDEX IF NOT EXISTS idx_group_sources_source ON group_sources(source_id);
"""


@dataclass(frozen=True, slots=True)
class GroupResult:
    """一次分组写操作的结果。

    `ok=False` 时看 `reason`：
    - `missing`：分组不存在
    - `duplicate`：名字已被别的分组用了
    - `reserved`：名字是保留名（全部数据源 / 未分组）或为空 / 过长
    - `unknown`：要放进的分组不存在（`unknown` 里是那些 id）
    - `in_use`：还有会话在用这个分组（`blocking` 里是那些会话）
    """

    ok: bool
    reason: str = ""
    group: dict | None = None
    group_ids: tuple[str, ...] = ()
    blocking: tuple[dict, ...] = ()
    unknown: tuple[str, ...] = ()


def new_group_id() -> str:
    """分组 id：`grp_` + 12 位十六进制，与数据源 / 会话 id 一个风格。"""
    return f"grp_{uuid.uuid4().hex[:12]}"


class GroupStore:
    """分组表 + 成员表的薄封装（每次调用开短连接，与 SessionStore 同一套做法）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()  # 护「先查再写」那几处（重名检查、删除前查引用）
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # 读写互不阻塞
            yield conn
            conn.commit()
        finally:
            conn.close()

    # —— 内部小工具 ————————————————————————————————————————————

    @staticmethod
    def _clean_name(name: str) -> str | None:
        """去掉首尾空白后校验名字；不合格返回 None（空、过长、保留名）。"""
        cleaned = (name or "").strip()
        if not cleaned or len(cleaned) > NAME_MAX_CHARS or cleaned in RESERVED_NAMES:
            return None
        return cleaned

    @staticmethod
    def _row(conn: sqlite3.Connection, group_id: str) -> dict | None:
        """单条分组（带成员数），给 create / rename 的返回值和 404 判断用。"""
        row = conn.execute(
            """
            SELECT g.group_id, g.name, g.created_at, COUNT(gs.source_id) AS source_count
            FROM source_groups g
            LEFT JOIN group_sources gs ON gs.group_id = g.group_id
            WHERE g.group_id = ?
            GROUP BY g.group_id
            """,
            (group_id,),
        ).fetchone()
        return dict(row) if row else None

    def _name_taken(self, conn: sqlite3.Connection, name: str, *, exclude: str | None = None) -> bool:
        row = conn.execute("SELECT group_id FROM source_groups WHERE name = ?", (name,)).fetchone()
        return row is not None and row["group_id"] != exclude

    # —— 分组 ————————————————————————————————————————————————

    def list(self) -> list[dict]:
        """列出全部分组，带成员数（界面下拉、分组管理都用它）。按名字排，跟数据源列表一致。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT g.group_id, g.name, g.created_at, COUNT(gs.source_id) AS source_count
                FROM source_groups g
                LEFT JOIN group_sources gs ON gs.group_id = g.group_id
                GROUP BY g.group_id
                ORDER BY g.name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, group_id: str) -> dict | None:
        with self._connect() as conn:
            return self._row(conn, group_id)

    def create(self, name: str) -> GroupResult:
        cleaned = self._clean_name(name)
        if cleaned is None:
            return GroupResult(ok=False, reason="reserved")
        group_id = new_group_id()
        with self._write_lock, self._connect() as conn:
            if self._name_taken(conn, cleaned):
                return GroupResult(ok=False, reason="duplicate")
            conn.execute(
                "INSERT INTO source_groups (group_id, name, created_at) VALUES (?, ?, ?)",
                (group_id, cleaned, utc_now()),
            )
            return GroupResult(ok=True, group=self._row(conn, group_id))

    def rename(self, group_id: str, name: str) -> GroupResult:
        cleaned = self._clean_name(name)
        if cleaned is None:
            return GroupResult(ok=False, reason="reserved")
        with self._write_lock, self._connect() as conn:
            if self._row(conn, group_id) is None:
                return GroupResult(ok=False, reason="missing")
            if self._name_taken(conn, cleaned, exclude=group_id):
                return GroupResult(ok=False, reason="duplicate")
            conn.execute("UPDATE source_groups SET name = ? WHERE group_id = ?", (cleaned, group_id))
            return GroupResult(ok=True, group=self._row(conn, group_id))

    def delete(self, group_id: str) -> GroupResult:
        """删分组。**还有会话在用它就不删**（否则那些会话的检索范围会悄悄变大）。"""
        with self._write_lock, self._connect() as conn:
            if self._row(conn, group_id) is None:
                return GroupResult(ok=False, reason="missing")
            blocking = self._sessions_using(conn, group_id)
            if blocking:
                return GroupResult(ok=False, reason="in_use", blocking=tuple(blocking))
            conn.execute("DELETE FROM group_sources WHERE group_id = ?", (group_id,))
            conn.execute("DELETE FROM source_groups WHERE group_id = ?", (group_id,))
        return GroupResult(ok=True)

    @staticmethod
    def _sessions_using(conn: sqlite3.Connection, group_id: str) -> list[dict]:
        """同库只读会话表：哪些会话绑了这个分组（删分组前要确认没人用）。

        sessions 表归 SessionStore 管，所以先确认它在（单独用本模块时当然没人引用）。
        """
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()
        if not exists:
            return []
        rows = conn.execute(
            "SELECT session_id, title FROM sessions WHERE group_id = ? ORDER BY seq DESC",
            (group_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # —— 成员关系 ————————————————————————————————————————————

    def set_source_groups(self, source_id: str, group_ids: Iterable[str]) -> GroupResult:
        """把一个数据源的分组**整体替换**成给定集合（前端多选后一次提交，天然幂等）。

        传空列表 = 从所有分组里拿出来（回到「未分组」）。
        """
        wanted = list(dict.fromkeys(item for item in ((g or "").strip() for g in group_ids) if item))
        with self._write_lock, self._connect() as conn:
            existing = {row["group_id"] for row in conn.execute("SELECT group_id FROM source_groups")}
            unknown = [item for item in wanted if item not in existing]
            if unknown:
                return GroupResult(ok=False, reason="unknown", unknown=tuple(unknown))
            conn.execute("DELETE FROM group_sources WHERE source_id = ?", (source_id,))
            now = utc_now()
            conn.executemany(
                "INSERT INTO group_sources (group_id, source_id, added_at) VALUES (?, ?, ?)",
                [(item, source_id, now) for item in wanted],
            )
        return GroupResult(ok=True, group_ids=tuple(wanted))

    def forget_source(self, source_id: str) -> int:
        """数据源被删掉时清掉它的成员关系，返回清掉的行数（不留指不到东西的成员记录）。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM group_sources WHERE source_id = ?", (source_id,))
        return cursor.rowcount

    def groups_of_sources(self, source_ids: Iterable[str]) -> dict[str, list[str]]:
        """批量查「每个数据源在哪些分组里」（列表页一次查完，避免一个源一次查询）。"""
        ids = [item for item in source_ids if item]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT gs.source_id, gs.group_id
                FROM group_sources gs
                JOIN source_groups g ON g.group_id = gs.group_id
                WHERE gs.source_id IN ({placeholders})
                ORDER BY g.name
                """,
                ids,
            ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["source_id"], []).append(row["group_id"])
        return out

    def sources_of(self, group_id: str) -> list[str]:
        """一个分组里的数据源 id（提问时就拿它当检索范围）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_id FROM group_sources WHERE group_id = ? ORDER BY added_at, rowid",
                (group_id,),
            ).fetchall()
        return [row["source_id"] for row in rows]

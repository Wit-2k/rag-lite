"""会话与聊天记录的持久化：SQLite（标准库 `sqlite3`，便携包因此不必加任何依赖）。

为什么是 SQLite 而不是复用 LanceDB：会话列表要按时间排序、删除要连带消息、要按会话取历史——
都是关系型操作的强项（有事务、有索引），而 LanceDB 是向量库，做这些很别扭。

库文件放在 `data/sessions.db`（与向量库 `data/lancedb/` 同层），所以整包拷走时会话也跟着走。

并发与线程：FastAPI 的同步端点跑在线程池里，`sqlite3` 的连接默认不能跨线程用。
这里的做法是**每次调用开一个短连接**（SQLite 打开本地文件非常便宜），并启用 WAL——
读不会挡写、写不会挡读，天然不怕多线程。写操作另外上一把进程内锁，
避免「检查再写入」这类逻辑（比如自动起标题）两个线程同时进来。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.schema import utc_now

DEFAULT_TITLE = "新会话"
TITLE_MAX_CHARS = 24  # 自动起标题时截断长度（后面加省略号）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- 列表排序用的单调序号：updated_at 是秒精度（跟向量库那份数据统一），
    -- 同一秒里建两个会话、或者建完立刻提问，时间戳会打平 → 靠 seq 才能稳定排出「最近在用」
    seq        INTEGER NOT NULL DEFAULT 0,
    -- 检索范围：绑定的数据源分组；NULL = 全部数据源（老会话就是它，行为不变）
    group_id   TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增即顺序，取历史不用再排序
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,                      -- user / assistant
    content    TEXT NOT NULL,
    sources    TEXT NOT NULL DEFAULT '[]',         -- 助手消息附带的参考片段（JSON，便于切换会话时还原来源行）
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, message_id);
"""


_NEXT_SEQ = "(SELECT COALESCE(MAX(seq), 0) + 1 FROM sessions)"  # 每次写都取「当前最大 + 1」


def new_session_id() -> str:
    """会话 id：`sess_` + 12 位十六进制，跟数据源 id 一个风格（稳定、可读、不乱）。"""
    return f"sess_{uuid.uuid4().hex[:12]}"


def auto_title(question: str) -> str:
    """用第一个问题生成标题：压掉换行与多余空白，截断到 TITLE_MAX_CHARS。"""
    flat = " ".join(question.split())
    return flat if len(flat) <= TITLE_MAX_CHARS else flat[:TITLE_MAX_CHARS] + "…"


class SessionStore:
    """会话表 + 消息表的薄封装（每次调用开短连接，见模块开头的说明）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()  # 只护「先读后写」那几处，不护单条 SQL
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 老库（本功能早期的形状）没有 seq 列，补上；新库自然跳过
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "seq" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")
            if "group_id" not in columns:
                # 分组功能之前建的库没有这一列；补上后老会话 = 全部数据源（与旧行为一致）
                conn.execute("ALTER TABLE sessions ADD COLUMN group_id TEXT")

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

    # —— 会话 ————————————————————————————————————————————————

    def list(self) -> list[dict]:
        """按最近使用倒序列出所有会话（看 seq，不看时间戳精度），附带消息条数。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.session_id, s.title, s.created_at, s.updated_at, s.group_id,
                       COUNT(m.message_id) AS message_count
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.seq DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, session_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def create(self, title: str = DEFAULT_TITLE, group_id: str | None = None) -> dict:
        session_id, now = new_session_id(), utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, title, created_at, updated_at, seq, group_id) "
                f"VALUES (?, ?, ?, ?, {_NEXT_SEQ}, ?)",
                (session_id, title.strip() or DEFAULT_TITLE, now, now, group_id),
            )
        return {"session_id": session_id, "title": title.strip() or DEFAULT_TITLE, "created_at": now,
                "updated_at": now, "message_count": 0, "group_id": group_id}

    def set_group(self, session_id: str, group_id: str | None) -> bool:
        """改这个会话的检索范围（None = 全部数据源）。

        刻意不动 `updated_at` / `seq`：换个检索范围不算「用了它」，
        不该把会话顶到列表最前面（列表顺序只该跟着真的问答走）。
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET group_id = ? WHERE session_id = ?", (group_id, session_id)
            )
        return cursor.rowcount > 0

    def rename(self, session_id: str, title: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE sessions SET title = ?, updated_at = ?, seq = {_NEXT_SEQ} WHERE session_id = ?",
                (title.strip() or DEFAULT_TITLE, utc_now(), session_id),
            )
        return cursor.rowcount > 0

    def delete(self, session_id: str) -> bool:
        """删会话，连带它的消息一起删（同一个事务里，不会留半截）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cursor = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        return cursor.rowcount > 0

    # —— 消息 ————————————————————————————————————————————————

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        sources: list[dict] | None = None,
    ) -> int:
        """追加一条消息，并把会话的 updated_at 顶到最新（列表按它排序）。返回 message_id。"""
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (session_id, role, content, sources, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, json.dumps(sources or [], ensure_ascii=False), now),
            )
            conn.execute(
                f"UPDATE sessions SET updated_at = ?, seq = {_NEXT_SEQ} WHERE session_id = ?", (now, session_id)
            )
        return int(cursor.lastrowid)

    def drop_message(self, message_id: int) -> None:
        """删掉单条消息（用于回滚：问题已落库但一个字都没产出时，别留下没有回答的问答）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))

    def history(self, session_id: str) -> list[dict]:
        """喂给大模型的历史：只要 role 与 content（参考片段不进历史，避免提示词随轮数膨胀）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY message_id",
                (session_id,),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def messages(self, session_id: str) -> list[dict]:
        """给前端渲染用的完整消息（含助手消息附带的参考片段）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT message_id, role, content, sources, created_at FROM messages "
                "WHERE session_id = ? ORDER BY message_id",
                (session_id,),
            ).fetchall()
        out = []
        for row in rows:
            try:
                sources = json.loads(row["sources"])
            except json.JSONDecodeError:
                sources = []
            out.append({
                "message_id": row["message_id"],
                "role": row["role"],
                "content": row["content"],
                "sources": sources,
                "created_at": row["created_at"],
            })
        return out

    def clear(self, session_id: str) -> int:
        """清空某个会话的消息（会话本身保留），返回删掉的条数。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute(
                f"UPDATE sessions SET updated_at = ?, seq = {_NEXT_SEQ} WHERE session_id = ?",
                (utc_now(), session_id),
            )
        return cursor.rowcount

    def title_from_question(self, session_id: str, question: str) -> str | None:
        """首次提问后据首问起标题；标题已被改过（或已有消息）就不动，返回新标题或 None。"""
        with self._write_lock:
            with self._connect() as conn:
                session = conn.execute(
                    "SELECT title FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if session is None or session["title"] != DEFAULT_TITLE:
                    return None
                existing = conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
                ).fetchone()
                if existing["n"] > 0:  # 已经不是第一问了
                    return None
                title = auto_title(question)
                conn.execute(
                    f"UPDATE sessions SET title = ?, updated_at = ?, seq = {_NEXT_SEQ} WHERE session_id = ?",
                    (title, utc_now(), session_id),
                )
        return title

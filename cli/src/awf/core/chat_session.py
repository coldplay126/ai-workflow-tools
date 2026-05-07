from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class ChatSession:
    id: str
    provider: str
    title: str
    created_at: str
    updated_at: str
    token_input_estimate: int
    token_output_estimate: int
    cost_usd_estimate: float


@dataclass
class ChatMessage:
    role: str
    content: str
    created_at: str
    token_estimate: int
    cost_usd_estimate: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    provider TEXT NOT NULL,
    title TEXT NOT NULL,
    token_input_estimate INTEGER NOT NULL DEFAULT 0,
    token_output_estimate INTEGER NOT NULL DEFAULT 0,
    cost_usd_estimate REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES session(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    cost_usd_estimate REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_session_id_id
ON message(session_id, id);
"""


def ensure_session_db(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(SCHEMA)
        session_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(session)").fetchall()}
        if "token_input_estimate" not in session_columns:
            conn.execute("ALTER TABLE session ADD COLUMN token_input_estimate INTEGER NOT NULL DEFAULT 0")
        if "token_output_estimate" not in session_columns:
            conn.execute("ALTER TABLE session ADD COLUMN token_output_estimate INTEGER NOT NULL DEFAULT 0")
        if "cost_usd_estimate" not in session_columns:
            conn.execute("ALTER TABLE session ADD COLUMN cost_usd_estimate REAL NOT NULL DEFAULT 0")
        message_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(message)").fetchall()}
        if "token_estimate" not in message_columns:
            conn.execute("ALTER TABLE message ADD COLUMN token_estimate INTEGER NOT NULL DEFAULT 0")
        if "cost_usd_estimate" not in message_columns:
            conn.execute("ALTER TABLE message ADD COLUMN cost_usd_estimate REAL NOT NULL DEFAULT 0")
        conn.commit()
    return db_path


def create_session(db_path: Path, provider_name: str, title: str = "awf chat") -> ChatSession:
    ensure_session_db(db_path)
    session_id = str(uuid.uuid4())
    now = _now_iso()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO session (
                id, mode, provider, title, token_input_estimate, token_output_estimate, cost_usd_estimate, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, "chat", provider_name, title, 0, 0, 0.0, now, now),
        )
        conn.commit()
    return ChatSession(
        id=session_id,
        provider=provider_name,
        title=title,
        created_at=now,
        updated_at=now,
        token_input_estimate=0,
        token_output_estimate=0,
        cost_usd_estimate=0.0,
    )


def get_session(db_path: Path, session_id: str) -> ChatSession | None:
    ensure_session_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            """
            SELECT id, provider, title, created_at, updated_at, token_input_estimate, token_output_estimate, cost_usd_estimate
            FROM session
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return ChatSession(
        id=str(row[0]),
        provider=str(row[1]),
        title=str(row[2]),
        created_at=str(row[3]),
        updated_at=str(row[4]),
        token_input_estimate=int(row[5]),
        token_output_estimate=int(row[6]),
        cost_usd_estimate=float(row[7]),
    )


def list_sessions(db_path: Path) -> list[dict[str, str | int]]:
    ensure_session_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.provider,
                s.title,
                s.created_at,
                s.updated_at,
                s.token_input_estimate,
                s.token_output_estimate,
                s.cost_usd_estimate,
                COUNT(m.id) AS message_count
            FROM session s
            LEFT JOIN message m ON m.session_id = s.id
            GROUP BY
                s.id, s.provider, s.title, s.created_at, s.updated_at,
                s.token_input_estimate, s.token_output_estimate, s.cost_usd_estimate
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
    return [
        {
            "id": str(row[0]),
            "provider": str(row[1]),
            "title": str(row[2]),
            "created_at": str(row[3]),
            "updated_at": str(row[4]),
            "token_input_estimate": int(row[5]),
            "token_output_estimate": int(row[6]),
            "cost_usd_estimate": float(row[7]),
            "message_count": int(row[8]),
        }
        for row in rows
    ]


def latest_session_id(db_path: Path) -> str | None:
    sessions = list_sessions(db_path)
    if not sessions:
        return None
    return str(sessions[0]["id"])


def append_message(
    db_path: Path,
    session_id: str,
    role: str,
    content: str,
    *,
    token_estimate: int = 0,
    cost_usd_estimate: float = 0.0,
) -> ChatMessage:
    ensure_session_db(db_path)
    now = _now_iso()
    input_increment = token_estimate if role == "user" else 0
    output_increment = token_estimate if role == "assistant" else 0
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO message (session_id, role, content, token_estimate, cost_usd_estimate, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, int(token_estimate), float(cost_usd_estimate), now),
        )
        conn.execute(
            """
            UPDATE session
            SET
                updated_at = ?,
                token_input_estimate = token_input_estimate + ?,
                token_output_estimate = token_output_estimate + ?,
                cost_usd_estimate = cost_usd_estimate + ?
            WHERE id = ?
            """,
            (now, int(input_increment), int(output_increment), float(cost_usd_estimate), session_id),
        )
        conn.commit()
    return ChatMessage(
        role=role,
        content=content,
        created_at=now,
        token_estimate=int(token_estimate),
        cost_usd_estimate=float(cost_usd_estimate),
    )


def load_messages(db_path: Path, session_id: str) -> list[ChatMessage]:
    ensure_session_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT role, content, created_at, token_estimate, cost_usd_estimate
            FROM message
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
    return [
        ChatMessage(
            role=str(row[0]),
            content=str(row[1]),
            created_at=str(row[2]),
            token_estimate=int(row[3]),
            cost_usd_estimate=float(row[4]),
        )
        for row in rows
    ]


def update_latest_message_usage(
    db_path: Path,
    session_id: str,
    role: str,
    *,
    token_estimate: int,
    cost_usd_estimate: float,
) -> None:
    ensure_session_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            """
            SELECT id, token_estimate, cost_usd_estimate
            FROM message
            WHERE session_id = ? AND role = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id, role),
        ).fetchone()
        if row is None:
            return
        message_id = int(row[0])
        old_tokens = int(row[1] or 0)
        old_cost = float(row[2] or 0.0)
        conn.execute(
            "UPDATE message SET token_estimate = ?, cost_usd_estimate = ? WHERE id = ?",
            (int(token_estimate), float(cost_usd_estimate), message_id),
        )
        session_column = "token_input_estimate" if role == "user" else "token_output_estimate"
        conn.execute(
            f"""
            UPDATE session
            SET {session_column} = {session_column} + ?,
                cost_usd_estimate = cost_usd_estimate + ?
            WHERE id = ?
            """,
            (int(token_estimate) - old_tokens, float(cost_usd_estimate) - old_cost, session_id),
        )
        conn.commit()


def _truncate_summary_text(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _build_summary_message(messages: list[ChatMessage]) -> str:
    lines = ["Session summary of earlier turns:"]
    for item in messages:
        if item.role == "system" and item.content.startswith("Session summary of earlier turns:"):
            summary_lines = item.content.splitlines()[1:]
            if summary_lines:
                lines.extend(summary_lines)
                continue
        lines.append(f"- {item.role}: {_truncate_summary_text(item.content)}")
    return "\n".join(lines)


def compact_session(
    db_path: Path,
    session_id: str,
    *,
    max_messages: int = 6,
    keep_last: int = 4,
    summary_content: str | None = None,
) -> dict[str, int | bool]:
    ensure_session_db(db_path)
    messages = load_messages(db_path, session_id)
    original_count = len(messages)
    if original_count < max_messages:
        return {
            "compacted": False,
            "original_count": original_count,
            "new_count": original_count,
            "summarized_count": 0,
            "summary_mode": "none",
        }

    keep_last = max(1, keep_last)
    summary_source = messages[:-keep_last]
    recent_messages = messages[-keep_last:]
    if not summary_source:
        return {
            "compacted": False,
            "original_count": original_count,
            "new_count": original_count,
            "summarized_count": 0,
            "summary_mode": "none",
        }

    summary_message = ChatMessage(
        role="system",
        content=summary_content or _build_summary_message(summary_source),
        created_at=_now_iso(),
        token_estimate=sum(item.token_estimate for item in summary_source),
        cost_usd_estimate=sum(item.cost_usd_estimate for item in summary_source),
    )

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
        conn.execute(
            """
            INSERT INTO message (session_id, role, content, token_estimate, cost_usd_estimate, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                summary_message.role,
                summary_message.content,
                summary_message.token_estimate,
                summary_message.cost_usd_estimate,
                summary_message.created_at,
            ),
        )
        for item in recent_messages:
            conn.execute(
                """
                INSERT INTO message (session_id, role, content, token_estimate, cost_usd_estimate, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, item.role, item.content, item.token_estimate, item.cost_usd_estimate, item.created_at),
            )
        conn.execute(
            "UPDATE session SET updated_at = ? WHERE id = ?",
            (_now_iso(), session_id),
        )
        conn.commit()

    return {
        "compacted": True,
        "original_count": original_count,
        "new_count": 1 + len(recent_messages),
        "summarized_count": len(summary_source),
        "summary_mode": "provider" if summary_content else "heuristic",
    }

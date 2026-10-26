from __future__ import annotations

import math
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .paths import database_path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(slots=True)
class Profile:
    display_name: str = ""
    preferred_address: str = ""
    preferences: str = ""
    dislikes: str = ""
    goals: str = ""
    communication_style: str = ""
    notes: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> "Profile":
        if row is None:
            return cls()
        return cls(
            display_name=row["display_name"],
            preferred_address=row["preferred_address"],
            preferences=row["preferences"],
            dislikes=row["dislikes"],
            goals=row["goals"],
            communication_style=row["communication_style"],
            notes=row["notes"],
        )

    def prompt_text(self) -> str:
        labels = {
            "display_name": "称呼自己为",
            "preferred_address": "希望被称作",
            "preferences": "偏好",
            "dislikes": "不喜欢",
            "goals": "目标或近期关注",
            "communication_style": "希望的交流方式",
            "notes": "其他备注",
        }
        lines = []
        for key, value in asdict(self).items():
            if str(value).strip():
                lines.append(f"- {labels[key]}：{str(value).strip()}")
        return "\n".join(lines) if lines else "- 暂无已保存的画像信息"


@dataclass(slots=True)
class Memory:
    id: int
    kind: str
    mem_key: str
    value: str
    importance: int
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ChatMessage:
    id: int
    conversation_id: int
    role: str
    content: str
    is_key: bool
    created_at: str
    attachment_path: str = ""


@dataclass(slots=True)
class Interaction:
    id: int
    conversation_id: int
    kind: str
    summary: str
    created_at: str


@dataclass(slots=True)
class QQConversation:
    id: int
    chat_key: str
    title: str
    is_group: bool
    last_signature: str
    updated_at: str


@dataclass(slots=True)
class QQMessage:
    id: int
    conversation_id: int
    sender: str
    content: str
    is_mentioned: bool
    fingerprint: str
    received_at: str


@dataclass(slots=True)
class QQDraft:
    id: int
    message_id: int
    chat_title: str
    incoming: str
    content: str
    status: str
    created_at: str


@dataclass(slots=True)
class QQInitiative:
    id: int
    event_type: str
    target_title: str
    member_name: str
    context: str
    content: str
    status: str
    created_at: str


@dataclass(slots=True)
class EventMemory:
    id: int
    scope: str
    related_user_key: str
    kind: str
    summary: str
    details: str
    importance: int
    compacted: bool
    occurred_at: str


@dataclass(slots=True)
class EventMemorySummary:
    id: int
    scope: str
    related_user_key: str
    period_start: str
    period_end: str
    summary: str
    event_ids: tuple[int, ...]
    created_at: str


@dataclass(slots=True)
class QQUserProfile:
    user_key: str
    display_name: str
    chat_scope: str
    is_group: bool
    mention_count: int
    message_count: int
    profile_summary: str
    last_message: str
    first_seen: str
    last_seen: str

    def prompt_text(self) -> str:
        relation = "群聊成员" if self.is_group else "私聊联系人"
        parts = [
            f"- 称呼：{self.display_name}",
            f"- 关系：{relation}",
            f"- 已记录互动：{self.message_count} 次",
        ]
        if self.mention_count:
            parts.append(f"- 曾 @ 大肥鱼：{self.mention_count} 次")
        if self.profile_summary:
            parts.append(f"- 简单画像：{self.profile_summary}")
        if self.last_message:
            parts.append(f"- 最近发言：{self.last_message[:240]}")
        return "\n".join(parts)


@dataclass(slots=True)
class PetPersona:
    traits: str
    preferences: str
    knowledge: str
    habits: str
    relationships: str
    revision: int
    updated_at: str

    def prompt_text(self) -> str:
        sections = (
            ("新增性格倾向", self.traits),
            ("新增偏好", self.preferences),
            ("学习到的知识", self.knowledge),
            ("新增说话与行为习惯", self.habits),
            ("新增关系认知", self.relationships),
        )
        lines = [
            f"- {label}：{value.strip()}"
            for label, value in sections
            if value.strip()
        ]
        if not lines:
            return "- 暂无后天成长记录"
        return "\n".join(lines)


@dataclass(slots=True)
class PersonaLearning:
    id: int
    category: str
    content: str
    confidence: int
    source: str
    created_at: str


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or database_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    display_name TEXT NOT NULL DEFAULT '',
                    preferred_address TEXT NOT NULL DEFAULT '',
                    preferences TEXT NOT NULL DEFAULT '',
                    dislikes TEXT NOT NULL DEFAULT '',
                    goals TEXT NOT NULL DEFAULT '',
                    communication_style TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '新对话',
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL
                        REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    is_key INTEGER NOT NULL DEFAULT 0 CHECK (is_key IN (0, 1)),
                    importance INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    mem_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind, mem_key)
                );

                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL
                        REFERENCES conversations(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS qq_conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    is_group INTEGER NOT NULL DEFAULT 0 CHECK (is_group IN (0, 1)),
                    last_signature TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS qq_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL
                        REFERENCES qq_conversations(id) ON DELETE CASCADE,
                    sender TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    is_mentioned INTEGER NOT NULL DEFAULT 0
                        CHECK (is_mentioned IN (0, 1)),
                    fingerprint TEXT NOT NULL UNIQUE,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS qq_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL UNIQUE
                        REFERENCES qq_messages(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS qq_initiatives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    target_title TEXT NOT NULL DEFAULT '',
                    member_name TEXT NOT NULL DEFAULT '',
                    context TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL DEFAULT '',
                    related_user_key TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    importance INTEGER NOT NULL DEFAULT 2,
                    compacted INTEGER NOT NULL DEFAULT 0
                        CHECK (compacted IN (0, 1)),
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_memory_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL DEFAULT '',
                    related_user_key TEXT NOT NULL DEFAULT '',
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    event_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS qq_user_profiles (
                    user_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    chat_scope TEXT NOT NULL,
                    is_group INTEGER NOT NULL DEFAULT 0
                        CHECK (is_group IN (0, 1)),
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    profile_summary TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT '',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pet_persona (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    traits TEXT NOT NULL DEFAULT '',
                    preferences TEXT NOT NULL DEFAULT '',
                    knowledge TEXT NOT NULL DEFAULT '',
                    habits TEXT NOT NULL DEFAULT '',
                    relationships TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pet_persona_learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    confidence INTEGER NOT NULL DEFAULT 3,
                    source TEXT NOT NULL DEFAULT 'conversation',
                    created_at TEXT NOT NULL,
                    UNIQUE(category, content)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_messages_key
                    ON messages(is_key, id DESC);
                CREATE INDEX IF NOT EXISTS idx_memories_rank
                    ON memories(importance DESC, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_interactions_conversation
                    ON interactions(conversation_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_qq_messages_chat
                    ON qq_messages(conversation_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_qq_drafts_created
                    ON qq_drafts(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_qq_initiatives_created
                    ON qq_initiatives(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_event_memories_scope
                    ON event_memories(scope, compacted, id DESC);
                CREATE INDEX IF NOT EXISTS idx_event_summaries_scope
                    ON event_memory_summaries(scope, related_user_key, id DESC);
                CREATE INDEX IF NOT EXISTS idx_qq_user_profiles_seen
                    ON qq_user_profiles(last_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_persona_learnings_created
                    ON pet_persona_learnings(created_at DESC);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO profile
                    (id, updated_at)
                VALUES (1, ?)
                """,
                (utc_now(),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO pet_persona
                    (id, updated_at)
                VALUES (1, ?)
                """,
                (utc_now(),),
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(messages)")
            }
            if "attachment_path" not in columns:
                connection.execute(
                    """
                    ALTER TABLE messages
                    ADD COLUMN attachment_path TEXT NOT NULL DEFAULT ''
                    """
                )

    def get_profile(self) -> Profile:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM profile WHERE id = 1").fetchone()
            return Profile.from_row(row)

    def save_profile(self, profile: Profile) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO profile (
                    id, display_name, preferred_address, preferences, dislikes,
                    goals, communication_style, notes, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    preferred_address = excluded.preferred_address,
                    preferences = excluded.preferences,
                    dislikes = excluded.dislikes,
                    goals = excluded.goals,
                    communication_style = excluded.communication_style,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.display_name.strip(),
                    profile.preferred_address.strip(),
                    profile.preferences.strip(),
                    profile.dislikes.strip(),
                    profile.goals.strip(),
                    profile.communication_style.strip(),
                    profile.notes.strip(),
                    utc_now(),
                ),
            )

    def create_conversation(self, title: str = "新对话") -> int:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO conversations (title, started_at, updated_at) VALUES (?, ?, ?)",
                (title.strip()[:80] or "新对话", now, now),
            )
            return int(cursor.lastrowid)

    def latest_conversation(self, create: bool = True) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM conversations ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()
        if row:
            return int(row["id"])
        return self.create_conversation() if create else None

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        *,
        is_key: bool = False,
        importance: int = 0,
        attachment_path: str = "",
    ) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        content = content.strip()
        if not content:
            raise ValueError("message cannot be empty")
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages
                    (
                        conversation_id, role, content, is_key, importance,
                        created_at, attachment_path
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    role,
                    content,
                    int(is_key),
                    importance,
                    now,
                    attachment_path.strip()[:2000],
                ),
            )
            update_title = (
                connection.execute(
                    "SELECT title FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
            )
            if role == "user" and update_title and update_title["title"] == "新对话":
                title = content.replace("\n", " ").strip()[:26]
                connection.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (title, conversation_id),
                )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            return int(cursor.lastrowid)

    def recent_messages(self, conversation_id: int, limit: int = 16) -> list[ChatMessage]:
        limit = max(1, min(int(limit), 100))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            ChatMessage(
                id=int(row["id"]),
                conversation_id=int(row["conversation_id"]),
                role=row["role"],
                content=row["content"],
                is_key=bool(row["is_key"]),
                created_at=row["created_at"],
                attachment_path=row["attachment_path"],
            )
            for row in rows
        ]

    def add_interaction(
        self,
        conversation_id: int,
        kind: str,
        summary: str,
    ) -> int:
        kind = kind.strip()[:40] or "interaction"
        summary = summary.strip()[:500]
        if not summary:
            raise ValueError("interaction summary cannot be empty")
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO interactions
                    (conversation_id, kind, summary, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (conversation_id, kind, summary, now),
            )
            connection.execute(
                """
                INSERT INTO event_memories
                    (
                        scope, related_user_key, kind, summary, details,
                        importance, compacted, occurred_at
                    )
                VALUES (?, '', ?, ?, '', ?, 0, ?)
                """,
                (
                    f"main:{conversation_id}",
                    kind,
                    summary,
                    _event_importance(kind),
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def recent_interactions(
        self,
        conversation_id: int,
        limit: int = 20,
    ) -> list[Interaction]:
        limit = max(1, min(int(limit), 100))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM interactions
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            Interaction(
                id=int(row["id"]),
                conversation_id=int(row["conversation_id"]),
                kind=row["kind"],
                summary=row["summary"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def add_event_memory(
        self,
        kind: str,
        summary: str,
        *,
        scope: str = "",
        related_user_key: str = "",
        details: str = "",
        importance: int = 2,
        occurred_at: str | None = None,
    ) -> int:
        summary = summary.strip()[:500]
        if not summary:
            raise ValueError("event memory summary cannot be empty")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO event_memories
                    (
                        scope, related_user_key, kind, summary, details,
                        importance, compacted, occurred_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    scope.strip()[:160],
                    related_user_key.strip()[:240],
                    kind.strip()[:40] or "event",
                    summary,
                    details.strip()[:8000],
                    max(1, min(int(importance), 5)),
                    occurred_at or utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _event_memory_from_row(row: sqlite3.Row) -> EventMemory:
        return EventMemory(
            id=int(row["id"]),
            scope=row["scope"],
            related_user_key=row["related_user_key"],
            kind=row["kind"],
            summary=row["summary"],
            details=row["details"],
            importance=int(row["importance"]),
            compacted=bool(row["compacted"]),
            occurred_at=row["occurred_at"],
        )

    def list_event_memories(self, limit: int = 200) -> list[EventMemory]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_memories
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._event_memory_from_row(row) for row in rows]

    def recent_event_memories(
        self,
        *,
        scope: str = "",
        related_user_key: str = "",
        limit: int = 12,
    ) -> list[EventMemory]:
        clauses = ["compacted = 0"]
        parameters: list[object] = []
        if scope.strip():
            clauses.append("scope = ?")
            parameters.append(scope.strip())
        if related_user_key.strip():
            clauses.append("related_user_key = ?")
            parameters.append(related_user_key.strip())
        parameters.append(max(1, min(int(limit), 100)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM event_memories
                WHERE {' AND '.join(clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [
            self._event_memory_from_row(row)
            for row in reversed(rows)
        ]

    def relevant_event_memories(
        self,
        query: str,
        *,
        scope: str = "",
        related_user_key: str = "",
        limit: int = 6,
    ) -> list[EventMemory]:
        if limit <= 0:
            return []
        clauses = ["compacted = 0"]
        parameters: list[object] = []
        if scope.strip():
            clauses.append("scope = ?")
            parameters.append(scope.strip())
        if related_user_key.strip():
            clauses.append("related_user_key = ?")
            parameters.append(related_user_key.strip())
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM event_memories
                    WHERE {' AND '.join(clauses)}
                    ORDER BY id DESC
                    LIMIT 400
                )
                """,
                parameters,
            ).fetchall()
        terms = _terms(query)
        scored: list[tuple[float, EventMemory]] = []
        for row in rows:
            event = self._event_memory_from_row(row)
            overlap = sum(
                1
                for term in terms
                if term in f"{event.summary} {event.details}".casefold()
            )
            score = overlap * 4.0 + event.importance + event.id / 1_000_000
            if overlap:
                scored.append((score, event))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [event for _score, event in scored[: max(1, int(limit))]]

    @staticmethod
    def _event_summary_from_row(row: sqlite3.Row) -> EventMemorySummary:
        try:
            parsed_ids = json.loads(row["event_ids"])
            event_ids = tuple(int(value) for value in parsed_ids)
        except (TypeError, ValueError, json.JSONDecodeError):
            event_ids = ()
        return EventMemorySummary(
            id=int(row["id"]),
            scope=row["scope"],
            related_user_key=row["related_user_key"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            summary=row["summary"],
            event_ids=event_ids,
            created_at=row["created_at"],
        )

    def list_event_summaries(
        self,
        limit: int = 100,
    ) -> list[EventMemorySummary]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_memory_summaries
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._event_summary_from_row(row) for row in rows]

    def relevant_event_summaries(
        self,
        query: str,
        *,
        scope: str = "",
        related_user_key: str = "",
        limit: int = 4,
    ) -> list[EventMemorySummary]:
        if limit <= 0:
            return []
        clauses: list[str] = []
        parameters: list[object] = []
        if scope.strip():
            clauses.append("scope = ?")
            parameters.append(scope.strip())
        if related_user_key.strip():
            clauses.append("related_user_key = ?")
            parameters.append(related_user_key.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM event_memory_summaries
                {where}
                ORDER BY id DESC
                LIMIT 300
                """,
                parameters,
            ).fetchall()
        terms = _terms(query)
        scored: list[tuple[float, EventMemorySummary]] = []
        for row in rows:
            summary = self._event_summary_from_row(row)
            overlap = sum(1 for term in terms if term in summary.summary.casefold())
            if overlap:
                scored.append((overlap * 4.0 + summary.id / 1_000_000, summary))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _score, item in scored[: max(1, int(limit))]]

    def compact_old_event_memories(
        self,
        *,
        older_than_days: int = 7,
        scope: str = "",
    ) -> int:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=max(1, int(older_than_days)))
        ).isoformat(timespec="microseconds")
        clauses = ["compacted = 0", "occurred_at < ?"]
        parameters: list[object] = [cutoff]
        if scope.strip():
            clauses.append("scope = ?")
            parameters.append(scope.strip())
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM event_memories
                WHERE {' AND '.join(clauses)}
                ORDER BY occurred_at ASC, id ASC
                """,
                parameters,
            ).fetchall()
            grouped: dict[tuple[str, str, str], list[EventMemory]] = {}
            for row in rows:
                event = self._event_memory_from_row(row)
                grouped.setdefault(
                    (
                        event.scope,
                        event.related_user_key,
                        event.occurred_at[:10],
                    ),
                    [],
                ).append(event)

            now = utc_now()
            created = 0
            for (
                event_scope,
                related_user_key,
                day,
            ), events in grouped.items():
                key_events = [event for event in events if event.importance >= 3]
                lines = [
                    (
                        f"[{event.occurred_at[11:16]} #{event.id}] "
                        f"{event.summary}"
                    )
                    for event in key_events
                ]
                low_count = len(events) - len(key_events)
                if not lines:
                    lines.append(f"当天有 {len(events)} 条低优先级日常事件")
                elif low_count:
                    lines.append(f"当天另有 {low_count} 条低优先级日常事件")
                connection.execute(
                    """
                    INSERT INTO event_memory_summaries
                        (
                            scope, related_user_key, period_start, period_end, summary,
                            event_ids, created_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_scope,
                        related_user_key,
                        f"{day}T00:00:00+00:00",
                        f"{day}T23:59:59+00:00",
                        "\n".join(lines)[:8000],
                        json.dumps(
                            [event.id for event in events],
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                placeholders = ",".join("?" for _event in events)
                connection.execute(
                    f"""
                    UPDATE event_memories
                    SET compacted = 1
                    WHERE id IN ({placeholders})
                    """,
                    [event.id for event in events],
                )
                created += 1
            return created

    def upsert_qq_user_profile(
        self,
        user_key: str,
        display_name: str,
        chat_scope: str,
        *,
        is_group: bool,
        mentioned_self: bool,
        message: str,
    ) -> QQUserProfile:
        user_key = user_key.strip()[:240]
        display_name = display_name.strip()[:120]
        chat_scope = chat_scope.strip()[:160]
        if not user_key or not display_name:
            raise ValueError("QQ user profile needs a key and display name")
        now = utc_now()
        clean_message = re.sub(r"\s+", " ", message).strip()[:500]
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM qq_user_profiles WHERE user_key = ?",
                (user_key,),
            ).fetchone()
            mention_count = int(existing["mention_count"]) if existing else 0
            message_count = int(existing["message_count"]) if existing else 0
            if mentioned_self:
                mention_count += 1
            message_count += 1
            summary_parts = [
                "在群里主动 @ 过大肥鱼" if mentioned_self else "有过直接聊天",
            ]
            signal = _qq_profile_signal(clean_message)
            if signal:
                summary_parts.append(signal)
            if clean_message:
                summary_parts.append(f"最近聊到：{clean_message[:160]}")
            connection.execute(
                """
                INSERT INTO qq_user_profiles
                    (
                        user_key, display_name, chat_scope, is_group,
                        mention_count, message_count, profile_summary,
                        last_message, first_seen, last_seen
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    chat_scope = excluded.chat_scope,
                    is_group = excluded.is_group,
                    mention_count = excluded.mention_count,
                    message_count = excluded.message_count,
                    profile_summary = excluded.profile_summary,
                    last_message = excluded.last_message,
                    last_seen = excluded.last_seen
                """,
                (
                    user_key,
                    display_name,
                    chat_scope,
                    int(is_group),
                    mention_count,
                    message_count,
                    "；".join(summary_parts)[:1000],
                    clean_message,
                    existing["first_seen"] if existing else now,
                    now,
                ),
            )
        profile = self.get_qq_user_profile(user_key)
        if profile is None:
            raise RuntimeError("QQ user profile was not saved")
        return profile

    @staticmethod
    def _qq_profile_from_row(row: sqlite3.Row) -> QQUserProfile:
        return QQUserProfile(
            user_key=row["user_key"],
            display_name=row["display_name"],
            chat_scope=row["chat_scope"],
            is_group=bool(row["is_group"]),
            mention_count=int(row["mention_count"]),
            message_count=int(row["message_count"]),
            profile_summary=row["profile_summary"],
            last_message=row["last_message"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )

    def get_qq_user_profile(self, user_key: str) -> QQUserProfile | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM qq_user_profiles WHERE user_key = ?",
                (user_key,),
            ).fetchone()
        return self._qq_profile_from_row(row) if row else None

    def recent_qq_user_profiles(
        self,
        limit: int = 100,
    ) -> list[QQUserProfile]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM qq_user_profiles
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._qq_profile_from_row(row) for row in rows]

    def get_pet_persona(self) -> PetPersona:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pet_persona WHERE id = 1"
            ).fetchone()
        if row is None:
            return PetPersona("", "", "", "", "", 0, utc_now())
        return PetPersona(
            traits=row["traits"],
            preferences=row["preferences"],
            knowledge=row["knowledge"],
            habits=row["habits"],
            relationships=row["relationships"],
            revision=int(row["revision"]),
            updated_at=row["updated_at"],
        )

    def list_pet_persona_learnings(
        self,
        limit: int = 200,
    ) -> list[PersonaLearning]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM pet_persona_learnings
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [
            PersonaLearning(
                id=int(row["id"]),
                category=row["category"],
                content=row["content"],
                confidence=int(row["confidence"]),
                source=row["source"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def apply_pet_persona_learnings(
        self,
        learnings: list[dict[str, object]],
        *,
        source: str = "conversation",
    ) -> int:
        category_fields = {
            "trait": "traits",
            "preference": "preferences",
            "knowledge": "knowledge",
            "habit": "habits",
            "style": "habits",
            "relationship": "relationships",
        }
        normalized: list[tuple[str, str, int]] = []
        seen: set[tuple[str, str]] = set()
        for item in learnings[:20]:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "")).strip().lower()
            field = category_fields.get(category)
            content = re.sub(
                r"\s+",
                " ",
                str(item.get("value", "")),
            ).strip(" ，,。；;")
            if not field or not content:
                continue
            content = content[:280]
            try:
                confidence = int(item.get("confidence", 3))
            except (TypeError, ValueError):
                confidence = 3
            confidence = max(1, min(confidence, 5))
            key = (field, content)
            if key in seen:
                continue
            seen.add(key)
            normalized.append((field, content, confidence))
        if not normalized:
            return 0

        now = utc_now()
        stored = 0
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pet_persona WHERE id = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO pet_persona (id, updated_at) VALUES (1, ?)",
                    (now,),
                )
                values = {
                    "traits": "",
                    "preferences": "",
                    "knowledge": "",
                    "habits": "",
                    "relationships": "",
                }
                revision = 0
            else:
                values = {
                    field: str(row[field])
                    for field in category_fields.values()
                }
                revision = int(row["revision"])

            for field, content, confidence in normalized:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO pet_persona_learnings
                        (
                            category, content, confidence, source, created_at
                        )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (field, content, confidence, source, now),
                )
                if cursor.rowcount == 0:
                    continue
                values[field] = _append_persona_value(
                    values[field],
                    content,
                )
                revision += 1
                stored += 1

            if stored:
                connection.execute(
                    """
                    UPDATE pet_persona SET
                        traits = ?,
                        preferences = ?,
                        knowledge = ?,
                        habits = ?,
                        relationships = ?,
                        revision = ?,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        values["traits"],
                        values["preferences"],
                        values["knowledge"],
                        values["habits"],
                        values["relationships"],
                        revision,
                        now,
                    ),
                )
        return stored

    def clear_pet_persona_learnings(self) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("DELETE FROM pet_persona_learnings")
            connection.execute(
                """
                UPDATE pet_persona SET
                    traits = '',
                    preferences = '',
                    knowledge = '',
                    habits = '',
                    relationships = '',
                    revision = 0,
                    updated_at = ?
                WHERE id = 1
                """,
                (now,),
            )

    def ensure_qq_conversation(
        self,
        chat_key: str,
        title: str,
        is_group: bool,
    ) -> QQConversation:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO qq_conversations
                    (chat_key, title, is_group, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    title = excluded.title,
                    is_group = excluded.is_group,
                    updated_at = excluded.updated_at
                """,
                (chat_key, title, int(is_group), now),
            )
            row = connection.execute(
                "SELECT * FROM qq_conversations WHERE chat_key = ?",
                (chat_key,),
            ).fetchone()
        return QQConversation(
            id=int(row["id"]),
            chat_key=row["chat_key"],
            title=row["title"],
            is_group=bool(row["is_group"]),
            last_signature=row["last_signature"],
            updated_at=row["updated_at"],
        )

    def set_qq_signature(self, conversation_id: int, signature: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE qq_conversations SET last_signature = ?, updated_at = ? WHERE id = ?",
                (signature, utc_now(), conversation_id),
            )

    def add_qq_message(
        self,
        conversation_id: int,
        sender: str,
        content: str,
        *,
        is_mentioned: bool,
        fingerprint: str,
    ) -> int | None:
        content = content.strip()[:12000]
        if not content:
            return None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO qq_messages
                    (conversation_id, sender, content, is_mentioned, fingerprint, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    sender.strip()[:120],
                    content,
                    int(is_mentioned),
                    fingerprint,
                    utc_now(),
                ),
            )
            if cursor.rowcount == 0:
                return None
            return int(cursor.lastrowid)

    def add_qq_draft(self, message_id: int, content: str) -> int:
        content = content.strip()[:8000]
        if not content:
            raise ValueError("QQ draft cannot be empty")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR REPLACE INTO qq_drafts
                    (message_id, content, status, created_at)
                VALUES (?, ?, 'draft', ?)
                """,
                (message_id, content, utc_now()),
            )
            return int(cursor.lastrowid)

    def set_qq_draft_status(self, message_id: int, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE qq_drafts SET status = ? WHERE message_id = ?",
                (status.strip()[:30] or "draft", int(message_id)),
            )

    def recent_qq_messages(
        self,
        conversation_id: int,
        limit: int = 12,
    ) -> list[QQMessage]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM qq_messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (conversation_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [
            QQMessage(
                id=int(row["id"]),
                conversation_id=int(row["conversation_id"]),
                sender=row["sender"],
                content=row["content"],
                is_mentioned=bool(row["is_mentioned"]),
                fingerprint=row["fingerprint"],
                received_at=row["received_at"],
            )
            for row in rows
        ]

    def recent_qq_drafts(self, limit: int = 20) -> list[QQDraft]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    d.id, d.message_id, c.title AS chat_title, m.content AS incoming,
                    d.content, d.status, d.created_at
                FROM qq_drafts d
                JOIN qq_messages m ON m.id = d.message_id
                JOIN qq_conversations c ON c.id = m.conversation_id
                ORDER BY d.id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [
            QQDraft(
                id=int(row["id"]),
                message_id=int(row["message_id"]),
                chat_title=row["chat_title"],
                incoming=row["incoming"],
                content=row["content"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def add_qq_initiative(
        self,
        event_type: str,
        content: str,
        *,
        target_title: str = "",
        member_name: str = "",
        context: str = "",
        status: str = "draft",
    ) -> int:
        content = content.strip()[:8000]
        if not content:
            raise ValueError("QQ initiative content cannot be empty")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO qq_initiatives
                    (
                        event_type, target_title, member_name, context,
                        content, status, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type.strip()[:40] or "unknown",
                    target_title.strip()[:160],
                    member_name.strip()[:120],
                    context.strip()[:8000],
                    content,
                    status.strip()[:30] or "draft",
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def set_qq_initiative_status(self, initiative_id: int, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE qq_initiatives SET status = ? WHERE id = ?",
                (status.strip()[:30] or "draft", int(initiative_id)),
            )

    def recent_qq_initiatives(self, limit: int = 30) -> list[QQInitiative]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM qq_initiatives
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [
            QQInitiative(
                id=int(row["id"]),
                event_type=row["event_type"],
                target_title=row["target_title"],
                member_name=row["member_name"],
                context=row["context"],
                content=row["content"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def latest_qq_message_at(self, conversation_id: int) -> datetime | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT received_at FROM qq_messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(conversation_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row["received_at"])
        except (TypeError, ValueError):
            return None

    def key_messages(self, limit: int = 30) -> list[ChatMessage]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE is_key = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            ChatMessage(
                id=int(row["id"]),
                conversation_id=int(row["conversation_id"]),
                role=row["role"],
                content=row["content"],
                is_key=True,
                created_at=row["created_at"],
                attachment_path=row["attachment_path"],
            )
            for row in rows
        ]

    def user_message_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS amount FROM messages WHERE role = 'user'"
            ).fetchone()
            return int(row["amount"])

    def upsert_memory(
        self,
        kind: str,
        mem_key: str,
        value: str,
        importance: int = 3,
    ) -> int:
        kind = kind.strip()[:32] or "fact"
        mem_key = mem_key.strip()[:120] or value.strip()[:80]
        value = value.strip()
        if not value:
            raise ValueError("memory value cannot be empty")
        importance = max(1, min(int(importance), 5))
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO memories
                    (kind, mem_key, value, importance, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, mem_key) DO UPDATE SET
                    value = excluded.value,
                    importance = MAX(memories.importance, excluded.importance),
                    updated_at = excluded.updated_at
                """,
                (kind, mem_key, value, importance, now, now),
            )
            row = connection.execute(
                "SELECT id FROM memories WHERE kind = ? AND mem_key = ?",
                (kind, mem_key),
            ).fetchone()
            return int(row["id"])

    def list_memories(self, limit: int = 200) -> list[Memory]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            Memory(
                id=int(row["id"]),
                kind=row["kind"],
                mem_key=row["mem_key"],
                value=row["value"],
                importance=int(row["importance"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def relevant_memories(self, query: str, limit: int = 12) -> list[Memory]:
        if int(limit) <= 0:
            return []
        memories = self.list_memories(limit=300)
        if not memories:
            return []
        query_terms = _terms(query)
        now = datetime.now(timezone.utc)

        scored: list[tuple[float, Memory]] = []
        for memory in memories:
            haystack = f"{memory.kind} {memory.mem_key} {memory.value}".casefold()
            overlap = sum(1 for term in query_terms if term in haystack)
            try:
                updated = datetime.fromisoformat(memory.updated_at)
                age_days = max(0.0, (now - updated).total_seconds() / 86400)
            except ValueError:
                age_days = 30.0
            recency = math.exp(-age_days / 120)
            score = memory.importance * 2.0 + overlap * 3.0 + recency
            scored.append((score, memory))

        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [memory for _score, memory in scored[: max(1, int(limit))]]

    def delete_memory(self, memory_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))

    def clear_memories(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM memories")

    def clear_conversation(self, conversation_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM messages WHERE conversation_id = ?",
                (int(conversation_id),),
            )


def _terms(text: str) -> set[str]:
    lowered = text.casefold()
    terms = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return {term for term in terms if len(term) >= 2}


def _event_importance(kind: str) -> int:
    if kind in {
        "setting_change",
        "command_failed",
        "qq_reply",
        "qq_proactive",
        "qq_space_draft",
    }:
        return 3
    if kind in {"context_menu", "poke", "drag", "chat_open", "random_noop"}:
        return 1
    return 2


def _qq_profile_signal(text: str) -> str:
    signals: list[str] = []
    patterns = (
        (r"我(?:很|最|比较)?喜欢([^，。；！？,.;!?]{1,40})", "表达过喜欢"),
        (r"我(?:最近|正在|目前在)([^，。；！？,.;!?]{1,50})", "近期在做"),
        (r"我想([^，。；！？,.;!?]{1,50})", "表达过想法"),
    )
    for pattern, label in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip(" ，,：:")
            if value:
                signals.append(f"{label}：{value}")
        if len(signals) >= 2:
            break
    return "；".join(signals)


def _append_persona_value(existing: str, value: str) -> str:
    items = [
        item.strip()
        for item in existing.split("；")
        if item.strip()
    ]
    if value in items:
        return existing
    items.append(value)
    return "；".join(items[-24:])[:4000]

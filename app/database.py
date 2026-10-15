from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiosqlite

from app.domain import CaseStatus, InvalidTransition, normalize_case_id, validate_transition
from app.models import (
    AuditEventRecord,
    CaseRecord,
    CaseSummary,
    DraftData,
    DraftRecord,
    MessageRecord,
    PendingReceiptRecord,
    SessionRecord,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path, timeout=30)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 30000")
        await connection.execute("PRAGMA secure_delete = ON")
        if self.path != ":memory:":
            await connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        async with self.connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    first_seen_at TEXT NOT NULL,
                    first_name TEXT,
                    last_name TEXT,
                    username TEXT
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                    telegram_id INTEGER PRIMARY KEY,
                    window_started_at REAL NOT NULL,
                    last_update_at REAL NOT NULL,
                    update_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rate_limits_window ON rate_limits(window_started_at);
                CREATE TABLE IF NOT EXISTS cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_number TEXT UNIQUE,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    retention_hold INTEGER NOT NULL DEFAULT 0 CHECK(retention_hold IN (0, 1)),
                    status TEXT NOT NULL,
                    have_options TEXT NOT NULL,
                    need_options TEXT NOT NULL,
                    have_values TEXT NOT NULL DEFAULT '{}',
                    requested_values TEXT NOT NULL DEFAULT '{}',
                    conversation_status TEXT NOT NULL DEFAULT 'PENDING',
                    other_request TEXT,
                    description TEXT
                );
                CREATE INDEX IF NOT EXISTS cases_status_created ON cases(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS cases_closed_at ON cases(closed_at);
                CREATE TABLE IF NOT EXISTS drafts (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    step TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    telegram_id INTEGER PRIMARY KEY,
                    case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
                    mode TEXT,
                    context TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    sender_kind TEXT NOT NULL CHECK(sender_kind IN ('user', 'admin')),
                    media_type TEXT NOT NULL,
                    text TEXT,
                    file_id TEXT,
                    source_chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    destination_message_id INTEGER,
                    delivery_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_chat_id, source_message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_case_created ON messages(case_id, created_at);
                CREATE TABLE IF NOT EXISTS case_reply_targets (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    PRIMARY KEY(chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    amount INTEGER,
                    expected_amount INTEGER,
                    declared_amount INTEGER,
                    currency TEXT,
                    method TEXT,
                    instructions_message_id INTEGER,
                    receipt_message_id INTEGER,
                    status TEXT NOT NULL,
                    reviewed_by INTEGER,
                    reviewed_at TEXT,
                    result_note TEXT,
                    admin_note TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS payments_status ON payments(status, created_at);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
                    actor_id INTEGER NOT NULL,
                    actor_kind TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    old_status TEXT,
                    new_status TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_case_created ON audit_events(case_id, created_at);
                CREATE TABLE IF NOT EXISTS consent_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
                    basis TEXT NOT NULL,
                    notice_version TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )
            cursor = await db.execute("PRAGMA table_info(cases)")
            columns = {row["name"] for row in await cursor.fetchall()}
            if "retention_hold" not in columns:
                await db.execute(
                    "ALTER TABLE cases ADD COLUMN retention_hold INTEGER NOT NULL DEFAULT 0"
                )
            if "requested_values" not in columns:
                await db.execute(
                    "ALTER TABLE cases ADD COLUMN requested_values TEXT NOT NULL DEFAULT '{}'"
                )
            if "have_values" not in columns:
                await db.execute(
                    "ALTER TABLE cases ADD COLUMN have_values TEXT NOT NULL DEFAULT '{}'"
                )
            if "conversation_status" not in columns:
                await db.execute(
                    "ALTER TABLE cases ADD COLUMN conversation_status TEXT NOT NULL DEFAULT 'PENDING'"
                )
            cursor = await db.execute("PRAGMA table_info(users)")
            user_columns = {row["name"] for row in await cursor.fetchall()}
            for column in ("first_name", "last_name", "username"):
                if column not in user_columns:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
            await db.execute(
                "UPDATE cases SET conversation_status='CLOSED' WHERE status IN (?, ?, ?, ?) "
                "AND conversation_status='PENDING'",
                (
                    CaseStatus.REJECTED.value,
                    CaseStatus.COMPLETED.value,
                    CaseStatus.CANCELLED.value,
                    CaseStatus.EXPIRED.value,
                ),
            )
            await db.execute(
                "UPDATE cases SET conversation_status='OPEN' WHERE conversation_status='PENDING' "
                "AND EXISTS (SELECT 1 FROM sessions s JOIN users u ON u.telegram_id=s.telegram_id "
                "WHERE s.mode='conversation' AND u.id=cases.user_id)"
            )
            cursor = await db.execute("PRAGMA table_info(sessions)")
            session_columns = {row["name"] for row in await cursor.fetchall()}
            if "context" not in session_columns:
                await db.execute(
                    "ALTER TABLE sessions ADD COLUMN context TEXT NOT NULL DEFAULT '{}'"
                )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS messages_destination_id "
                "ON messages(sender_kind, destination_message_id)"
            )
            cursor = await db.execute("PRAGMA table_info(payments)")
            payment_columns = {row["name"] for row in await cursor.fetchall()}
            for column in ("expected_amount", "declared_amount", "admin_note"):
                if column not in payment_columns:
                    await db.execute(f"ALTER TABLE payments ADD COLUMN {column} INTEGER" if column != "admin_note" else "ALTER TABLE payments ADD COLUMN admin_note TEXT")
            await db.commit()

    async def allow_update(
        self,
        telegram_id: int,
        now: float,
        *,
        max_updates: int = 30,
        window_seconds: float = 60.0,
        min_interval_seconds: float = 0.35,
    ) -> bool:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT window_started_at, last_update_at, update_count "
                "FROM rate_limits WHERE telegram_id=?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
            if not row or now - row["window_started_at"] >= window_seconds:
                await db.execute(
                    "INSERT INTO rate_limits(telegram_id, window_started_at, last_update_at, update_count) "
                    "VALUES (?, ?, ?, 1) ON CONFLICT(telegram_id) DO UPDATE SET "
                    "window_started_at=excluded.window_started_at, "
                    "last_update_at=excluded.last_update_at, update_count=1",
                    (telegram_id, now, now),
                )
                allowed = True
            else:
                allowed = (
                    now >= row["last_update_at"]
                    and now - row["last_update_at"] >= min_interval_seconds
                    and row["update_count"] < max_updates
                )
                if allowed:
                    await db.execute(
                        "UPDATE rate_limits SET last_update_at=?, update_count=update_count+1 "
                        "WHERE telegram_id=?",
                        (now, telegram_id),
                    )
            await db.execute(
                "DELETE FROM rate_limits WHERE window_started_at < ?",
                (now - 2 * window_seconds,),
            )
            await db.commit()
        return allowed

    async def register_user(
        self,
        telegram_id: int,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
    ) -> int:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO users (telegram_id, first_seen_at) VALUES (?, ?) "
                "ON CONFLICT(telegram_id) DO NOTHING",
                (telegram_id, utc_now()),
            )
            if any(value is not None for value in (first_name, last_name, username)):
                await db.execute(
                    "UPDATE users SET first_name=?, last_name=?, username=? WHERE telegram_id=?",
                    (first_name, last_name, username, telegram_id),
                )
            cursor = await db.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            row = await cursor.fetchone()
            await db.commit()
            return int(row["id"])

    async def save_draft(self, user_id: int, step: str, data: DraftData) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO drafts(user_id, step, data, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET step=excluded.step, data=excluded.data, updated_at=excluded.updated_at",
                (user_id, step, json.dumps(data, ensure_ascii=False), utc_now()),
            )
            await db.commit()

    async def get_draft(self, telegram_id: int) -> DraftRecord | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT d.step, d.data FROM drafts d JOIN users u ON u.id=d.user_id WHERE u.telegram_id=?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return cast(DraftRecord, {"step": row["step"], "data": json.loads(row["data"])})

    async def delete_draft(self, user_id: int) -> None:
        async with self.connect() as db:
            await db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))
            await db.commit()

    async def create_case(self, telegram_id: int, data: DraftData) -> CaseRecord:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO users(telegram_id, first_seen_at) VALUES (?, ?) ON CONFLICT(telegram_id) DO NOTHING",
                (telegram_id, now),
            )
            cursor = await db.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,))
            user_id = int((await cursor.fetchone())["id"])
            case_id = await self._insert_case(db, user_id, data, now)
            await db.commit()
        case = await self.get_case(case_id)
        if case is None:
            raise LookupError("پرونده پس از ثبت پیدا نشد")
        return case

    async def submit_draft(self, telegram_id: int) -> CaseRecord | None:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT u.id, d.step, d.data FROM users u JOIN drafts d ON d.user_id=u.id "
                "WHERE u.telegram_id=?",
                (telegram_id,),
            )
            draft = await cursor.fetchone()
            if not draft or draft["step"] != "ready":
                await db.rollback()
                return None
            user_id = int(draft["id"])
            data = cast(DraftData, json.loads(draft["data"]))
            have_fields = data.get("have_fields", data.get("have", []))
            have_values = data.get("have_values", {})
            fields = data.get("requested_fields", data.get("need", []))
            values = data.get("requested_values", {})
            required_values = [(have_fields, have_values)]
            if any(
                not isinstance(field_values.get(field), str) or not field_values[field].strip()
                for selected_fields, field_values in required_values
                for field in selected_fields
            ):
                await db.rollback()
                return None
            data["have_fields"] = have_fields
            data["have_values"] = have_values
            data["requested_fields"] = fields
            data["requested_values"] = values
            case_id = await self._insert_case(db, user_id, data, now)
            await db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))
            await db.commit()
        case = await self.get_case(case_id)
        if case is None:
            raise LookupError("پرونده پس از ثبت پیدا نشد")
        return case

    async def _insert_case(
        self, db: aiosqlite.Connection, user_id: int, data: DraftData, now: str
    ) -> int:
        cursor = await db.execute(
            "INSERT INTO cases(user_id, created_at, updated_at, status, have_options, need_options, "
            "have_values, requested_values, other_request, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                now,
                now,
                CaseStatus.NEW.value,
                json.dumps(data.get("have_fields", data.get("have", [])), ensure_ascii=False),
                json.dumps(data.get("requested_fields", data.get("need", [])), ensure_ascii=False),
                json.dumps(data.get("have_values", {}), ensure_ascii=False),
                json.dumps(data.get("requested_values", {}), ensure_ascii=False),
                data.get("other_request"),
                data.get("description", ""),
            ),
        )
        case_id = int(cursor.lastrowid)
        await db.execute(
            "UPDATE cases SET case_number=? WHERE id=?", (f"R-{case_id:06d}", case_id)
        )
        await db.execute(
            "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, new_status, created_at) "
            "VALUES (?, ?, 'user', 'CASE_CREATED', ?, ?)",
            (case_id, user_id, CaseStatus.NEW.value, now),
        )
        if data.get("notice_version"):
            await db.execute(
                "INSERT INTO consent_records(user_id, case_id, basis, notice_version, recorded_at) "
                "VALUES (?, ?, 'notice_acknowledged', ?, ?)",
                (user_id, case_id, data["notice_version"], now),
            )
        return case_id

    async def get_case(self, case_id: int) -> CaseRecord | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT c.*, u.telegram_id, u.first_name, u.last_name, u.username "
                "FROM cases c LEFT JOIN users u ON u.id=c.user_id WHERE c.id=?",
                (case_id,),
            )
            row = await cursor.fetchone()
        return cast(CaseRecord, dict(row)) if row else None

    async def get_case_for_reply(
        self,
        sender_kind: str,
        destination_message_id: int,
        owner_telegram_id: int | None = None,
    ) -> CaseRecord | None:
        query = (
            "SELECT c.*, u.telegram_id, u.first_name, u.last_name, u.username "
            "FROM messages m JOIN cases c ON c.id=m.case_id "
            "LEFT JOIN users u ON u.id=c.user_id "
            "WHERE m.sender_kind=? AND m.destination_message_id=?"
        )
        parameters: list[Any] = [sender_kind, destination_message_id]
        if owner_telegram_id is not None:
            query += " AND u.telegram_id=?"
            parameters.append(owner_telegram_id)
        async with self.connect() as db:
            cursor = await db.execute(query, parameters)
            row = await cursor.fetchone()
        return cast(CaseRecord, dict(row)) if row else None

    async def save_request_reply_target(self, case_id: int, chat_id: int, message_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT OR REPLACE INTO case_reply_targets(chat_id, message_id, case_id) VALUES (?, ?, ?)",
                (chat_id, message_id, case_id),
            )
            await db.commit()

    async def get_case_for_admin_reply(self, chat_id: int, message_id: int) -> CaseRecord | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT c.*, u.telegram_id, u.first_name, u.last_name, u.username "
                "FROM case_reply_targets r JOIN cases c ON c.id=r.case_id "
                "JOIN users u ON u.id=c.user_id WHERE r.chat_id=? AND r.message_id=?",
                (chat_id, message_id),
            )
            row = await cursor.fetchone()
        return cast(CaseRecord, dict(row)) if row else None

    async def get_user_case(self, case_id: int, telegram_id: int) -> CaseRecord | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT c.*, u.telegram_id, u.first_name, u.last_name, u.username "
                "FROM cases c JOIN users u ON u.id=c.user_id "
                "WHERE c.id=? AND u.telegram_id=?",
                (case_id, telegram_id),
            )
            row = await cursor.fetchone()
        return cast(CaseRecord, dict(row)) if row else None

    async def list_user_cases(self, telegram_id: int) -> list[CaseSummary]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT c.id, c.case_number, c.status, c.created_at FROM cases c "
                "JOIN users u ON u.id=c.user_id WHERE u.telegram_id=? "
                "AND c.status NOT IN (?, ?, ?, ?) ORDER BY c.created_at DESC",
                (
                    telegram_id,
                    CaseStatus.REJECTED.value,
                    CaseStatus.COMPLETED.value,
                    CaseStatus.CANCELLED.value,
                    CaseStatus.EXPIRED.value,
                ),
            )
            return [cast(CaseSummary, dict(row)) for row in await cursor.fetchall()]

    async def get_case_by_number(
        self, case_number: str, owner_telegram_id: int | None = None
    ) -> CaseRecord | None:
        query = (
            "SELECT c.*, u.telegram_id, u.first_name, u.last_name, u.username "
            "FROM cases c LEFT JOIN users u ON u.id=c.user_id WHERE c.case_number=?"
        )
        parameters: list[Any] = [case_number]
        if owner_telegram_id is not None:
            query += " AND u.telegram_id=?"
            parameters.append(owner_telegram_id)
        async with self.connect() as db:
            cursor = await db.execute(query, parameters)
            row = await cursor.fetchone()
        return cast(CaseRecord, dict(row)) if row else None

    async def list_cases(
        self,
        statuses: list[CaseStatus] | None,
        limit: int,
        offset: int,
        conversation_status: str | None = None,
    ) -> list[CaseSummary]:
        query = (
            "SELECT c.id, c.case_number, c.status, c.created_at, c.conversation_status, "
            "u.telegram_id, u.first_name, u.last_name, u.username, "
            "(SELECT MAX(m.created_at) FROM messages m WHERE m.case_id=c.id) AS last_message_at, "
            "(SELECT COALESCE(m.text, '[' || m.media_type || ']') FROM messages m "
            "WHERE m.case_id=c.id ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message "
            "FROM cases c LEFT JOIN users u ON u.id=c.user_id"
        )
        conditions: list[str] = []
        parameters: list[Any] = []
        if statuses:
            conditions.append(f"c.status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(status.value for status in statuses)
        if conversation_status:
            conditions.append("c.conversation_status=?")
            parameters.append(conversation_status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
        async with self.connect() as db:
            cursor = await db.execute(query, parameters)
            return [cast(CaseSummary, dict(row)) for row in await cursor.fetchall()]

    async def count_cases(
        self,
        statuses: list[CaseStatus] | None = None,
        conversation_status: str | None = None,
    ) -> int:
        query = "SELECT COUNT(*) AS count FROM cases"
        parameters: list[Any] = []
        conditions: list[str] = []
        if statuses:
            conditions.append(f"status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(status.value for status in statuses)
        if conversation_status:
            conditions.append("conversation_status=?")
            parameters.append(conversation_status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        async with self.connect() as db:
            cursor = await db.execute(query, parameters)
            return int((await cursor.fetchone())["count"])

    async def search_cases(self, query_text: str, limit: int = 10) -> list[CaseSummary]:
        normalized = normalize_case_id(query_text)
        if normalized.isdigit():
            normalized = f"R-{int(normalized):06d}"
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT id, case_number, status, created_at FROM cases WHERE case_number LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (f"%{normalized}%", limit),
            )
            return [cast(CaseSummary, dict(row)) for row in await cursor.fetchall()]

    async def set_session(
        self,
        telegram_id: int,
        case_id: int | None,
        mode: str | None,
        context: dict[str, Any] | None = None,
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO sessions(telegram_id, case_id, mode, context, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET case_id=excluded.case_id, mode=excluded.mode, "
                "context=excluded.context, updated_at=excluded.updated_at",
                (telegram_id, case_id, mode, json.dumps(context or {}), utc_now()),
            )
            await db.commit()

    async def start_conversation(self, case_id: int, admin_id: int) -> bool:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT user_id, conversation_status FROM cases WHERE id=?", (case_id,)
            )
            case = await cursor.fetchone()
            if not case or case["user_id"] is None:
                await db.rollback()
                return False
            cursor = await db.execute(
                "SELECT id FROM cases WHERE user_id=? AND id!=? "
                "AND conversation_status IN ('STARTING', 'OPEN') LIMIT 1",
                (case["user_id"], case_id),
            )
            if await cursor.fetchone():
                await db.rollback()
                return False
            if case["conversation_status"] != "PENDING":
                await db.rollback()
                return False
            await db.execute(
                "UPDATE cases SET conversation_status='STARTING', updated_at=? WHERE id=?",
                (now, case_id),
            )
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, created_at) "
                "VALUES (?, ?, 'admin', 'CONVERSATION_STARTING', ?)",
                (case_id, admin_id, now),
            )
            await db.commit()
            return True

    async def cancel_conversation_start(self, case_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE cases SET conversation_status='PENDING' "
                "WHERE id=? AND conversation_status='STARTING'",
                (case_id,),
            )
            await db.commit()

    async def activate_conversation(self, case_id: int, admin_id: int) -> bool:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE cases SET conversation_status='OPEN', updated_at=? "
                "WHERE id=? AND conversation_status='STARTING'",
                (now, case_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, created_at) "
                "VALUES (?, ?, 'admin', 'CONVERSATION_OPENED', ?)",
                (case_id, admin_id, now),
            )
            await db.commit()
            return True

    async def end_conversation(self, case_id: int, admin_id: int) -> bool:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT user_id FROM cases WHERE id=? AND conversation_status IN ('STARTING', 'OPEN')",
                (case_id,),
            )
            case = await cursor.fetchone()
            if not case:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE cases SET conversation_status='CLOSED', updated_at=? WHERE id=?",
                (now, case_id),
            )
            await db.execute(
                "UPDATE sessions SET mode='closed', context='{}', updated_at=? WHERE case_id=?",
                (now, case_id),
            )
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, created_at) "
                "VALUES (?, ?, 'admin', 'CONVERSATION_CLOSED', ?)",
                (case_id, admin_id, now),
            )
            await db.commit()
            return True

    async def user_has_active_conversation(self, telegram_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT 1 FROM sessions s JOIN cases c ON c.id=s.case_id "
                "JOIN users u ON u.telegram_id=s.telegram_id "
                "WHERE s.telegram_id=? AND s.mode='conversation' "
                "AND c.conversation_status='OPEN' AND u.id=c.user_id LIMIT 1",
                (telegram_id,),
            )
            return await cursor.fetchone() is not None

    async def get_session(self, telegram_id: int) -> SessionRecord | None:
        async with self.connect() as db:
            cursor = await db.execute("SELECT * FROM sessions WHERE telegram_id=?", (telegram_id,))
            row = await cursor.fetchone()
        if not row:
            return None
        session = dict(row)
        session["context"] = json.loads(session["context"] or "{}")
        return cast(SessionRecord, session)

    async def transition_case(
        self, case_id: int, target: CaseStatus, actor_id: int, actor_kind: str = "admin"
    ) -> CaseRecord:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT status FROM cases WHERE id=?", (case_id,))
            row = await cursor.fetchone()
            if not row:
                raise LookupError("پرونده پیدا نشد")
            current = CaseStatus(row["status"])
            validate_transition(current, target)
            closed_at = now if target in {CaseStatus.REJECTED, CaseStatus.COMPLETED, CaseStatus.CANCELLED} else None
            await db.execute(
                "UPDATE cases SET status=?, updated_at=?, closed_at=? WHERE id=?",
                (target.value, now, closed_at, case_id),
            )
            if target in {CaseStatus.REJECTED, CaseStatus.COMPLETED, CaseStatus.CANCELLED}:
                await db.execute(
                    "UPDATE cases SET conversation_status='CLOSED' WHERE id=?", (case_id,)
                )
                await db.execute(
                    "UPDATE sessions SET mode='closed', context='{}', updated_at=? WHERE case_id=?",
                    (now, case_id),
                )
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, old_status, new_status, created_at) "
                "VALUES (?, ?, ?, 'STATUS_CHANGED', ?, ?, ?)",
                (case_id, actor_id, actor_kind, current.value, target.value, now),
            )
            await db.commit()
        case = await self.get_case(case_id)
        if case is None:
            raise LookupError("پرونده پس از تغییر پیدا نشد")
        return case

    async def set_retention_hold(self, case_id: int, enabled: bool, admin_id: int) -> CaseRecord:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT status, retention_hold FROM cases WHERE id=?", (case_id,))
            row = await cursor.fetchone()
            if not row:
                raise LookupError("پرونده پیدا نشد")
            if row["status"] == CaseStatus.EXPIRED.value:
                raise InvalidTransition("پروندهٔ منقضی‌شده قابل نگه‌داری نیست")
            await db.execute(
                "UPDATE cases SET retention_hold=?, updated_at=? WHERE id=?",
                (int(enabled), now, case_id),
            )
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, created_at) "
                "VALUES (?, ?, 'admin', ?, ?)",
                (case_id, admin_id, "RETENTION_HOLD_ENABLED" if enabled else "RETENTION_HOLD_DISABLED", now),
            )
            await db.commit()
        case = await self.get_case(case_id)
        if case is None:
            raise LookupError("پرونده پس از تغییر پیدا نشد")
        return case

    async def add_message(
        self,
        case_id: int,
        sender_kind: str,
        media_type: str,
        text: str | None,
        file_id: str | None,
        source_chat_id: int,
        source_message_id: int,
        delivery_state: str = "QUEUED",
    ) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO messages(case_id, sender_kind, media_type, text, file_id, source_chat_id, "
                "source_message_id, delivery_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (case_id, sender_kind, media_type, text, file_id, source_chat_id, source_message_id, delivery_state, utc_now()),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def update_message_delivery(
        self, source_chat_id: int, source_message_id: int, state: str, destination_message_id: int | None = None
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE messages SET delivery_state=?, destination_message_id=? WHERE source_chat_id=? AND source_message_id=?",
                (state, destination_message_id, source_chat_id, source_message_id),
            )
            await db.commit()

    async def list_messages(self, case_id: int, limit: int = 30) -> list[MessageRecord]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM messages WHERE case_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (case_id, limit),
            )
            return [cast(MessageRecord, dict(row)) for row in reversed(await cursor.fetchall())]

    async def record_receipt(
        self,
        case_id: int,
        source_chat_id: int,
        source_message_id: int,
        media_type: str,
        text: str | None,
        file_id: str,
    ) -> bool:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT status FROM cases WHERE id=?", (case_id,))
            row = await cursor.fetchone()
            if not row or row["status"] != CaseStatus.AWAITING_PAYMENT.value:
                await db.rollback()
                return False
            cursor = await db.execute(
                "INSERT OR IGNORE INTO messages(case_id, sender_kind, media_type, text, file_id, source_chat_id, "
                "source_message_id, delivery_state, created_at) VALUES (?, 'user', ?, ?, ?, ?, ?, 'QUEUED', ?)",
                (case_id, media_type, text, file_id, source_chat_id, source_message_id, now),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.execute(
                "INSERT INTO payments(case_id, receipt_message_id, status, created_at) VALUES (?, ?, 'REVIEW', ?)",
                (case_id, source_message_id, now),
            )
            await db.execute(
                "UPDATE cases SET status=?, updated_at=?, closed_at=NULL WHERE id=?",
                (CaseStatus.RECEIPT_REVIEW.value, now, case_id),
            )
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, old_status, new_status, created_at) "
                "VALUES (?, ?, 'user', 'RECEIPT_SUBMITTED', ?, ?, ?)",
                (case_id, source_chat_id, CaseStatus.AWAITING_PAYMENT.value, CaseStatus.RECEIPT_REVIEW.value, now),
            )
            await db.commit()
        return True

    async def add_payment_instruction(
        self,
        case_id: int,
        instruction_message_id: int,
        admin_id: int,
        expected_amount: int | None = None,
        declared_amount: int | None = None,
        currency: str | None = None,
        method: str | None = None,
    ) -> None:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT status FROM cases WHERE id=?", (case_id,))
            row = await cursor.fetchone()
            if not row:
                raise LookupError("پرونده پیدا نشد")
            current = CaseStatus(row["status"])
            validate_transition(current, CaseStatus.AWAITING_PAYMENT)
            await db.execute(
                "INSERT INTO payments(case_id, amount, expected_amount, declared_amount, currency, method, "
                "instructions_message_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'AWAITING', ?)",
                (
                    case_id,
                    expected_amount,
                    expected_amount,
                    declared_amount,
                    currency,
                    method,
                    instruction_message_id,
                    now,
                ),
            )
            await db.execute(
                "UPDATE cases SET status=?, updated_at=?, closed_at=NULL WHERE id=?",
                (CaseStatus.AWAITING_PAYMENT.value, now, case_id),
            )
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, old_status, new_status, created_at) "
                "VALUES (?, ?, 'admin', 'PAYMENT_INSTRUCTIONS_SENT', ?, ?, ?)",
                (case_id, admin_id, current.value, CaseStatus.AWAITING_PAYMENT.value, now),
            )
            await db.commit()

    async def get_pending_receipt(self, case_id: int) -> PendingReceiptRecord | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT m.*, p.admin_note FROM payments p JOIN messages m "
                "ON m.case_id=p.case_id AND m.source_message_id=p.receipt_message_id "
                "WHERE p.case_id=? AND p.status='REVIEW' ORDER BY p.id DESC LIMIT 1",
                (case_id,),
            )
            row = await cursor.fetchone()
        return cast(PendingReceiptRecord, dict(row)) if row else None

    async def set_receipt_note(self, case_id: int, note: str, admin_id: int) -> None:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT id FROM payments WHERE case_id=? AND status='REVIEW' ORDER BY id DESC LIMIT 1",
                (case_id,),
            )
            payment = await cursor.fetchone()
            if not payment:
                raise LookupError("رسید در انتظار بررسی پیدا نشد")
            await db.execute(
                "UPDATE payments SET admin_note=? WHERE id=?", (note, payment["id"])
            )
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, created_at) "
                "VALUES (?, ?, 'admin', 'RECEIPT_NOTE_UPDATED', ?)",
                (case_id, admin_id, now),
            )
            await db.commit()

    async def review_receipt(self, case_id: int, approved: bool, admin_id: int, note: str | None = None) -> CaseRecord:
        target = CaseStatus.PAYMENT_CONFIRMED if approved else CaseStatus.AWAITING_PAYMENT
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT status FROM cases WHERE id=?", (case_id,))
            row = await cursor.fetchone()
            if not row:
                raise LookupError("پرونده پیدا نشد")
            current = CaseStatus(row["status"])
            validate_transition(current, target)
            cursor = await db.execute(
                "SELECT id FROM payments WHERE case_id=? AND status='REVIEW' ORDER BY id DESC LIMIT 1", (case_id,)
            )
            payment = await cursor.fetchone()
            if not payment:
                raise LookupError("رسید در انتظار بررسی پیدا نشد")
            await db.execute(
                "UPDATE payments SET status=?, reviewed_by=?, reviewed_at=?, result_note=? WHERE id=?",
                ("CONFIRMED" if approved else "REJECTED", admin_id, now, note, payment["id"]),
            )
            await db.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?", (target.value, now, case_id))
            await db.execute(
                "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, old_status, new_status, created_at) "
                "VALUES (?, ?, 'admin', 'RECEIPT_REVIEWED', ?, ?, ?)",
                (case_id, admin_id, current.value, target.value, now),
            )
            await db.commit()
        case = await self.get_case(case_id)
        if case is None:
            raise LookupError("پرونده پس از بررسی رسید پیدا نشد")
        return case

    async def record_consent(self, user_id: int, case_id: int, basis: str, notice_version: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO consent_records(user_id, case_id, basis, notice_version, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, case_id, basis, notice_version, utc_now()),
            )
            await db.commit()

    async def audit_history(self, case_id: int, limit: int = 30) -> list[AuditEventRecord]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT actor_id, actor_kind, event_type, old_status, new_status, created_at "
                "FROM audit_events WHERE case_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (case_id, limit),
            )
            return [cast(AuditEventRecord, dict(row)) for row in reversed(await cursor.fetchall())]

    async def purge_closed_cases(self, retention_hours: int) -> tuple[int, int]:
        cutoff = (datetime.now(UTC) - timedelta(hours=retention_hours)).isoformat(timespec="seconds")
        deleted = failed = 0
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT id, user_id, status FROM cases WHERE closed_at IS NOT NULL AND closed_at < ? "
                "AND status != ? AND retention_hold=0 ORDER BY id LIMIT 100",
                (cutoff, CaseStatus.EXPIRED.value),
            )
            candidates = await cursor.fetchall()
        for candidate in candidates:
            try:
                now = utc_now()
                async with self.connect() as db:
                    await db.execute("BEGIN IMMEDIATE")
                    cursor = await db.execute(
                        "SELECT user_id, status, closed_at, retention_hold FROM cases WHERE id=?",
                        (candidate["id"],),
                    )
                    current = await cursor.fetchone()
                    if (
                        not current
                        or current["retention_hold"]
                        or current["closed_at"] is None
                        or current["closed_at"] >= cutoff
                        or current["status"] == CaseStatus.EXPIRED.value
                    ):
                        await db.rollback()
                        continue
                    await db.execute("DELETE FROM messages WHERE case_id=?", (candidate["id"],))
                    await db.execute("DELETE FROM payments WHERE case_id=?", (candidate["id"],))
                    await db.execute("DELETE FROM consent_records WHERE case_id=?", (candidate["id"],))
                    await db.execute(
                        "UPDATE sessions SET case_id=NULL, mode=NULL, updated_at=? WHERE case_id=?", (now, candidate["id"])
                    )
                    await db.execute(
                        "UPDATE cases SET user_id=NULL, status=?, conversation_status='CLOSED', "
                        "have_options='[]', need_options='[]', have_values='{}', requested_values='{}', "
                        "other_request=NULL, description=NULL, updated_at=? WHERE id=?",
                        (CaseStatus.EXPIRED.value, now, candidate["id"]),
                    )
                    await db.execute(
                        "INSERT INTO audit_events(case_id, actor_id, actor_kind, event_type, old_status, new_status, created_at) "
                        "VALUES (?, 0, 'system', 'DATA_PURGED', ?, ?, ?)",
                        (candidate["id"], candidate["status"], CaseStatus.EXPIRED.value, now),
                    )
                    if current["user_id"] is not None:
                        cursor = await db.execute(
                            "SELECT telegram_id FROM users WHERE id=?", (current["user_id"],)
                        )
                        user = await cursor.fetchone()
                        cursor = await db.execute(
                            "SELECT 1 FROM cases WHERE user_id=? AND status != ? LIMIT 1",
                            (current["user_id"], CaseStatus.EXPIRED.value),
                        )
                        other_cases = await cursor.fetchone()
                        if user and not other_cases:
                            await db.execute("DELETE FROM sessions WHERE telegram_id=?", (user["telegram_id"],))
                        await db.execute(
                            "DELETE FROM users WHERE id=? AND NOT EXISTS "
                            "(SELECT 1 FROM cases WHERE user_id=? AND status != ?) "
                            "AND NOT EXISTS (SELECT 1 FROM drafts WHERE user_id=?)",
                            (current["user_id"], current["user_id"], CaseStatus.EXPIRED.value, current["user_id"]),
                        )
                    await db.commit()
                deleted += 1
            except Exception:
                failed += 1
        if deleted:
            try:
                async with self.connect() as db:
                    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                failed += 1
        return deleted, failed
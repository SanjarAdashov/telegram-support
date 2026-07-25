"""PostgreSQL storage for support requests, message history, and Telegram entities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True)
class SupportRequest:
    id: int
    user_id: int
    user_label: str
    chat_id: int
    chat_type: str
    source_message_id: int | None
    message_thread_id: int | None
    question: str
    language: str | None
    is_auto_answer: bool
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Employee:
    id: int
    first_name: str
    last_name: str
    position: str
    role: str
    login: str
    password_hash: str
    telegram_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EmployeePublic:
    id: int
    first_name: str
    last_name: str
    position: str
    role: str
    login: str
    telegram_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SupportMessage:
    id: int
    request_id: int
    author_id: int
    author_role: str
    body: str
    created_at: datetime


class SupportStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def init_schema(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS support_requests (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    user_label TEXT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    chat_type TEXT NOT NULL,
                    source_message_id BIGINT,
                    message_thread_id BIGINT,
                    question TEXT NOT NULL,
                    language TEXT,
                    is_auto_answer BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'answered', 'waiting_employee', 'closed')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS support_messages (
                    id BIGSERIAL PRIMARY KEY,
                    request_id BIGINT NOT NULL REFERENCES support_requests(id) ON DELETE CASCADE,
                    author_id BIGINT NOT NULL,
                    author_role TEXT NOT NULL CHECK (author_role IN ('user', 'bot', 'employee')),
                    body TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS support_requests_status_idx
                    ON support_requests(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS support_messages_request_idx
                    ON support_messages(request_id, created_at);
                ALTER TABLE support_requests
                    ADD COLUMN IF NOT EXISTS is_auto_answer BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE support_requests
                    ADD COLUMN IF NOT EXISTS source_message_id BIGINT;
                ALTER TABLE support_requests
                    ADD COLUMN IF NOT EXISTS message_thread_id BIGINT;
                CREATE INDEX IF NOT EXISTS support_requests_type_idx
                    ON support_requests(is_auto_answer, updated_at DESC);
                CREATE TABLE IF NOT EXISTS employees (
                    id BIGSERIAL PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    position TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'analyst', 'operator')),
                    login TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    telegram_id BIGINT UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE employees ADD COLUMN IF NOT EXISTS telegram_id BIGINT;
                CREATE UNIQUE INDEX IF NOT EXISTS employees_telegram_id_idx
                    ON employees(telegram_id) WHERE telegram_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS employees_role_idx ON employees(role);
                CREATE TABLE IF NOT EXISTS telegram_entities (
                    entity_type TEXT NOT NULL CHECK (entity_type IN ('user', 'group')),
                    entity_id BIGINT NOT NULL,
                    title TEXT NOT NULL,
                    username TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    is_blocked BOOLEAN NOT NULL DEFAULT FALSE,
                    blocked_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (entity_type, entity_id)
                );
                CREATE INDEX IF NOT EXISTS telegram_entities_type_idx
                    ON telegram_entities(entity_type, last_seen_at DESC);
                """
            )

    @staticmethod
    def _request(row: dict[str, Any] | None) -> SupportRequest | None:
        return SupportRequest(**row) if row else None

    @staticmethod
    def _message(row: dict[str, Any] | None) -> SupportMessage | None:
        return SupportMessage(**row) if row else None

    def create_request(
        self,
        user_id: int,
        user_label: str,
        chat_id: int,
        chat_type: str,
        question: str,
        source_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> SupportRequest:
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO support_requests(
                    user_id, user_label, chat_id, chat_type,
                    source_message_id, message_thread_id, question
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
                """,
                (user_id, user_label, chat_id, chat_type, source_message_id, message_thread_id, question),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO support_messages(request_id, author_id, author_role, body)
                VALUES (%s, %s, 'user', %s)
                """,
                (row["id"], user_id, question),
            )
        return self._request(row)

    def reply_to_request(self, request_id: int, author_id: int, body: str) -> SupportRequest | None:
        """Append an employee reply or create a new follow-up if the request is closed."""
        body = body.strip()
        if not body:
            return None
        with self.connect() as connection:
            parent = connection.execute("SELECT * FROM support_requests WHERE id=%s", (request_id,)).fetchone()
            if not parent:
                return None
            if parent["status"] == "closed":
                row = connection.execute(
                    """
                    INSERT INTO support_requests(
                        user_id, user_label, chat_id, chat_type,
                        source_message_id, message_thread_id, question, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'answered')
                    RETURNING *
                    """,
                    (
                        parent["user_id"], parent["user_label"], parent["chat_id"], parent["chat_type"],
                        parent["source_message_id"], parent["message_thread_id"],
                        f"Продолжение заявки №{parent['id']}",
                    ),
                ).fetchone()
            else:
                row = connection.execute(
                    "UPDATE support_requests SET status='answered', updated_at=NOW() WHERE id=%s RETURNING *",
                    (request_id,),
                ).fetchone()
            connection.execute(
                """
                INSERT INTO support_messages(request_id, author_id, author_role, body)
                VALUES (%s, %s, 'employee', %s)
                """,
                (row["id"], author_id, body),
            )
        return self._request(row)

    def add_message(self, request_id: int, author_id: int, author_role: str, body: str) -> SupportMessage:
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO support_messages(request_id, author_id, author_role, body)
                VALUES (%s, %s, %s, %s) RETURNING *
                """,
                (request_id, author_id, author_role, body),
            ).fetchone()
            connection.execute("UPDATE support_requests SET updated_at=NOW() WHERE id=%s", (request_id,))
        return self._message(row)

    def set_language(self, request_id: int, language: str | None) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE support_requests SET language=%s, updated_at=NOW() WHERE id=%s", (language, request_id))

    def set_auto_answer(self, request_id: int, is_auto_answer: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE support_requests SET is_auto_answer=%s, updated_at=NOW() WHERE id=%s",
                (is_auto_answer, request_id),
            )

    def set_status(self, request_id: int, status: str) -> bool:
        with self.connect() as connection:
            result = connection.execute("UPDATE support_requests SET status=%s, updated_at=NOW() WHERE id=%s", (status, request_id))
        return result.rowcount == 1

    def get(self, request_id: int) -> SupportRequest | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM support_requests WHERE id=%s", (request_id,)).fetchone()
        return self._request(row)

    def messages(self, request_id: int) -> list[SupportMessage]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM support_messages WHERE request_id=%s ORDER BY created_at, id", (request_id,)
            ).fetchall()
        return [self._message(row) for row in rows]

    def list(self, search: str = "", status: str = "", kind: str = "") -> list[SupportRequest]:
        query = "SELECT * FROM support_requests WHERE TRUE"
        params: list[Any] = []
        if kind == "auto":
            query += " AND is_auto_answer=TRUE"
        elif kind == "manual":
            query += " AND is_auto_answer=FALSE"
        if status:
            query += " AND status=%s"
            params.append(status)
        if search.strip():
            query += " AND (question ILIKE %s OR user_label ILIKE %s)"
            pattern = f"%{search.strip()}%"
            params.extend([pattern, pattern])
        query += " ORDER BY updated_at DESC, id DESC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._request(row) for row in rows]

    def latest_open_for_user(self, user_id: int) -> SupportRequest | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM support_requests
                WHERE user_id=%s AND status IN ('open', 'waiting_employee')
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return self._request(row)

    def get_employee(self, employee_id: int) -> Employee | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM employees WHERE id=%s", (employee_id,)).fetchone()
        return self._employee(row)

    def get_employee_by_login(self, login: str) -> Employee | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM employees WHERE login=%s", (login,)).fetchone()
        return self._employee(row)

    def get_employee_by_telegram_id(self, telegram_id: int) -> Employee | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM employees WHERE telegram_id=%s", (telegram_id,)).fetchone()
        return self._employee(row)

    def list_employees(self) -> list[EmployeePublic]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, first_name, last_name, position, role, login, telegram_id, created_at, updated_at FROM employees ORDER BY last_name, first_name, id"
            ).fetchall()
        return [self._employee_public(row) for row in rows]

    def get_employee_public(self, employee_id: int) -> EmployeePublic | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, first_name, last_name, position, role, login, telegram_id, created_at, updated_at FROM employees WHERE id=%s",
                (employee_id,),
            ).fetchone()
        return self._employee_public(row)

    def create_employee(self, first_name: str, last_name: str, position: str, role: str, login: str, password_hash: str, telegram_id: int | None = None) -> EmployeePublic:
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO employees(first_name,last_name,position,role,login,password_hash,telegram_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (first_name, last_name, position, role, login, password_hash, telegram_id),
            ).fetchone()
        return self._employee_public(row)

    def update_employee(self, employee_id: int, first_name: str, last_name: str, position: str, role: str, login: str, password_hash: str | None = None, telegram_id: int | None = None) -> EmployeePublic | None:
        fields = ["first_name=%s", "last_name=%s", "position=%s", "role=%s", "login=%s", "telegram_id=%s"]
        params: list[Any] = [first_name, last_name, position, role, login, telegram_id]
        if password_hash:
            fields.append("password_hash=%s")
            params.append(password_hash)
        params.append(employee_id)
        query = f"UPDATE employees SET {', '.join(fields)}, updated_at=NOW() WHERE id=%s RETURNING *"
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._employee_public(row)

    def delete_employee(self, employee_id: int) -> bool:
        with self.connect() as connection:
            result = connection.execute("DELETE FROM employees WHERE id=%s", (employee_id,))
        return result.rowcount == 1

    @staticmethod
    def _employee(row: dict[str, Any] | None) -> Employee | None:
        return Employee(**row) if row else None

    @staticmethod
    def _employee_public(row: dict[str, Any] | None) -> EmployeePublic | None:
        if not row:
            return None
        return EmployeePublic(**{key: row[key] for key in EmployeePublic.__dataclass_fields__})

    def register_entity(
        self,
        entity_type: str,
        entity_id: int,
        title: str,
        username: str | None = None,
        is_active: bool = True,
    ) -> None:
        if entity_type not in {"user", "group"}:
            raise ValueError("entity_type must be user or group")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_entities(entity_type, entity_id, title, username, is_active)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                    title=EXCLUDED.title, username=EXCLUDED.username,
                    is_active=EXCLUDED.is_active, last_seen_at=NOW()
                """,
                (entity_type, entity_id, title or str(entity_id), username, is_active),
            )

    def set_entity_active(self, entity_type: str, entity_id: int, is_active: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE telegram_entities SET is_active=%s, last_seen_at=NOW()
                WHERE entity_type=%s AND entity_id=%s
                """,
                (is_active, entity_type, entity_id),
            )

    def is_entity_blocked(self, entity_type: str, entity_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT is_blocked FROM telegram_entities WHERE entity_type=%s AND entity_id=%s",
                (entity_type, entity_id),
            ).fetchone()
        return bool(row and row["is_blocked"])

    def set_entity_blocked(self, entity_type: str, entity_id: int, blocked: bool) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE telegram_entities
                SET is_blocked=%s, blocked_at=CASE WHEN %s THEN NOW() ELSE NULL END
                WHERE entity_type=%s AND entity_id=%s
                """,
                (blocked, blocked, entity_type, entity_id),
            )
        return result.rowcount == 1

    def list_entities(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM telegram_entities"
        params: list[Any] = []
        if entity_type:
            query += " WHERE entity_type=%s"
            params.append(entity_type)
        query += " ORDER BY entity_type, last_seen_at DESC"
        with self.connect() as connection:
            return list(connection.execute(query, params).fetchall())

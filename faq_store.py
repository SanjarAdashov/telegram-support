"""PostgreSQL storage for the support knowledge base."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

DEFAULT_FAQ = {
    "какие часы работы": "Мы работаем ежедневно с 09:00 до 18:00 по будням.",
    "как оформить заказ": "Напишите, какой товар вам нужен, и оператор поможет оформить заказ.",
    "как вернуть товар": "Для возврата сохраните товарный вид и напишите оператору номер заказа.",
}


@dataclass(frozen=True)
class FAQItem:
    id: int
    question: str
    answer: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class FAQStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def init_schema(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS faq_items (
                    id BIGSERIAL PRIMARY KEY,
                    question TEXT NOT NULL UNIQUE,
                    answer TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

    def seed_defaults(self) -> None:
        with self.connect() as connection:
            for question, answer in DEFAULT_FAQ.items():
                connection.execute(
                    """
                    INSERT INTO faq_items(question, answer)
                    VALUES (%s, %s)
                    ON CONFLICT (question) DO NOTHING
                    """,
                    (question, answer),
                )

    @staticmethod
    def _item(row: dict[str, Any] | None) -> FAQItem | None:
        return FAQItem(**row) if row else None

    def list(self, search: str = "", include_inactive: bool = True) -> list[FAQItem]:
        query = "SELECT * FROM faq_items WHERE TRUE"
        params: list[Any] = []
        if not include_inactive:
            query += " AND is_active = TRUE"
        if search.strip():
            query += " AND (question ILIKE %s OR answer ILIKE %s)"
            pattern = f"%{search.strip()}%"
            params.extend([pattern, pattern])
        query += " ORDER BY updated_at DESC, id DESC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._item(row) for row in rows]

    def get(self, item_id: int) -> FAQItem | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM faq_items WHERE id=%s", (item_id,)).fetchone()
        return self._item(row)

    def create(self, question: str, answer: str, is_active: bool = True) -> FAQItem:
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO faq_items(question, answer, is_active)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (question.strip(), answer.strip(), is_active),
            ).fetchone()
        return self._item(row)

    def update(self, item_id: int, question: str, answer: str, is_active: bool) -> FAQItem | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE faq_items
                SET question=%s, answer=%s, is_active=%s, updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (question.strip(), answer.strip(), is_active, item_id),
            ).fetchone()
        return self._item(row)

    def delete(self, item_id: int) -> bool:
        with self.connect() as connection:
            result = connection.execute("DELETE FROM faq_items WHERE id=%s", (item_id,))
        return result.rowcount == 1

    def knowledge(self) -> dict[str, str]:
        return {item.question: item.answer for item in self.list(include_inactive=False)}

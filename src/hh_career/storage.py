import asyncio
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from hh_career.models import (
    STATUS_DRAFT,
    STATUS_NOTIFIED,
    Resume,
    VacancyDetails,
    VacancyRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    salary TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    score INTEGER,
    resume_hash TEXT,
    resume_title TEXT,
    match_reason TEXT,
    short_description TEXT,
    letter TEXT,
    filter_name TEXT,
    test_required INTEGER NOT NULL DEFAULT 0,
    preview_chat_id INTEGER,
    preview_message_id INTEGER,
    preview_overflow_message_id INTEGER,
    apply_error TEXT,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS resumes (
    hash TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, declaration: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    if any(row[1] == column for row in rows):
        return
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Storage:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> "Storage":
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await _ensure_column(db, "vacancies", "salary", "TEXT")
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def is_known(self, vacancy_id: str) -> bool:
        async with self._lock:
            cursor = await self._db.execute("SELECT 1 FROM vacancies WHERE id = ?", (vacancy_id,))
            row = await cursor.fetchone()
            return row is not None

    async def remember(
        self,
        details: VacancyDetails,
        *,
        status: str,
        score: int,
        resume_hash: str,
        resume_title: str,
        reason: str,
        short_description: str,
        filter_name: str,
        description: str,
    ) -> VacancyRecord:
        async with self._lock:
            now = utcnow()
            await self._db.execute(
                """
                INSERT INTO vacancies (
                    id, url, title, company, salary, description, status, score, resume_hash,
                    resume_title, match_reason, short_description, filter_name,
                    test_required, first_seen, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    details.id,
                    details.url,
                    details.title,
                    details.company,
                    details.salary,
                    description,
                    status,
                    score,
                    resume_hash,
                    resume_title,
                    reason,
                    short_description,
                    filter_name,
                    int(details.test_required),
                    now,
                    now,
                ),
            )
            await self._db.commit()
            record = await self._get(details.id)
            if record is None:
                raise RuntimeError(f"Вакансия {details.id} не сохранилась")
            return record

    async def get(self, vacancy_id: str) -> VacancyRecord | None:
        async with self._lock:
            return await self._get(vacancy_id)

    async def list_by_status(self, status: str) -> list[VacancyRecord]:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT * FROM vacancies WHERE status = ? ORDER BY first_seen",
                (status,),
            )
            rows = await cursor.fetchall()
            return [_record(row) for row in rows]

    async def set_status(self, vacancy_id: str, status: str) -> None:
        async with self._lock:
            await self._set_status(vacancy_id, status)
            await self._db.commit()

    async def save_letter(self, vacancy_id: str, letter: str) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE vacancies SET letter = ?, updated_at = ? WHERE id = ?",
                (letter, utcnow(), vacancy_id),
            )
            await self._db.commit()

    async def save_preview(
        self,
        vacancy_id: str,
        *,
        chat_id: int,
        message_id: int,
        overflow_message_id: int | None,
    ) -> None:
        async with self._lock:
            await self._db.execute(
                """
                UPDATE vacancies
                SET preview_chat_id = ?, preview_message_id = ?,
                    preview_overflow_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (chat_id, message_id, overflow_message_id, utcnow(), vacancy_id),
            )
            await self._db.commit()

    async def mark_applied(self, vacancy_id: str) -> None:
        async with self._lock:
            now = utcnow()
            await self._db.execute(
                """
                UPDATE vacancies
                SET status = 'applied', applied_at = ?, apply_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, now, vacancy_id),
            )
            await self._clear_active_if(vacancy_id)
            await self._db.commit()

    async def mark_apply_failed(self, vacancy_id: str, error: str) -> None:
        async with self._lock:
            await self._db.execute(
                """
                UPDATE vacancies
                SET status = 'apply_failed', apply_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:500], utcnow(), vacancy_id),
            )
            await self._db.commit()

    async def cancel_draft(self, vacancy_id: str) -> None:
        async with self._lock:
            await self._set_status(vacancy_id, STATUS_NOTIFIED)
            await self._clear_active_if(vacancy_id)
            await self._db.commit()

    async def activate_draft(self, vacancy_id: str) -> None:
        async with self._lock:
            current = await self._get_state("active_draft_id")
            if current and current != vacancy_id:
                previous = await self._get(current)
                if previous is not None and previous.status == STATUS_DRAFT:
                    await self._set_status(current, STATUS_NOTIFIED)
            await self._set_state("active_draft_id", vacancy_id)
            await self._set_status(vacancy_id, STATUS_DRAFT)
            await self._db.commit()

    async def get_active_draft(self) -> str | None:
        async with self._lock:
            return await self._get_state("active_draft_id")

    async def get_state(self, key: str) -> str | None:
        async with self._lock:
            return await self._get_state(key)

    async def set_state(self, key: str, value: str | None) -> None:
        async with self._lock:
            await self._set_state(key, value)
            await self._db.commit()

    async def replace_resumes(self, resumes: list[Resume]) -> None:
        async with self._lock:
            now = utcnow()
            await self._db.execute("DELETE FROM resumes")
            await self._db.executemany(
                "INSERT INTO resumes (hash, title, text, updated_at) VALUES (?, ?, ?, ?)",
                [(resume.hash, resume.title, resume.text, now) for resume in resumes],
            )
            await self._db.commit()

    async def get_resume(self, resume_hash: str) -> Resume | None:
        async with self._lock:
            cursor = await self._db.execute("SELECT * FROM resumes WHERE hash = ?", (resume_hash,))
            row = await cursor.fetchone()
            if row is None:
                return None
            return Resume(hash=row["hash"], title=row["title"], text=row["text"])

    async def _get(self, vacancy_id: str) -> VacancyRecord | None:
        cursor = await self._db.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,))
        row = await cursor.fetchone()
        return _record(row) if row is not None else None

    async def _set_status(self, vacancy_id: str, status: str) -> None:
        await self._db.execute(
            "UPDATE vacancies SET status = ?, updated_at = ? WHERE id = ?",
            (status, utcnow(), vacancy_id),
        )

    async def _get_state(self, key: str) -> str | None:
        cursor = await self._db.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return None if row is None else str(row["value"])

    async def _set_state(self, key: str, value: str | None) -> None:
        if value is None:
            await self._db.execute("DELETE FROM app_state WHERE key = ?", (key,))
            return
        await self._db.execute(
            """
            INSERT INTO app_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    async def _clear_active_if(self, vacancy_id: str) -> None:
        current = await self._get_state("active_draft_id")
        if current == vacancy_id:
            await self._set_state("active_draft_id", None)
def _record(row: aiosqlite.Row) -> VacancyRecord:
    return VacancyRecord(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        salary=row["salary"],
        description=row["description"],
        status=row["status"],
        score=row["score"],
        resume_hash=row["resume_hash"],
        resume_title=row["resume_title"],
        match_reason=row["match_reason"],
        short_description=row["short_description"],
        letter=row["letter"],
        filter_name=row["filter_name"],
        test_required=bool(row["test_required"]),
        preview_chat_id=row["preview_chat_id"],
        preview_message_id=row["preview_message_id"],
        preview_overflow_message_id=row["preview_overflow_message_id"],
        apply_error=row["apply_error"],
        first_seen=row["first_seen"],
        updated_at=row["updated_at"],
        applied_at=row["applied_at"],
    )

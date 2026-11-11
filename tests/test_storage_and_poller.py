import aiosqlite
import pytest

from hh_career.bot import format_vacancy_message
from hh_career.config import Settings
from hh_career.models import (
    STATUS_BELOW_THRESHOLD,
    STATUS_NOTIFIED,
    STATUS_PENDING_NOTIFY,
    MatchResult,
    Resume,
    VacancyDetails,
    VacancyPreview,
    VacancyRecord,
)
from hh_career.poller import deliver, process_vacancy
from hh_career.storage import Storage


def _settings(threshold: int = 70) -> Settings:
    return Settings(
        poll_interval_minutes=5,
        min_match_percent=threshold,
        request_delay_seconds=0,
        http_timeout_seconds=5,
        hh_base_url="https://hh.ru",
        database_path=__import__("pathlib").Path("data/hh.sqlite"),
        filters_path=__import__("pathlib").Path("filters.yaml"),
        openai_base_url="https://example.test/v1",
        openai_model="demo",
        openai_temperature=0,
        openai_api_key="key",
        telegram_bot_token="token",
        telegram_chat_id=1,
        cookies={"_xsrf": "a", "hhtoken": "b"},
    )


def _details() -> VacancyDetails:
    return VacancyDetails(
        id="100",
        title="Python разработчик",
        company="Acme",
        url="https://hh.ru/vacancy/100",
        description="Нужен Python и PostgreSQL",
        skills=("Python",),
        salary="от 200000 RUR",
        test_required=False,
    )


class _HH:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_vacancy(self, vacancy_id: str, *, fallback_title: str, fallback_company: str | None):
        self.calls += 1
        return _details()


class _AI:
    def __init__(self, score_value: int) -> None:
        self.score_value = score_value

    async def score(self, details, resumes):
        return MatchResult(
            resume_hash=resumes[0].hash,
            resume_title=resumes[0].title,
            score=self.score_value,
            reason="совпадение по Python",
            short_description="Ищут Python-разработчика.",
        )


class _Notifier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.alerts = []
        self.texts = []

    async def send_vacancy_alert(self, record) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.alerts.append(record)

    async def send_text(self, text: str) -> None:
        self.texts.append(text)


@pytest.mark.asyncio
async def test_processed_vacancy_is_remembered(tmp_path) -> None:
    storage = await Storage.open(tmp_path / "hh.sqlite")
    try:
        assert await storage.is_known("100") is False
        resumes = [Resume("abcd1234", "Backend", "Python")]
        hh = _HH()
        notifier = _Notifier()
        preview = VacancyPreview("100", "Python разработчик", "Acme", "https://hh.ru/vacancy/100")
        await process_vacancy(
            preview,
            search_name="python",
            resumes=resumes,
            settings=_settings(),
            storage=storage,
            hh=hh,
            ai=_AI(40),
            notifier=notifier,
        )
        assert await storage.is_known("100") is True
        saved = await storage.get("100")
        assert saved is not None
        assert saved.status == STATUS_BELOW_THRESHOLD
        assert saved.score == 40
        assert saved.salary == "от 200000 RUR"
        assert notifier.alerts == []
        await process_vacancy(
            preview,
            search_name="python",
            resumes=resumes,
            settings=_settings(),
            storage=storage,
            hh=hh,
            ai=_AI(99),
            notifier=notifier,
        )
        assert hh.calls == 1
        assert notifier.alerts == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_suitable_vacancy_is_notified_once_and_pending_retries(tmp_path) -> None:
    storage = await Storage.open(tmp_path / "hh.sqlite")
    try:
        resumes = [Resume("abcd1234", "Backend", "Python")]
        preview = VacancyPreview("100", "Python разработчик", "Acme", "https://hh.ru/vacancy/100")
        failing = _Notifier(fail=True)
        await process_vacancy(
            preview,
            search_name="python",
            resumes=resumes,
            settings=_settings(),
            storage=storage,
            hh=_HH(),
            ai=_AI(88),
            notifier=failing,
        )
        saved = await storage.get("100")
        assert saved is not None
        assert saved.status == STATUS_PENDING_NOTIFY
        working = _Notifier()
        await deliver(saved, storage, working)
        assert len(working.alerts) == 1
        assert "Ищут Python" in (working.alerts[0].short_description or "")
        assert (await storage.get("100")).status == STATUS_NOTIFIED
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_draft_switches_back_to_notified(tmp_path) -> None:
    storage = await Storage.open(tmp_path / "hh.sqlite")
    try:
        for vacancy_id in ("1", "2"):
            await storage.remember(
                VacancyDetails(
                    id=vacancy_id,
                    title=f"Вакансия {vacancy_id}",
                    company=None,
                    url=f"https://hh.ru/vacancy/{vacancy_id}",
                    description="текст",
                    skills=(),
                    salary=None,
                    test_required=False,
                ),
                status=STATUS_NOTIFIED,
                score=80,
                resume_hash="abcd1234",
                resume_title="Backend",
                reason="",
                short_description="кратко",
                filter_name="python",
                description="текст",
            )
        await storage.activate_draft("1")
        await storage.save_letter("1", "Письмо")
        await storage.activate_draft("2")
        assert (await storage.get("1")).status == STATUS_NOTIFIED
        assert (await storage.get("2")).status == "draft"
        assert await storage.get_active_draft() == "2"
        await storage.cancel_draft("2")
        assert (await storage.get("2")).status == STATUS_NOTIFIED
        assert await storage.get_active_draft() is None
        assert (await storage.get("2")).letter == "Письмо" or (await storage.get("1")).letter == "Письмо"
    finally:
        await storage.close()


def _record(**overrides) -> VacancyRecord:
    data = dict(
        id="1",
        url="https://hh.ru/vacancy/1",
        title="Backend",
        company="Acme",
        salary="200000–300000 RUR",
        description="",
        status=STATUS_NOTIFIED,
        score=80,
        resume_hash=None,
        resume_title=None,
        match_reason=None,
        short_description="Кратко",
        letter=None,
        filter_name=None,
        test_required=False,
        preview_chat_id=None,
        preview_message_id=None,
        preview_overflow_message_id=None,
        apply_error=None,
        first_seen="",
        updated_at="",
        applied_at=None,
    )
    data.update(overrides)
    return VacancyRecord(**data)


def test_vacancy_message_shows_salary_under_company() -> None:
    text = format_vacancy_message(_record())
    company_at = text.index("Acme")
    salary_at = text.index("200000–300000 RUR")
    assert company_at < salary_at
    assert "Уровень дохода не указан" not in text
    missing = format_vacancy_message(_record(salary=None, description="Описание"))
    assert "Уровень дохода не указан" in missing
    stored = format_vacancy_message(
        _record(salary=None, description="Описание\n\nЗарплата: от 150000 RUR")
    )
    assert "от 150000 RUR" in stored
    assert "Уровень дохода не указан" not in stored


@pytest.mark.asyncio
async def test_old_database_without_salary_column_still_opens(tmp_path) -> None:
    path = tmp_path / "old.sqlite"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE vacancies (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                company TEXT,
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
            )
            """
        )
        await db.execute(
            """
            INSERT INTO vacancies (
                id, url, title, description, status, test_required, first_seen, updated_at
            ) VALUES ('9', 'https://hh.ru/vacancy/9', 'Old', 'текст\n\nЗарплата: до 100000 RUR', 'notified', 0, 't', 't')
            """
        )
        await db.commit()
    storage = await Storage.open(path)
    try:
        saved = await storage.get("9")
        assert saved is not None
        assert saved.salary is None
        assert "до 100000 RUR" in format_vacancy_message(saved)
    finally:
        await storage.close()

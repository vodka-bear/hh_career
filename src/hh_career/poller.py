import asyncio
import logging
from typing import Protocol

from hh_career.ai import CareerAI, is_suitable
from hh_career.config import Settings, load_filters, search_page_urls
from hh_career.hh import AuthError, HHClient, HHError, VacancyClosed
from hh_career.models import (
    STATUS_BELOW_THRESHOLD,
    STATUS_NOTIFIED,
    STATUS_PENDING_NOTIFY,
    Resume,
    VacancyDetails,
    VacancyPreview,
    VacancyRecord,
)
from hh_career.storage import Storage

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send_vacancy_alert(self, record: VacancyRecord) -> None: ...

    async def send_text(self, text: str) -> None: ...


async def poll_loop(
    settings: Settings,
    storage: Storage,
    hh: HHClient,
    ai: CareerAI,
    notifier: Notifier,
) -> None:
    while True:
        try:
            await run_once(settings, storage, hh, ai, notifier)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Цикл поиска завершился с ошибкой")
        await asyncio.sleep(settings.poll_interval_minutes * 60)


async def run_once(
    settings: Settings,
    storage: Storage,
    hh: HHClient,
    ai: CareerAI,
    notifier: Notifier,
) -> None:
    try:
        resumes = await hh.fetch_resumes()
    except AuthError as exc:
        await _alert_once(
            storage,
            notifier,
            "auth_alert",
            f"Сессия hh.ru недействительна. Обновите cookies. {exc}",
        )
        return
    except HHError:
        logger.exception("Не удалось получить резюме")
        return

    await storage.set_state("auth_alert", None)
    if not resumes:
        await _alert_once(storage, notifier, "resumes_alert", "На hh.ru не найдено ни одного резюме.")
        return
    await storage.set_state("resumes_alert", None)
    await storage.replace_resumes(resumes)
    await flush_pending(storage, notifier)

    try:
        filters = load_filters(settings.filters_path)
    except ValueError:
        logger.exception("Файл фильтров не прочитан")
        return

    for search in filters:
        for url in search_page_urls(search):
            try:
                previews = await hh.search(url)
            except AuthError as exc:
                await _alert_once(
                    storage,
                    notifier,
                    "auth_alert",
                    f"Сессия hh.ru недействительна. Обновите cookies. {exc}",
                )
                return
            except HHError:
                logger.exception("Поиск не выполнен: %s", url)
                continue
            if not previews:
                break
            for preview in previews:
                try:
                    await process_vacancy(
                        preview,
                        search_name=search.name,
                        resumes=resumes,
                        settings=settings,
                        storage=storage,
                        hh=hh,
                        ai=ai,
                        notifier=notifier,
                    )
                except VacancyClosed:
                    await remember_closed(preview, search.name, resumes, storage)
                except AuthError as exc:
                    await _alert_once(
                        storage,
                        notifier,
                        "auth_alert",
                        f"Сессия hh.ru недействительна. Обновите cookies. {exc}",
                    )
                    return
                except Exception:
                    logger.exception("Вакансия %s будет проверена в следующем цикле", preview.id)


async def process_vacancy(
    preview: VacancyPreview,
    *,
    search_name: str,
    resumes: list[Resume],
    settings: Settings,
    storage: Storage,
    hh: HHClient,
    ai: CareerAI,
    notifier: Notifier,
) -> None:
    if await storage.is_known(preview.id):
        return
    details = await hh.fetch_vacancy(
        preview.id,
        fallback_title=preview.title,
        fallback_company=preview.company,
    )
    match = await ai.score(details, resumes)
    status = (
        STATUS_PENDING_NOTIFY
        if is_suitable(match.score, settings.min_match_percent)
        else STATUS_BELOW_THRESHOLD
    )
    record = await storage.remember(
        details,
        status=status,
        score=match.score,
        resume_hash=match.resume_hash,
        resume_title=match.resume_title,
        reason=match.reason,
        short_description=match.short_description,
        filter_name=search_name,
        description=description_for_storage(details),
    )
    logger.info("Вакансия %s: %s%%, статус %s", preview.id, match.score, status)
    if status == STATUS_PENDING_NOTIFY:
        await deliver(record, storage, notifier)


async def flush_pending(storage: Storage, notifier: Notifier) -> None:
    for record in await storage.list_by_status(STATUS_PENDING_NOTIFY):
        await deliver(record, storage, notifier)


async def deliver(record: VacancyRecord, storage: Storage, notifier: Notifier) -> None:
    try:
        await notifier.send_vacancy_alert(record)
    except Exception:
        logger.exception("Не удалось отправить вакансию %s в Telegram", record.id)
        return
    await storage.set_status(record.id, STATUS_NOTIFIED)


async def remember_closed(
    preview: VacancyPreview,
    search_name: str,
    resumes: list[Resume],
    storage: Storage,
) -> None:
    if await storage.is_known(preview.id):
        return
    resume = resumes[0]
    details = VacancyDetails(
        id=preview.id,
        title=preview.title,
        company=preview.company,
        url=preview.url,
        description="",
        skills=(),
        salary=None,
        test_required=False,
    )
    await storage.remember(
        details,
        status=STATUS_BELOW_THRESHOLD,
        score=0,
        resume_hash=resume.hash,
        resume_title=resume.title,
        reason="Вакансия недоступна",
        short_description=preview.title,
        filter_name=search_name,
        description="",
    )


def description_for_storage(details: VacancyDetails) -> str:
    chunks = [details.description.strip()]
    if details.skills:
        chunks.append("Навыки: " + ", ".join(details.skills))
    return "\n\n".join(chunk for chunk in chunks if chunk)[:100000]


async def _alert_once(storage: Storage, notifier: Notifier, key: str, text: str) -> None:
    logger.error(text)
    if await storage.get_state(key) == "1":
        return
    try:
        await notifier.send_text(text)
    except Exception:
        logger.exception("Не удалось отправить предупреждение в Telegram")
        return
    await storage.set_state(key, "1")

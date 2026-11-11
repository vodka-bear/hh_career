import json
import logging
import re
from typing import Any

from openai import APIStatusError, AsyncOpenAI

from hh_career.config import Settings
from hh_career.models import MatchResult, Resume, VacancyDetails, VacancyRecord

logger = logging.getLogger(__name__)

_MATCH_SYSTEM = """Ты сравниваешь вакансию с резюме соискателя.
Верни только JSON-объект с ключами:
best_resume_hash — hash одного из переданных резюме, которое подходит лучше остальных;
score — целое число от 0 до 100, насколько эта вакансия подходит выбранному резюме;
reason — короткое обоснование на русском;
short_description — описание вакансии на русском в 2–4 предложениях, без оценки кандидата.
Не выдумывай опыт и навыки. Не выбирай hash, которого нет во входе."""

_LETTER_SYSTEM = """Напиши сопроводительное письмо на русском от первого лица.
Опирайся только на факты из резюме. Не добавляй компании, должности, годы, проекты и навыки, которых в резюме нет.
Без темы, без markdown, без контактов и без подписи с выдуманными данными.
Объём 900–1800 знаков. Верни только текст письма."""

_REVISE_SYSTEM = """Отредактируй сопроводительное письмо по указанию пользователя.
Сохрани факты только из резюме. Если указание просит добавить опыт, которого нет в резюме, не добавляй его.
Без markdown. Верни только текст письма."""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_LIMIT = 12000


class AIError(Exception):
    pass


def is_suitable(score: int, min_match_percent: int) -> bool:
    return score >= min_match_percent


def parse_match_payload(payload: dict[str, Any], resumes: list[Resume]) -> MatchResult:
    by_hash = {resume.hash.lower(): resume for resume in resumes}
    raw_hash = payload.get("best_resume_hash") or payload.get("resume_hash")
    if not isinstance(raw_hash, str) or raw_hash.lower() not in by_hash:
        raise ValueError("Модель выбрала резюме, которого нет среди переданных")
    resume = by_hash[raw_hash.lower()]
    try:
        score = int(round(float(payload["score"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("В ответе модели нет корректного score") from exc
    if not 0 <= score <= 100:
        raise ValueError("score вне диапазона 0–100")
    reason = str(payload.get("reason") or "").strip()
    short_description = str(payload.get("short_description") or "").strip()
    if not short_description:
        short_description = "Краткое описание не подготовлено"
    return MatchResult(
        resume_hash=resume.hash,
        resume_title=resume.title,
        score=score,
        reason=reason,
        short_description=short_description,
    )


def load_json_object(text: str) -> dict[str, Any]:
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Ответ модели не объект JSON")
    return data


def vacancy_prompt_text(details: VacancyDetails | VacancyRecord) -> str:
    salary = getattr(details, "salary", None)
    skills = getattr(details, "skills", ())
    lines = [
        f"Название: {details.title}",
        f"Компания: {details.company or 'не указана'}",
        f"Ссылка: {details.url}",
    ]
    if salary:
        lines.append(f"Зарплата: {salary}")
    if skills:
        lines.append("Навыки: " + ", ".join(skills))
    description = details.description.strip()
    if len(description) > _LIMIT:
        description = description[:_LIMIT] + "\n[текст обрезан]"
    lines.append("Описание:")
    lines.append(description or "Описание отсутствует")
    return "\n".join(lines)


def resumes_prompt_text(resumes: list[Resume]) -> str:
    blocks: list[str] = []
    for resume in resumes:
        text = resume.text.strip()
        if len(text) > _LIMIT:
            text = text[:_LIMIT] + "\n[текст обрезан]"
        blocks.append(f"hash: {resume.hash}\nНазвание: {resume.title}\n{text}")
    return "\n\n".join(blocks)


class CareerAI:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self._settings = settings
        self._client = client or AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=180,
        )

    async def score(self, details: VacancyDetails, resumes: list[Resume]) -> MatchResult:
        if not resumes:
            raise AIError("Нет резюме для оценки")
        user = (
            "Вакансия:\n"
            f"{vacancy_prompt_text(details)}\n\n"
            "Резюме:\n"
            f"{resumes_prompt_text(resumes)}"
        )
        raw = await self._complete(_MATCH_SYSTEM, user, json_mode=True)
        try:
            return parse_match_payload(load_json_object(raw), resumes)
        except (json.JSONDecodeError, ValueError) as exc:
            raise AIError(f"Не удалось разобрать оценку вакансии: {exc}") from exc

    async def cover_letter(self, vacancy: VacancyRecord, resume: Resume) -> str:
        user = (
            "Вакансия:\n"
            f"{vacancy_prompt_text(vacancy)}\n\n"
            "Резюме:\n"
            f"{resumes_prompt_text([resume])}"
        )
        letter = await self._complete(_LETTER_SYSTEM, user, json_mode=False)
        return _plain_letter(letter)

    async def revise_letter(
        self,
        vacancy: VacancyRecord,
        resume: Resume,
        letter: str,
        instruction: str,
    ) -> str:
        user = (
            "Вакансия:\n"
            f"{vacancy_prompt_text(vacancy)}\n\n"
            "Резюме:\n"
            f"{resumes_prompt_text([resume])}\n\n"
            "Текущее письмо:\n"
            f"{letter}\n\n"
            "Указание пользователя:\n"
            f"{instruction.strip()}"
        )
        revised = await self._complete(_REVISE_SYSTEM, user, json_mode=False)
        return _plain_letter(revised)

    async def _complete(self, system: str, user: str, *, json_mode: bool) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            return await self._request(messages, json_mode=json_mode)
        except Exception as exc:
            if not json_mode:
                logger.exception("Запрос к модели не выполнен")
                raise AIError("Модель не ответила") from exc
            logger.warning("Повтор оценки без JSON mode: %s", exc)
            try:
                return await self._request(messages, json_mode=False)
            except Exception as retry_exc:
                logger.exception("Повторный запрос к модели не выполнен")
                raise AIError("Модель не ответила") from retry_exc

    async def _request(self, messages: list[dict[str, str]], *, json_mode: bool) -> str:
        kwargs: dict[str, Any] = {
            "model": self._settings.openai_model,
            "temperature": self._settings.openai_temperature,
            "messages": messages,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if exc.status_code != 400 or "temperature" not in str(exc).lower():
                raise
            kwargs.pop("temperature", None)
            completion = await self._client.chat.completions.create(**kwargs)
        message = completion.choices[0].message if completion.choices else None
        content = _message_text(message)
        if not content:
            raise AIError("Пустой ответ модели")
        return content


def _message_text(message: Any) -> str:
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()
    extra = getattr(message, "model_extra", None) or {}
    candidates: list[Any] = []
    if isinstance(extra, dict):
        candidates.extend(extra.get(key) for key in ("reasoning_content", "reasoning"))
    candidates.extend(getattr(message, key, None) for key in ("reasoning_content", "reasoning"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _plain_letter(text: str) -> str:
    letter = text.strip()
    if letter.startswith("```"):
        letter = _FENCE_RE.sub("", letter).strip()
    if not letter:
        raise AIError("Модель вернула пустое письмо")
    return letter

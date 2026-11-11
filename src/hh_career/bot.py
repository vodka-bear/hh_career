import logging
import re
from dataclasses import dataclass, field
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from hh_career.ai import AIError, CareerAI
from hh_career.config import Settings
from hh_career.hh import AuthError, HHClient, _as_text
from hh_career.models import STATUS_APPLIED, Resume, VacancyRecord
from hh_career.storage import Storage

logger = logging.getLogger(__name__)

_PREVIEW_LIMIT = 3500
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_STORED_SALARY_RE = re.compile(r"(?:^|\n)Зарплата: (.+?)\s*$")
_SALARY_UNKNOWN = "Уровень дохода не указан"


@dataclass
class AppContext:
    settings: Settings
    storage: Storage
    hh: HHClient
    ai: CareerAI
    bot: Bot
    busy: set[str] = field(default_factory=set)


class TelegramUI:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self._chat_id = settings.telegram_chat_id

    async def send_vacancy_alert(self, record: VacancyRecord) -> None:
        await self.bot.send_message(
            self._chat_id,
            format_vacancy_message(record),
            parse_mode="HTML",
            reply_markup=apply_keyboard(record.id, record.url),
            link_preview_options=_NO_PREVIEW,
        )

    async def send_text(self, text: str) -> None:
        await self.bot.send_message(self._chat_id, text)


def build_router() -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, ctx: AppContext) -> None:
        if not _allowed(message.chat.id, ctx):
            return
        await message.answer(
            "Слежу за новыми вакансиями hh.ru. Подходящие пришлю сюда с кнопкой «Откликнуться»."
        )

    @router.callback_query(F.data.startswith("apply:"))
    async def on_apply(query: CallbackQuery, ctx: AppContext) -> None:
        if query.message is None or not _allowed(query.message.chat.id, ctx):
            await query.answer()
            return
        vacancy_id = _callback_id(query.data or "", "apply:")
        if vacancy_id is None:
            await query.answer()
            return
        record = await ctx.storage.get(vacancy_id)
        if record is None:
            await query.answer("Вакансия не найдена", show_alert=True)
            return
        if record.status == STATUS_APPLIED:
            await query.answer("Отклик уже отправлен", show_alert=True)
            return
        if record.test_required:
            await query.answer("У вакансии есть тест, автоотклик невозможен", show_alert=True)
            await ctx.bot.send_message(
                query.message.chat.id,
                f"Вакансия «{record.title}» требует тест. Откликнитесь вручную:\n{record.url}",
            )
            return
        if record.letter:
            await query.answer()
            await _show_letter(ctx, query.message.chat.id, record, record.letter)
            return
        if vacancy_id in ctx.busy:
            await query.answer("Письмо уже готовится")
            return
        await query.answer("Готовлю письмо")
        ctx.busy.add(vacancy_id)
        try:
            resume = await _resume_for(ctx, record)
            letter = await ctx.ai.cover_letter(record, resume)
        except (AIError, AuthError) as exc:
            logger.exception("Не удалось подготовить письмо для %s", vacancy_id)
            await ctx.bot.send_message(query.message.chat.id, f"Не удалось составить письмо: {exc}")
            return
        except Exception:
            logger.exception("Не удалось подготовить письмо для %s", vacancy_id)
            await ctx.bot.send_message(query.message.chat.id, "Не удалось составить письмо. Попробуйте ещё раз.")
            return
        finally:
            ctx.busy.discard(vacancy_id)
        fresh = await ctx.storage.get(vacancy_id)
        await _show_letter(ctx, query.message.chat.id, fresh or record, letter)

    @router.callback_query(F.data.startswith("send:"))
    async def on_send(query: CallbackQuery, ctx: AppContext) -> None:
        if query.message is None or not _allowed(query.message.chat.id, ctx):
            await query.answer()
            return
        vacancy_id = _callback_id(query.data or "", "send:")
        if vacancy_id is None:
            await query.answer()
            return
        record = await ctx.storage.get(vacancy_id)
        if record is None or not record.letter or not record.resume_hash:
            await query.answer("Нет письма для отправки", show_alert=True)
            return
        if record.status == STATUS_APPLIED:
            await query.answer("Отклик уже отправлен", show_alert=True)
            return
        await query.answer("Отправляю отклик")
        try:
            if record.test_required or await ctx.hh.requires_test(vacancy_id):
                await ctx.bot.send_message(
                    query.message.chat.id,
                    f"Вакансия «{record.title}» требует тест. Отклик не отправлен.\n{record.url}",
                )
                return
            result = await ctx.hh.apply(vacancy_id, record.resume_hash, record.letter)
        except AuthError as exc:
            await ctx.storage.mark_apply_failed(vacancy_id, str(exc))
            await ctx.bot.send_message(query.message.chat.id, f"Отклик не отправлен: {exc}")
            return
        except Exception as exc:
            logger.exception("Отклик на %s не отправлен", vacancy_id)
            await ctx.storage.mark_apply_failed(vacancy_id, str(exc))
            await ctx.bot.send_message(query.message.chat.id, "Отклик не отправлен из-за ошибки сети. Можно повторить.")
            return
        if not result.ok:
            error = result.error or "hh.ru отклонил отклик"
            await ctx.storage.mark_apply_failed(vacancy_id, error)
            await ctx.bot.send_message(query.message.chat.id, f"Отклик не отправлен: {error}")
            return
        await ctx.storage.mark_applied(vacancy_id)
        await _clear_keyboard(ctx.bot, query.message.chat.id, record.preview_message_id)
        await ctx.bot.send_message(
            query.message.chat.id,
            f"Отклик на «{record.title}» отправлен.\n{record.url}",
        )

    @router.callback_query(F.data.startswith("cancel:"))
    async def on_cancel(query: CallbackQuery, ctx: AppContext) -> None:
        if query.message is None or not _allowed(query.message.chat.id, ctx):
            await query.answer()
            return
        vacancy_id = _callback_id(query.data or "", "cancel:")
        if vacancy_id is None:
            await query.answer()
            return
        record = await ctx.storage.get(vacancy_id)
        await ctx.storage.cancel_draft(vacancy_id)
        if record is not None:
            await _clear_keyboard(ctx.bot, query.message.chat.id, record.preview_message_id)
        await query.answer("Черновик отменён")
        await ctx.bot.send_message(query.message.chat.id, "Черновик отменён. Отклик не отправлен.")

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_edit_prompt(message: Message, ctx: AppContext) -> None:
        if message.text is None or not _allowed(message.chat.id, ctx):
            return
        instruction = message.text.strip()
        if not instruction:
            return
        vacancy_id = await ctx.storage.get_active_draft()
        if not vacancy_id:
            await message.answer("Нет активного черновика. Нажмите «Откликнуться» у вакансии.")
            return
        record = await ctx.storage.get(vacancy_id)
        if record is None or not record.letter:
            await message.answer("Черновик письма не найден.")
            return
        if record.status == STATUS_APPLIED:
            await message.answer("Отклик уже отправлен, письмо больше не правится.")
            return
        if vacancy_id in ctx.busy:
            await message.answer("Подождите, предыдущий запрос к модели ещё выполняется.")
            return
        await message.answer("Переписываю письмо…")
        ctx.busy.add(vacancy_id)
        try:
            resume = await _resume_for(ctx, record)
            letter = await ctx.ai.revise_letter(record, resume, record.letter, instruction[:2000])
        except (AIError, AuthError) as exc:
            logger.exception("Не удалось поправить письмо %s", vacancy_id)
            await message.answer(f"Не удалось поправить письмо: {exc}")
            return
        except Exception:
            logger.exception("Не удалось поправить письмо %s", vacancy_id)
            await message.answer("Не удалось поправить письмо. Попробуйте ещё раз.")
            return
        finally:
            ctx.busy.discard(vacancy_id)
        fresh = await ctx.storage.get(vacancy_id)
        await _show_letter(ctx, message.chat.id, fresh or record, letter)

    return router


def format_vacancy_message(record: VacancyRecord) -> str:
    lines = [f'<a href="{escape(record.url)}">{escape(record.title)}</a>']
    if record.company:
        lines.append(f"<b>{escape(record.company)}</b>")
    lines.append(escape(_salary_text(record)))
    lines.append("")
    lines.append(escape(record.short_description or ""))
    lines.append("")
    lines.append(f"<b>Соответствие</b>: {record.score}%")
    if record.resume_title:
        lines.append(f"<b>Резюме</b>: {escape(_as_text(record.resume_title))}")
    return "\n".join(lines)


def _salary_text(record: VacancyRecord) -> str:
    salary = (record.salary or "").strip()
    if salary:
        return salary
    match = _STORED_SALARY_RE.search(record.description or "")
    if match:
        return match.group(1).strip()
    return _SALARY_UNKNOWN


def preview_messages(letter: str) -> list[str]:
    header = "Сопроводительное письмо"
    footer = "Чтобы поправить письмо, отправьте сообщение с тем, что изменить."
    body = letter.strip()
    single = f"{header}\n\n{body}\n\n{footer}"
    if len(single) <= _PREVIEW_LIMIT:
        return [single]
    head_budget = max(500, _PREVIEW_LIMIT - len(header) - 2)
    head = f"{header}\n\n{body[:head_budget]}"
    tail_body = body[head_budget:]
    tail = f"{tail_body}\n\n{footer}"
    if len(tail) > _PREVIEW_LIMIT:
        note = "\n\n[предпросмотр обрезан, в отклик уйдёт полный текст]"
        tail = tail_body[: _PREVIEW_LIMIT - len(footer) - len(note) - 2] + f"\n\n{footer}{note}"
    return [head, tail]


def apply_keyboard(vacancy_id: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Откликнуться", callback_data=f"apply:{vacancy_id}"),
                InlineKeyboardButton(text="Открыть", url=url),
            ]
        ]
    )


def draft_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отправить отклик", callback_data=f"send:{vacancy_id}")],
            [InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{vacancy_id}")],
        ]
    )


async def _show_letter(ctx: AppContext, chat_id: int, record: VacancyRecord, letter: str) -> None:
    await ctx.storage.save_letter(record.id, letter)
    await ctx.storage.activate_draft(record.id)
    parts = preview_messages(letter)
    keyboard = draft_keyboard(record.id)
    same_shape = _same_preview_shape(record, len(parts))
    if same_shape and record.preview_chat_id == chat_id and record.preview_message_id:
        try:
            if len(parts) == 1:
                await ctx.bot.edit_message_text(
                    parts[0],
                    chat_id=chat_id,
                    message_id=record.preview_message_id,
                    reply_markup=keyboard,
                )
            else:
                await ctx.bot.edit_message_text(
                    parts[0],
                    chat_id=chat_id,
                    message_id=record.preview_overflow_message_id,
                )
                await ctx.bot.edit_message_text(
                    parts[1],
                    chat_id=chat_id,
                    message_id=record.preview_message_id,
                    reply_markup=keyboard,
                )
            return
        except TelegramBadRequest:
            logger.info("Предпросмотр %s отправлен заново", record.id)
    await _delete_preview(ctx.bot, chat_id, record)
    overflow_id = None
    if len(parts) == 2:
        first = await ctx.bot.send_message(chat_id, parts[0])
        overflow_id = first.message_id
        text = parts[1]
    else:
        text = parts[0]
    sent = await ctx.bot.send_message(chat_id, text, reply_markup=keyboard)
    await ctx.storage.save_preview(
        record.id,
        chat_id=chat_id,
        message_id=sent.message_id,
        overflow_message_id=overflow_id,
    )


def _same_preview_shape(record: VacancyRecord, parts: int) -> bool:
    if parts == 1:
        return record.preview_overflow_message_id is None
    return record.preview_overflow_message_id is not None


async def _delete_preview(bot: Bot, chat_id: int, record: VacancyRecord) -> None:
    for message_id in (record.preview_overflow_message_id, record.preview_message_id):
        if message_id is None:
            continue
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            continue


async def _clear_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramBadRequest:
        return


async def _resume_for(ctx: AppContext, record: VacancyRecord) -> Resume:
    if not record.resume_hash:
        raise AIError("Для вакансии не выбрано резюме")
    cached = await ctx.storage.get_resume(record.resume_hash)
    if cached is not None:
        return cached
    return await ctx.hh.fetch_resume(record.resume_hash, record.resume_title)


def _allowed(chat_id: int, ctx: AppContext) -> bool:
    return chat_id == ctx.settings.telegram_chat_id


def _callback_id(data: str, prefix: str) -> str | None:
    vacancy_id = data.removeprefix(prefix)
    if vacancy_id.isdigit():
        return vacancy_id
    return None

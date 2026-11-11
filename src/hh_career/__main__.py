import argparse
import asyncio
import contextlib
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

from hh_career.ai import CareerAI
from hh_career.bot import AppContext, TelegramUI, build_router
from hh_career.config import load_filters, load_settings
from hh_career.hh import HHClient
from hh_career.poller import poll_loop
from hh_career.storage import Storage

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск вакансий hh.ru и отклики в Telegram")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--cookies", type=Path, default=None, help="Файл cookies hh.ru")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.config, args.cookies))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        return


async def _run(config_path: Path, cookie_file: Path | None) -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings(config_path, cookie_file=cookie_file)
    load_filters(settings.filters_path)
    storage = await Storage.open(settings.database_path)
    hh = HHClient(
        settings.cookies,
        delay_seconds=settings.request_delay_seconds,
        timeout_seconds=settings.http_timeout_seconds,
        base_url=settings.hh_base_url,
    )
    ai = CareerAI(settings)
    bot = Bot(token=settings.telegram_bot_token)
    ui = TelegramUI(bot, settings)
    ctx = AppContext(settings=settings, storage=storage, hh=hh, ai=ai, bot=bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router())
    poller = asyncio.create_task(poll_loop(settings, storage, hh, ai, ui), name="hh-poller")
    logger.info(
        "Запуск: опрос каждые %s мин, порог %s%%",
        settings.poll_interval_minutes,
        settings.min_match_percent,
    )
    try:
        await dispatcher.start_polling(bot, ctx=ctx)
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        await hh.aclose()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    main()

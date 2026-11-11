import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import yaml

from hh_career.cookies import load_cookies


@dataclass(frozen=True)
class SearchFilter:
    name: str
    url: str
    pages: int


@dataclass(frozen=True)
class Settings:
    poll_interval_minutes: float
    min_match_percent: int
    request_delay_seconds: float
    http_timeout_seconds: float
    hh_base_url: str
    database_path: Path
    filters_path: Path
    openai_base_url: str
    openai_model: str
    openai_temperature: float
    openai_api_key: str
    telegram_bot_token: str
    telegram_chat_id: int
    cookies: dict[str, str]


def load_settings(path: Path, *, cookie_file: Path | None = None) -> Settings:
    if not path.is_file():
        raise ValueError(f"Файл конфигурации не найден: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml должен быть словарём")
    base = path.parent
    openai = raw.get("openai") or {}
    if not isinstance(openai, dict):
        raise ValueError("Блок openai в конфиге должен быть словарём")

    poll_interval = float(raw.get("poll_interval_minutes", 5))
    min_match = int(raw.get("min_match_percent", 70))
    delay = float(raw.get("request_delay_seconds", 2))
    timeout = float(raw.get("http_timeout_seconds", 30))
    if poll_interval <= 0:
        raise ValueError("poll_interval_minutes должен быть больше нуля")
    if not 0 <= min_match <= 100:
        raise ValueError("min_match_percent должен быть от 0 до 100")
    if delay < 0 or timeout <= 0:
        raise ValueError("Некорректные request_delay_seconds или http_timeout_seconds")

    filters_path = _resolve(base, raw.get("filters_path", "filters.yaml"))
    database_path = _resolve(base, raw.get("database_path", "data/hh.sqlite"))
    hh_base_url = _hh_base_url(raw.get("hh_base_url", "https://hh.ru"))
    api_key = _env("OPENAI_API_KEY") or _env("XAI_API_KEY")
    if not api_key:
        raise ValueError("Не задана переменная окружения OPENAI_API_KEY или XAI_API_KEY")
    token = _required_env("TELEGRAM_BOT_TOKEN")
    chat_raw = _required_env("TELEGRAM_CHAT_ID")
    try:
        chat_id = int(chat_raw)
    except ValueError as exc:
        raise ValueError("TELEGRAM_CHAT_ID должен быть числом") from exc

    return Settings(
        poll_interval_minutes=poll_interval,
        min_match_percent=min_match,
        request_delay_seconds=delay,
        http_timeout_seconds=timeout,
        hh_base_url=hh_base_url,
        database_path=database_path,
        filters_path=filters_path,
        openai_base_url=str(openai.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
        openai_model=str(openai.get("model") or "gpt-4o-mini"),
        openai_temperature=float(openai.get("temperature", 0.2)),
        openai_api_key=api_key,
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        cookies=load_cookies(cookie_file=cookie_file, env_value=os.environ.get("HH_COOKIES")),
    )


def load_filters(path: Path) -> list[SearchFilter]:
    if not path.is_file():
        raise ValueError(f"Файл фильтров не найден: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("searches"), list):
        raise ValueError("В файле фильтров нужен список searches")
    filters: list[SearchFilter] = []
    for index, item in enumerate(raw["searches"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Поиск #{index} должен быть словарём")
        name = str(item.get("name") or f"search-{index}").strip()
        url = str(item.get("url") or "").strip()
        pages = int(item.get("pages", 1))
        if pages < 1:
            raise ValueError(f"У поиска {name} pages должен быть не меньше 1")
        _validate_search_url(url, name)
        filters.append(SearchFilter(name=name, url=url, pages=pages))
    if not filters:
        raise ValueError("Список searches пуст")
    return filters


def search_page_urls(search: SearchFilter) -> list[str]:
    return [with_page(search.url, page) for page in range(search.pages)]


def with_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "page"]
    query.append(("page", str(page)))
    return urlunparse(parsed._replace(query=urlencode(query)))


def is_hh_host(host: str) -> bool:
    name = host.lower().split(":")[0].rstrip(".")
    return name == "hh.ru" or name.endswith(".hh.ru")


def _hh_base_url(value: object) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not is_hh_host(parsed.netloc):
        raise ValueError("hh_base_url должен вести на hh.ru, например https://krasnoyarsk.hh.ru")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("hh_base_url должен быть адресом сайта без пути и параметров")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_search_url(url: str, name: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not is_hh_host(parsed.netloc):
        raise ValueError(f"URL поиска {name} должен вести на hh.ru")


def _resolve(base: Path, value: object) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    return base / path


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Не задана переменная окружения {name}")
    return value

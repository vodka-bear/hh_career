import json
import re
from pathlib import Path

_AUTH_COOKIE_NAMES = ("hhtoken", "hhuid")


def parse_cookies(raw: str) -> dict[str, str]:
    """Разбирает Cookie-Editor JSON, словарь name/value или строку заголовка Cookie."""
    text = raw.lstrip("\ufeff").strip()
    if not text:
        raise ValueError("Пустой набор cookies")
    if text[0] in "[{":
        cookies = _parse_json_cookies(text)
    else:
        cookies = _parse_header_cookies(text)
    _require_session(cookies)
    return cookies


def load_cookies(*, cookie_file: Path | None, env_value: str | None) -> dict[str, str]:
    if cookie_file is not None:
        if not cookie_file.is_file():
            raise ValueError(f"Файл cookies не найден: {cookie_file}")
        return parse_cookies(cookie_file.read_text(encoding="utf-8"))
    if env_value and env_value.strip():
        return parse_cookies(env_value)
    raise ValueError("Передайте cookies файлом --cookies или переменной HH_COOKIES")


def _parse_json_cookies(text: str) -> dict[str, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Cookies в JSON не разбираются") from exc
    cookies: dict[str, str] = {}
    if isinstance(payload, dict):
        for name, value in payload.items():
            _put_cookie(cookies, str(name), value)
        return cookies
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or "name" not in item:
                continue
            _put_cookie(cookies, str(item.get("name", "")), item.get("value", ""))
        return cookies
    raise ValueError("JSON cookies должен быть списком или словарём")


def _parse_header_cookies(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        _put_cookie(cookies, name, value)
    return cookies


def _put_cookie(cookies: dict[str, str], name: str, value: object) -> None:
    key = name.strip()
    if not key or key.startswith("#"):
        return
    cookie_value = str(value).strip()
    if len(cookie_value) >= 2 and cookie_value[0] == cookie_value[-1] and cookie_value[0] in "\"'":
        cookie_value = cookie_value[1:-1]
    try:
        cookie_value.encode("latin-1")
    except UnicodeEncodeError:
        return
    if cookie_value:
        cookies[key] = cookie_value


def _require_session(cookies: dict[str, str]) -> None:
    if "_xsrf" not in cookies:
        raise ValueError("В cookies нет _xsrf")
    if not any(name in cookies for name in _AUTH_COOKIE_NAMES):
        raise ValueError("В cookies нет hhtoken или hhuid")

import ast
import asyncio
import json
import logging
import re
from html import unescape
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from hh_career.config import is_hh_host
from hh_career.models import ApplyResult, Resume, VacancyDetails, VacancyPreview

logger = logging.getLogger(__name__)

DEFAULT_HH_BASE_URL = "https://hh.ru"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_HASH_RE = re.compile(r"[a-fA-F0-9]{8,64}")
_VACANCY_ID_RE = re.compile(r"/vacancy/(\d+)")
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"<(br|p|li|div|h\d|tr)[^>]*>", re.IGNORECASE)


class HHError(Exception):
    pass


class AuthError(HHError):
    pass


class VacancyClosed(HHError):
    pass


def extract_lux_state(html: str) -> dict[str, Any] | None:
    marker = html.find("HH-Lux-InitialState")
    if marker < 0:
        return None
    tag_end = html.find(">", marker)
    if tag_end < 0:
        return None
    start = html.find("{", tag_end)
    if start < 0:
        return None
    raw = unescape(html[start:])
    try:
        data, _ = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_search_state(state: dict[str, Any], *, site_url: str = DEFAULT_HH_BASE_URL) -> list[VacancyPreview]:
    block = find_first(state, "vacancySearchResult")
    if not isinstance(block, dict):
        return []
    items = block.get("vacancies") or []
    if not isinstance(items, list):
        return []
    previews: list[VacancyPreview] = []
    seen: set[str] = set()
    for item in items:
        preview = _preview_from_item(item, site_url=site_url)
        if preview is None or preview.id in seen:
            continue
        seen.add(preview.id)
        previews.append(preview)
    return previews


def parse_search_html(html: str, *, site_url: str = DEFAULT_HH_BASE_URL) -> list[VacancyPreview]:
    previews: list[VacancyPreview] = []
    seen: set[str] = set()
    for match in _ANCHOR_RE.finditer(html):
        attrs = match.group(1)
        if "serp-item__title" not in attrs:
            continue
        href_match = re.search(r'href="([^"]+)"', attrs)
        if href_match is None:
            continue
        vacancy_id = _vacancy_id_from_href(href_match.group(1))
        if vacancy_id is None or vacancy_id in seen:
            continue
        title = _clean_text(match.group(2))
        if not title:
            continue
        window = html[match.end() : match.end() + 2000]
        company = _employer_from_window(window)
        seen.add(vacancy_id)
        previews.append(
            VacancyPreview(
                id=vacancy_id,
                title=title,
                company=company,
                url=canonical_vacancy_url(vacancy_id, site_url),
            )
        )
    return previews


def vacancy_details_from_view(
    view: dict[str, Any],
    vacancy_id: str,
    *,
    test_required: bool,
    fallback_title: str = "",
    fallback_company: str | None = None,
    site_url: str = DEFAULT_HH_BASE_URL,
) -> VacancyDetails:
    if view.get("archived") is True or view.get("status") == "archived":
        raise VacancyClosed(f"Вакансия {vacancy_id} в архиве")
    source = view
    nested = view.get("vacancy")
    if isinstance(nested, dict) and not any(key in view for key in ("description", "name", "vacancyId")):
        source = nested
    if source.get("archived") is True or source.get("status") == "archived":
        raise VacancyClosed(f"Вакансия {vacancy_id} в архиве")
    title = _as_text(source.get("name") or source.get("title") or fallback_title) or f"Вакансия {vacancy_id}"
    company = _company_name(source.get("company") or source.get("employer")) or fallback_company
    description = html_to_text(_as_text(source.get("description")))
    skills = tuple(_skill_names(source.get("keySkills") or source.get("skills")))
    salary = format_salary(source.get("compensation") or source.get("salary"))
    return VacancyDetails(
        id=str(vacancy_id),
        title=title,
        company=company,
        url=canonical_vacancy_url(str(vacancy_id), site_url),
        description=description,
        skills=skills,
        salary=salary,
        test_required=test_required or _view_requires_test(source) or _view_requires_test(view),
    )


def parse_vacancy_html_fallback(
    html: str,
    vacancy_id: str,
    *,
    test_required: bool,
    site_url: str = DEFAULT_HH_BASE_URL,
) -> VacancyDetails:
    title = ""
    title_match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if title_match is None:
        title_match = re.search(r'content="([^"]+)"[^>]+property="og:title"', html, re.IGNORECASE)
    if title_match:
        title = unescape(title_match.group(1)).strip()
    description = ""
    desc_at = html.find('data-qa="vacancy-description"')
    if desc_at >= 0:
        description = html_to_text(html[desc_at : desc_at + 30000])
    return VacancyDetails(
        id=str(vacancy_id),
        title=title or f"Вакансия {vacancy_id}",
        company=None,
        url=canonical_vacancy_url(str(vacancy_id), site_url),
        description=description,
        skills=(),
        salary=None,
        test_required=test_required,
    )


def popup_requires_test(data: dict[str, Any]) -> bool:
    test = data.get("test")
    if isinstance(test, dict) and (test.get("hasTests") or test.get("required")):
        return True
    return bool(data.get("hasTests") or data.get("testRequired") or data.get("userTestPresent"))


def extract_resumes_from_json(data: Any) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            resume_hash = node.get("hash") or node.get("resumeHash") or node.get("resume_hash")
            title = _as_text(node.get("title") or node.get("name"))
            if (
                isinstance(resume_hash, str)
                and _HASH_RE.fullmatch(resume_hash)
                and (
                    title
                    or "experience" in node
                    or "_attributes" in node
                    or "status" in node
                    or "skillSet" in node
                )
            ):
                found.append((resume_hash, title or None))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return _dedupe_hashes(found)


def parse_resume_list_html(html: str) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    for match in _ANCHOR_RE.finditer(html):
        attrs = match.group(1)
        if "resume" not in attrs.lower():
            continue
        href_match = re.search(r'href="([^"]+)"', attrs)
        if href_match is None:
            continue
        hash_match = re.search(r"/resume/([a-fA-F0-9]{8,64})", href_match.group(1))
        if hash_match is None:
            continue
        title = _clean_text(match.group(2)) or None
        found.append((hash_match.group(1), title))
    return _dedupe_hashes(found)


def resume_from_state(state: dict[str, Any] | None, fallback_hash: str, fallback_title: str | None) -> Resume:
    data = find_first(state, "applicantResume") if state else None
    if isinstance(data, dict):
        return resume_from_dict(data, fallback_hash, fallback_title)
    title = fallback_title or f"Резюме {fallback_hash[:8]}"
    return Resume(hash=fallback_hash.lower(), title=title, text=title)


def resume_from_dict(data: dict[str, Any], fallback_hash: str, fallback_title: str | None = None) -> Resume:
    resume_hash = str(data.get("hash") or data.get("resumeHash") or fallback_hash).lower()
    title = _as_text(data.get("title") or data.get("name") or fallback_title) or f"Резюме {resume_hash[:8]}"
    lines = [f"Должность: {title}"]
    skills = _skill_names(data.get("skillSet"))
    skills_text = _as_text(data.get("skills"))
    if skills or skills_text:
        joined = ", ".join(part for part in (", ".join(skills), skills_text) if part)
        lines.append(f"Навыки: {joined}")
    about = html_to_text(_as_text(data.get("about") or data.get("comment")))
    if about:
        lines.append(f"О себе: {about}")
    experience = data.get("experience")
    if isinstance(experience, list):
        lines.append("Опыт:")
        for item in experience:
            if not isinstance(item, dict):
                continue
            position = _as_text(item.get("position"))
            company = _company_name(item.get("company") or item.get("companyName") or item.get("employer"))
            start = _as_text(item.get("startDate") or item.get("start"))
            end = _as_text(item.get("endDate") or item.get("end"))
            period = f"{start} — {end or 'по настоящее время'}" if start or end else ""
            description = html_to_text(_as_text(item.get("description")))
            header = ", ".join(part for part in (position, company, period) if part)
            if header:
                lines.append(f"- {header}")
            if description:
                lines.append(description)
    education = data.get("education")
    if isinstance(education, dict):
        lines.extend(_education_lines(education))
    text = "\n".join(line for line in lines if line).strip()
    return Resume(hash=resume_hash, title=title, text=text or title)


def interpret_apply_response(status_code: int, body: str) -> ApplyResult:
    if status_code >= 400:
        return ApplyResult(False, f"HTTP {status_code}: {_compact(body)}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ApplyResult(False, "Ответ hh.ru не JSON")
    if not isinstance(data, dict):
        return ApplyResult(False, "Неожиданный ответ hh.ru")
    if str(data.get("type") or "").lower() in {"error", "fail"}:
        return ApplyResult(False, _error_text(data) or "hh.ru отклонил отклик")
    error = data.get("error") or data.get("errors")
    if error:
        return ApplyResult(False, _error_text(error) or _error_text(data) or "hh.ru отклонил отклик")
    return ApplyResult(True)


def html_to_text(value: str) -> str:
    if not value:
        return ""
    text = _BLOCK_RE.sub("\n", value)
    text = _TAG_RE.sub("", text)
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_salary(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, dict):
        return None
    amount_from = value.get("from") or value.get("amountFrom")
    amount_to = value.get("to") or value.get("amountTo")
    currency = _as_text(value.get("currencyCode") or value.get("currency") or "")
    parts: list[str] = []
    if amount_from and amount_to:
        parts.append(f"{amount_from}–{amount_to}")
    elif amount_from:
        parts.append(f"от {amount_from}")
    elif amount_to:
        parts.append(f"до {amount_to}")
    if currency:
        parts.append(currency)
    if value.get("gross") is True:
        parts.append("до вычета налогов")
    text = " ".join(parts).strip()
    return text or None


def find_first(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = find_first(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_first(item, key)
            if found is not None:
                return found
    return None


def canonical_vacancy_url(vacancy_id: str, site_url: str = DEFAULT_HH_BASE_URL) -> str:
    return f"{site_url.rstrip('/')}/vacancy/{vacancy_id}"


def looks_like_login(response: httpx.Response) -> bool:
    url = str(response.url).lower()
    return "/account/login" in url or "login.hh.ru" in url


def ensure_accessible(response: httpx.Response) -> None:
    if looks_like_login(response):
        raise AuthError("Сессия hh.ru недействительна")
    if response.status_code in {401, 403, 406, 429}:
        raise HHError(f"hh.ru вернул {response.status_code}")


class HHClient:
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        delay_seconds: float,
        timeout_seconds: float,
        base_url: str = DEFAULT_HH_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._delay = delay_seconds
        self._base_url = base_url.rstrip("/")
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            cookies=cookies,
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, url: str) -> list[VacancyPreview]:
        response = await self._request("GET", url)
        ensure_accessible(response)
        state = extract_lux_state(response.text)
        previews = parse_search_state(state, site_url=self._base_url) if state else []
        if previews:
            return previews
        previews = parse_search_html(response.text, site_url=self._base_url)
        if previews:
            return previews
        if state is not None:
            return []
        if _page_blocked(response):
            raise AuthError("hh.ru не отдал страницу поиска: возможна капча или истёкшая сессия")
        logger.warning("Не удалось разобрать страницу поиска %s", url)
        return []

    async def fetch_resumes(self) -> list[Resume]:
        stubs = await self._resume_stubs()
        if not stubs:
            return []
        resumes: list[Resume] = []
        for resume_hash, title in stubs:
            resumes.append(await self.fetch_resume(resume_hash, title))
        return resumes

    async def fetch_resume(self, resume_hash: str, fallback_title: str | None = None) -> Resume:
        response = await self._request("GET", f"/resume/{resume_hash}")
        ensure_accessible(response)
        state = extract_lux_state(response.text)
        resume = resume_from_state(state, resume_hash, fallback_title)
        if len(resume.text) < 80:
            plain = html_to_text(response.text)[:20000]
            if len(plain) > len(resume.text):
                resume = Resume(hash=resume.hash, title=resume.title, text=plain)
        return resume

    async def fetch_vacancy(
        self,
        vacancy_id: str,
        *,
        fallback_title: str,
        fallback_company: str | None,
    ) -> VacancyDetails:
        response = await self._request("GET", f"/vacancy/{vacancy_id}")
        if response.status_code == 404:
            raise VacancyClosed(f"Вакансия {vacancy_id} не найдена")
        ensure_accessible(response)
        test_required = await self._popup_requires_test(vacancy_id)
        state = extract_lux_state(response.text)
        view = find_first(state, "vacancyView") if state else None
        if isinstance(view, dict):
            return vacancy_details_from_view(
                view,
                vacancy_id,
                test_required=test_required,
                fallback_title=fallback_title,
                fallback_company=fallback_company,
                site_url=self._base_url,
            )
        details = parse_vacancy_html_fallback(
            response.text,
            vacancy_id,
            test_required=test_required,
            site_url=self._base_url,
        )
        if not details.description and _page_blocked(response):
            raise AuthError("hh.ru не отдал карточку вакансии")
        return details

    async def apply(self, vacancy_id: str, resume_hash: str, letter: str) -> ApplyResult:
        token = self._xsrf()
        if not token:
            raise AuthError("В сессии нет cookie _xsrf, отклик не отправлен")
        payload = {
            "_xsrf": token,
            "vacancy_id": vacancy_id,
            "resume_hash": resume_hash,
            "letter": letter,
            "ignore_postponed": "true",
            "incomplete": "false",
            "lux": "true",
            "withoutTest": "no",
        }
        response = await self._request(
            "POST",
            "/applicant/vacancy_response/popup",
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "X-Xsrftoken": token,
                "X-Hhtmfrom": "vacancy",
                "X-Hhtmsource": "vacancy_response",
                "Origin": self._base_url,
                "Referer": canonical_vacancy_url(vacancy_id, self._base_url),
            },
        )
        if looks_like_login(response):
            raise AuthError("Сессия hh.ru недействительна: отклик не принят")
        return interpret_apply_response(response.status_code, response.text)

    async def _resume_stubs(self) -> list[tuple[str, str | None]]:
        shard = await self._request(
            "GET",
            "/shards/applicant/resumes",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )
        ensure_accessible(shard)
        stubs: list[tuple[str, str | None]] = []
        try:
            payload = shard.json()
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            stubs.extend(extract_resumes_from_json(payload))
        if stubs:
            return _dedupe_hashes(stubs)
        page = await self._request("GET", "/applicant/resumes")
        ensure_accessible(page)
        state = extract_lux_state(page.text)
        if state is not None:
            stubs.extend(extract_resumes_from_json(state))
        stubs.extend(parse_resume_list_html(page.text))
        return _dedupe_hashes(stubs)

    async def _popup_requires_test(self, vacancy_id: str) -> bool:
        response = await self._request(
            "GET",
            "/applicant/vacancy_response/popup",
            params={"vacancyId": vacancy_id},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )
        ensure_accessible(response)
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return False
        if isinstance(payload, dict):
            return popup_requires_test(payload)
        return False

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self._lock:
            if self._delay:
                await asyncio.sleep(self._delay)
            target = self._on_configured_host(url)
            response = await self._client.request(method, target, **kwargs)
            logger.info("hh %s %s -> %s", method, target, response.status_code)
            return response

    async def requires_test(self, vacancy_id: str) -> bool:
        return await self._popup_requires_test(vacancy_id)

    def _on_configured_host(self, url: str) -> str:
        parsed = urlsplit(url)
        if not parsed.netloc or not is_hh_host(parsed.netloc):
            return url
        base = urlsplit(self._base_url)
        return urlunsplit(parsed._replace(scheme=base.scheme, netloc=base.netloc))

    def _xsrf(self) -> str:
        value = self._client.cookies.get("_xsrf")
        if value:
            return unquote(value)
        for cookie in self._client.cookies.jar:
            if cookie.name == "_xsrf" and cookie.value:
                return unquote(cookie.value)
        return ""


def _preview_from_item(item: Any, *, site_url: str = DEFAULT_HH_BASE_URL) -> VacancyPreview | None:
    if not isinstance(item, dict):
        return None
    source = item
    if "vacancyId" not in source and isinstance(source.get("vacancy"), dict):
        source = source["vacancy"]
    vacancy_id = source.get("vacancyId") or source.get("id")
    if vacancy_id is None:
        link = _vacancy_link(source)
        vacancy_id = _vacancy_id_from_href(link) if link else None
    if vacancy_id is None:
        return None
    vacancy_id = str(vacancy_id)
    if not vacancy_id.isdigit():
        extracted = _vacancy_id_from_href(vacancy_id)
        if extracted is None:
            return None
        vacancy_id = extracted
    title = _as_text(source.get("name") or source.get("title"))
    if not title:
        return None
    return VacancyPreview(
        id=vacancy_id,
        title=title,
        company=_company_name(source.get("company") or source.get("employer")),
        url=canonical_vacancy_url(vacancy_id, site_url),
    )


def _vacancy_link(source: dict[str, Any]) -> str:
    links = source.get("links")
    if isinstance(links, dict):
        for key in ("desktop", "alternateUrl", "url"):
            if isinstance(links.get(key), str):
                return links[key]
    for key in ("alternateUrl", "alternate_url", "url"):
        if isinstance(source.get(key), str):
            return source[key]
    return ""


def _vacancy_id_from_href(href: str) -> str | None:
    match = _VACANCY_ID_RE.search(href)
    if match:
        return match.group(1)
    return None


def _employer_from_window(window: str) -> str | None:
    for match in _ANCHOR_RE.finditer(window):
        if "vacancy-serp__vacancy-employer" not in match.group(1):
            continue
        company = _clean_text(match.group(2))
        if company:
            return company
    return None


def _company_name(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("visibleName", "name", "companyName"):
            text = _as_text(value.get(key))
            if text:
                return text
    return None


def _skill_names(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            text = _as_text(item.get("name") or item.get("string"))
            if text:
                names.append(text)
    return names


def _education_lines(education: dict[str, Any]) -> list[str]:
    lines = ["Образование:"]
    added = False
    for key in ("primary", "additional", "elementary", "attestation"):
        items = education.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            parts = [
                _as_text(item.get("name") or item.get("organization") or item.get("result")),
                _as_text(item.get("result") or item.get("faculty")),
                _as_text(item.get("year")),
            ]
            text = ", ".join(part for part in parts if part)
            if text:
                lines.append(f"- {text}")
                added = True
    return lines if added else []


def _view_requires_test(view: dict[str, Any]) -> bool:
    if view.get("testRequired") or view.get("userTestPresent"):
        return True
    test = view.get("test") or view.get("userTest")
    if isinstance(test, dict):
        return bool(test.get("required") or test.get("hasTests"))
    return False


def _dedupe_hashes(items: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    seen: set[str] = set()
    result: list[tuple[str, str | None]] = []
    for resume_hash, title in items:
        key = resume_hash.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append((key, title))
    return result


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        unpacked = _unpack_hh_string(text)
        if unpacked is not None:
            return _as_text(unpacked)
        return text
    if isinstance(value, dict) and "string" in value:
        return _as_text(value.get("string"))
    if isinstance(value, list) and value and all(_is_hh_string(item) for item in value):
        return "".join(_as_text(item) for item in value).strip()
    return str(value).strip()


def _is_hh_string(value: Any) -> bool:
    return isinstance(value, dict) and "string" in value


def _unpack_hh_string(text: str) -> Any | None:
    if len(text) < 2 or text[0] not in "[{":
        return None
    if "'string'" not in text and '"string"' not in text:
        return None
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError, MemoryError):
        return None
    if _is_hh_string(parsed):
        return parsed
    if isinstance(parsed, list) and parsed and all(_is_hh_string(item) for item in parsed):
        return parsed
    return None


def _clean_text(value: str) -> str:
    return html_to_text(value)


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:300]


def _error_text(value: Any) -> str:
    if isinstance(value, str):
        return _compact(value)[:500]
    if isinstance(value, dict):
        for key in ("message", "error", "reason", "text"):
            if value.get(key):
                return _error_text(value[key])
        return _compact(json.dumps(value, ensure_ascii=False))[:500]
    if isinstance(value, list):
        parts = [_error_text(item) for item in value[:3]]
        return "; ".join(part for part in parts if part)[:500]
    return ""


def _page_blocked(response: httpx.Response) -> bool:
    if response.status_code in {401, 403, 429, 503}:
        return True
    title_match = re.search(r"<title>(.*?)</title>", response.text[:5000], re.IGNORECASE | re.DOTALL)
    title = unescape(title_match.group(1)).lower() if title_match else ""
    return "captcha" in title or "робот" in title

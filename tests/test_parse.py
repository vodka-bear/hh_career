import json

import httpx
import pytest

from hh_career.hh import (
    HHClient,
    VacancyClosed,
    extract_lux_state,
    extract_resumes_from_json,
    html_to_text,
    interpret_apply_response,
    parse_resume_list_html,
    parse_search_html,
    parse_search_state,
    popup_requires_test,
    resume_from_dict,
    vacancy_details_from_view,
)


def _lux(payload: dict) -> str:
    escaped = json.dumps(payload, ensure_ascii=False).replace('"', "&quot;")
    return f'<html><template id="HH-Lux-InitialState">{escaped}</template></html>'


def test_lux_search_state() -> None:
    html = _lux(
        {
            "vacancySearchResult": {
                "vacancies": [
                    {
                        "vacancyId": 42,
                        "name": "Python",
                        "company": {"visibleName": "Acme"},
                    }
                ]
            }
        }
    )
    state = extract_lux_state(html)
    assert state is not None
    previews = parse_search_state(state)
    assert len(previews) == 1
    assert previews[0].id == "42"
    assert previews[0].title == "Python"
    assert previews[0].company == "Acme"
    assert previews[0].url == "https://hh.ru/vacancy/42"


def test_search_html_fallback() -> None:
    html = """
    <a data-qa="serp-item__title" href="https://hh.ru/vacancy/7?query=1">Backend</a>
    <a data-qa="vacancy-serp__vacancy-employer" href="/employer/1">Ромашка</a>
    """
    previews = parse_search_html(html)
    assert len(previews) == 1
    assert previews[0].id == "7"
    assert previews[0].title == "Backend"
    assert previews[0].company == "Ромашка"


def test_vacancy_view_and_archived() -> None:
    view = {
        "name": "Python",
        "description": "<p>Писать сервисы</p>",
        "keySkills": [{"name": "Django"}],
        "compensation": {"from": 200000, "currencyCode": "RUR", "gross": True},
        "company": {"visibleName": "Acme"},
    }
    details = vacancy_details_from_view(view, "42", test_required=False)
    assert "Писать сервисы" in details.description
    assert details.skills == ("Django",)
    assert details.salary is not None
    assert "200000" in details.salary
    assert details.company == "Acme"
    with pytest.raises(VacancyClosed):
        vacancy_details_from_view({"archived": True, "name": "Старая"}, "1", test_required=False)


def test_resume_json_and_html_list() -> None:
    stubs = extract_resumes_from_json(
        {"resumes": [{"hash": "abcdef1234567890", "title": "Backend", "_attributes": {}}]}
    )
    assert stubs == [("abcdef1234567890", "Backend")]
    html = '<a data-qa="resume-title" href="/resume/abcdef1234567890">Backend</a>'
    assert parse_resume_list_html(html)[0][0] == "abcdef1234567890"
    resume = resume_from_dict(
        {
            "hash": "abcdef1234567890",
            "title": "Backend",
            "skillSet": ["Python"],
            "experience": [{"position": "Dev", "companyName": "Acme", "description": "<p>API</p>"}],
        },
        "abcdef1234567890",
    )
    assert resume.title == "Backend"
    titled = resume_from_dict(
        {
            "hash": "abcdef1234567890",
            "title": [{"string": "Backend-разработчик (PHP, Laravel)"}],
        },
        "abcdef1234567890",
    )
    assert titled.title == "Backend-разработчик (PHP, Laravel)"
    stored = "[{'string': 'Backend-разработчик (PHP, Laravel, Bitrix, Yii2, JavaScript, Linux, SQL)'}]"
    assert resume_from_dict({"hash": "abcdef1234567890", "title": stored}, "abcdef1234567890").title == (
        "Backend-разработчик (PHP, Laravel, Bitrix, Yii2, JavaScript, Linux, SQL)"
    )
    assert "Python" in resume.text
    assert "API" in resume.text
    assert html_to_text("<p>A&amp;B</p>") == "A&B"


def test_popup_and_apply_response() -> None:
    assert popup_requires_test({"test": {"hasTests": True}})
    assert not popup_requires_test({"test": {"hasTests": False}})
    assert interpret_apply_response(200, '{"responseStatus":"ok"}').ok
    failed = interpret_apply_response(200, '{"error":"letter-required"}')
    assert not failed.ok
    assert failed.error is not None
    assert "letter-required" in failed.error


@pytest.mark.asyncio
async def test_search_reads_lux_with_session_cookies() -> None:
    html = _lux(
        {
            "vacancySearchResult": {
                "vacancies": [{"vacancyId": 5, "name": "Go", "company": {"name": "North"}}]
            }
        }
    )
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie", "")
        seen["accept"] = request.headers.get("accept", "")
        return httpx.Response(200, text=html)

    client = HHClient(
        {"_xsrf": "token", "hhtoken": "session"},
        delay_seconds=0,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    try:
        previews = await client.search("https://hh.ru/search/vacancy?text=go")
    finally:
        await client.aclose()
    assert [item.id for item in previews] == ["5"]
    assert previews[0].url == "https://hh.ru/vacancy/5"
    assert "_xsrf=token" in seen["cookie"]
    assert "hhtoken=session" in seen["cookie"]
    assert "application/json" not in seen["accept"]


@pytest.mark.asyncio
async def test_requests_stay_on_configured_host() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    client = HHClient(
        {"_xsrf": "token"},
        delay_seconds=0,
        timeout_seconds=5,
        base_url="https://krasnoyarsk.hh.ru",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.search("https://hh.ru/search/vacancy?text=go&page=0")
        await client.fetch_vacancy("5", fallback_title="Go", fallback_company=None)
    except Exception:
        pass
    finally:
        await client.aclose()
    assert seen
    assert all(url.startswith("https://krasnoyarsk.hh.ru/") for url in seen)
    assert any(url.startswith("https://krasnoyarsk.hh.ru/search/vacancy?") for url in seen)
    assert "https://krasnoyarsk.hh.ru/vacancy/5" in seen

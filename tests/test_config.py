from urllib.parse import parse_qs, urlparse

import pytest

from hh_career.config import SearchFilter, is_hh_host, load_filters, load_settings, search_page_urls, with_page


def test_with_page_replaces_existing_page_and_keeps_repeated_params() -> None:
    url = with_page("https://spb.hh.ru/search/vacancy?text=python&area=1&area=2&page=9", 0)
    query = parse_qs(urlparse(url).query)
    assert query["page"] == ["0"]
    assert query["area"] == ["1", "2"]
    assert query["text"] == ["python"]

    pages = search_page_urls(SearchFilter("python", url, 2))
    assert [parse_qs(urlparse(item).query)["page"] for item in pages] == [["0"], ["1"]]


def test_hh_host() -> None:
    assert is_hh_host("hh.ru")
    assert is_hh_host("spb.hh.ru")
    assert not is_hh_host("hh.ru.evil.com")
    assert not is_hh_host("evilhh.ru")


def test_load_settings_and_filters(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("HH_COOKIES", "_xsrf=a; hhtoken=b")
    filters = tmp_path / "filters.yaml"
    filters.write_text(
        "searches:\n  - name: python\n    url: https://hh.ru/search/vacancy?text=python\n    pages: 1\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "poll_interval_minutes: 3",
                "min_match_percent: 80",
                "filters_path: filters.yaml",
                "database_path: data/db.sqlite",
                "openai:",
                "  base_url: https://example.test/v1",
                "  model: demo",
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.poll_interval_minutes == 3
    assert settings.min_match_percent == 80
    assert settings.filters_path == filters
    assert settings.database_path == tmp_path / "data" / "db.sqlite"
    assert settings.openai_model == "demo"
    assert settings.hh_base_url == "https://hh.ru"
    assert settings.cookies["hhtoken"] == "b"
    assert load_filters(filters)[0].name == "python"


def test_filter_host_must_be_hh(tmp_path) -> None:
    path = tmp_path / "filters.yaml"
    path.write_text(
        "searches:\n  - name: bad\n    url: https://example.com/search\n    pages: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hh.ru"):
        load_filters(path)


def test_hh_base_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HH_COOKIES", "_xsrf=a; hhtoken=b")
    config = tmp_path / "config.yaml"
    config.write_text("hh_base_url: https://krasnoyarsk.hh.ru/\n", encoding="utf-8")
    assert load_settings(config).hh_base_url == "https://krasnoyarsk.hh.ru"
    config.write_text("hh_base_url: https://example.com\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hh_base_url"):
        load_settings(config)


def test_missing_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    config = tmp_path / "config.yaml"
    config.write_text("poll_interval_minutes: 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        load_settings(config)

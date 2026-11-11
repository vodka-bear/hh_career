import json

import pytest

from hh_career.cookies import parse_cookies


def test_header_cookies() -> None:
    cookies = parse_cookies(" _xsrf=abc; hhtoken=token; hhuid=uid ")
    assert cookies == {"_xsrf": "abc", "hhtoken": "token", "hhuid": "uid"}


def test_cookie_editor_json() -> None:
    raw = json.dumps(
        [
            {"name": "_xsrf", "value": "token"},
            {"name": "hhtoken", "value": "session"},
            {"name": "skip", "value": "значение"},
        ]
    )
    cookies = parse_cookies(raw)
    assert cookies["_xsrf"] == "token"
    assert cookies["hhtoken"] == "session"
    assert "skip" not in cookies


def test_newline_separated_cookies() -> None:
    cookies = parse_cookies("_xsrf=a\nhhuid=b\n")
    assert cookies["_xsrf"] == "a"
    assert cookies["hhuid"] == "b"


def test_missing_xsrf() -> None:
    with pytest.raises(ValueError, match="_xsrf"):
        parse_cookies("hhtoken=abc")


def test_missing_auth_cookie() -> None:
    with pytest.raises(ValueError, match="hhtoken"):
        parse_cookies("_xsrf=abc")

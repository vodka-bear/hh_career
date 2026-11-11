import pytest

from hh_career.ai import is_suitable, load_json_object, parse_match_payload
from hh_career.bot import preview_messages
from hh_career.models import Resume


def test_match_selects_known_resume_and_threshold() -> None:
    resumes = [
        Resume(hash="aaaabbbb", title="Первое", text="Python"),
        Resume(hash="ccccdddd", title="Второе", text="Java"),
    ]
    match = parse_match_payload(
        {
            "best_resume_hash": "CCCCDDDD",
            "score": 81.2,
            "reason": "есть Java",
            "short_description": "Нужен Java-разработчик в команду платежей.",
        },
        resumes,
    )
    assert match.resume_hash == "ccccdddd"
    assert match.resume_title == "Второе"
    assert match.score == 81
    assert is_suitable(match.score, 70)
    assert not is_suitable(69, 70)
    assert is_suitable(70, 70)
    with pytest.raises(ValueError, match="резюме"):
        parse_match_payload({"best_resume_hash": "missing", "score": 90}, resumes)


def test_json_object_can_be_wrapped_in_prose() -> None:
    payload = load_json_object('Оценка готова:\n{"score": 70, "best_resume_hash": "aaaabbbb"}')
    assert payload["score"] == 70


def test_preview_contains_edit_hint() -> None:
    parts = preview_messages("Здравствуйте. Готов обсудить вакансию.")
    assert len(parts) == 1
    assert "Сопроводительное письмо" in parts[0]
    assert "что изменить" in parts[0]

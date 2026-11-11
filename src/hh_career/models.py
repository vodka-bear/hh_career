from dataclasses import dataclass

STATUS_BELOW_THRESHOLD = "below_threshold"
STATUS_PENDING_NOTIFY = "pending_notify"
STATUS_NOTIFIED = "notified"
STATUS_DRAFT = "draft"
STATUS_APPLIED = "applied"
STATUS_APPLY_FAILED = "apply_failed"


@dataclass(frozen=True)
class Resume:
    hash: str
    title: str
    text: str


@dataclass(frozen=True)
class VacancyPreview:
    id: str
    title: str
    company: str | None
    url: str


@dataclass(frozen=True)
class VacancyDetails:
    id: str
    title: str
    company: str | None
    url: str
    description: str
    skills: tuple[str, ...]
    salary: str | None
    test_required: bool


@dataclass
class VacancyRecord:
    id: str
    url: str
    title: str
    company: str | None
    salary: str | None
    description: str
    status: str
    score: int | None
    resume_hash: str | None
    resume_title: str | None
    match_reason: str | None
    short_description: str | None
    letter: str | None
    filter_name: str | None
    test_required: bool
    preview_chat_id: int | None
    preview_message_id: int | None
    preview_overflow_message_id: int | None
    apply_error: str | None
    first_seen: str
    updated_at: str
    applied_at: str | None


@dataclass(frozen=True)
class MatchResult:
    resume_hash: str
    resume_title: str
    score: int
    reason: str
    short_description: str


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    error: str | None = None

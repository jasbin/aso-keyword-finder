"""Domain models shared by every layer (engine, storage, API, CLI)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_term(term: str) -> str:
    """Canonical keyword form: lowercase, single spaces, trimmed."""
    return " ".join(term.lower().split())


class Store(str, Enum):
    APPSTORE = "appstore"
    PLAY = "play"


class AppSummary(BaseModel):
    """One app ranking for a keyword (used for difficulty)."""

    app_id: str
    title: str
    developer: str = ""
    ratings: int = Field(default=0, ge=0)
    rating: float | None = None
    url: str | None = None


class KeywordResult(BaseModel):
    keyword: str
    popularity: int = Field(ge=0, le=100)
    difficulty: int | None = Field(default=None, ge=0, le=100)
    opportunity: int | None = Field(default=None, ge=0, le=100)
    chars_typed: int | None = None  # shortest typed prefix that surfaced the keyword
    position: int | None = None  # 1-based rank within that suggestion list
    top_apps: list[AppSummary] = Field(default_factory=list)


_COUNTRY_RE = re.compile(r"^[a-z]{2}$")
_LANG_RE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$")


class ResearchParams(BaseModel):
    seed: str = Field(min_length=1, max_length=100)
    store: Store = Store.APPSTORE
    country: str = "us"
    language: str = "en"
    limit: int = Field(default=50, ge=5, le=300)
    depth: int = Field(default=1, ge=1, le=2)
    probe: bool = True
    difficulty: bool = True

    @field_validator("seed")
    @classmethod
    def _seed(cls, v: str) -> str:
        v = normalize_term(v)
        if not v:
            raise ValueError("seed keyword is empty")
        return v

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        v = v.strip().lower()
        if not _COUNTRY_RE.match(v):
            raise ValueError("country must be a two-letter ISO code, e.g. 'us'")
        return v

    @field_validator("language")
    @classmethod
    def _language(cls, v: str) -> str:
        v = v.strip().lower()
        if not _LANG_RE.match(v):
            raise ValueError("language must look like 'en' or 'en-gb'")
        return v


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)


class Stage(str, Enum):
    QUEUED = "queued"
    EXPANDING = "expanding"
    PROBING = "probing"
    DIFFICULTY = "difficulty"
    DONE = "done"


class Progress(BaseModel):
    stage: Stage = Stage.QUEUED
    done: int = 0  # units finished within the current stage
    total: int = 0  # units in the current stage (0 = unknown)
    keywords: int = 0  # keywords discovered so far
    requests: int = 0  # network requests made
    cache_hits: int = 0


class ResearchRun(BaseModel):
    id: str
    params: ResearchParams
    status: RunStatus = RunStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: Progress = Field(default_factory=Progress)
    error: str | None = None
    results: list[KeywordResult] = Field(default_factory=list)


class RunSummary(BaseModel):
    """History listing entry (no results payload)."""

    id: str
    params: ResearchParams
    status: RunStatus
    created_at: datetime
    finished_at: datetime | None = None
    keyword_count: int = 0
    error: str | None = None

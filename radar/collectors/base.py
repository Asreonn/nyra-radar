"""Collector interface. Collectors are pure: SourceSpec in, FetchResult out."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from radar.config import Config
from radar.net import Http
from radar.normalization.models import RawItem


@dataclass
class SourceSpec:
    id: int
    kind: str
    identifier: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    last_success_at: datetime | None = None

    @property
    def trial(self) -> bool:
        return self.status == "trial"

    def limit(self, default: int) -> int:
        value = int(self.config.get("max_items", default))
        return min(value, 15) if self.trial else value


@dataclass
class FetchResult:
    items: list[RawItem] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    state: dict[str, Any] | None = None  # replaces Source.state when not None
    status_code: int | None = None
    not_modified: bool = False


class CollectorError(Exception):
    def __init__(self, message: str, status_code: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.permanent = permanent


class Deferred(CollectorError):
    """The source asked us to come back later (rate limit). Not a failure: no backoff, no error shown."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message, 429)
        self.retry_after = retry_after


class MissingCredentials(CollectorError):
    def __init__(self, message: str):
        super().__init__(message, permanent=True)


class Collector(ABC):
    kind: ClassVar[str]
    content_type: ClassVar[str] = "article"
    requires_credentials: ClassVar[bool] = False

    def __init__(self, http: Http, config: Config):
        self.http = http
        self.config = config

    @abstractmethod
    async def fetch(self, source: SourceSpec) -> FetchResult:
        """Fetch new/updated items for one source. Must not write to the DB."""

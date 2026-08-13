"""Metadata filtering and secret detection for memory.

Project isolation is a security/correctness boundary: a memory belonging to
Project A must never surface in Project B's results unless explicitly requested.
Filters are enforced at the store layer (WHERE clauses), not as after-the-fact
pruning, so a misconfigured query cannot leak across projects.

Secret detection rejects credential-like values before they are ever persisted.
When detection is uncertain we prefer NOT to persist (fail closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from kinetic.memory.models import MemoryScope


class TagMatch(StrEnum):
    ANY = "any"
    ALL = "all"


@dataclass
class MemoryFilter:
    """Structured filter for memory queries.

    All fields are optional; a ``None``/empty field means "no constraint".
    ``project_id``/``workspace_id`` default to a sentinel rather than None when
    isolation is desired — see :meth:`for_project`.
    """

    memory_types: set[MemoryScope] = field(default_factory=set)
    project_id: str | None = None
    workspace_id: str | None = None
    source: str | None = None
    tags: set[str] = field(default_factory=set)
    tag_match: TagMatch = TagMatch.ANY
    include_invalidated: bool = False
    created_after: datetime | None = None
    created_before: datetime | None = None

    @classmethod
    def for_project(cls, project_id: str | None, **kwargs: object) -> MemoryFilter:
        """A filter locked to a single project scope."""
        return cls(project_id=project_id, **kwargs)  # type: ignore[arg-type]

    def matches_tags(self, tags: list[str]) -> bool:
        if not self.tags:
            return True
        tag_set = set(tags)
        if self.tag_match == TagMatch.ALL:
            return self.tags.issubset(tag_set)
        return bool(self.tags & tag_set)


# ---------------------------------------------------------------------------
# Secret detection
# ---------------------------------------------------------------------------

# Intentionally conservative patterns. False positives are acceptable (a memory
# is rejected, which is safe); false negatives are the risk, so patterns err on
# the side of matching credential-like shapes.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"(?i)api[_-]?key")),
    ("secret_key", re.compile(r"(?i)secret[_-]?key")),
    ("access_token", re.compile(r"(?i)access[_-]?token")),
    ("auth_token", re.compile(r"(?i)(auth[_-]?token|bearer)")),
    ("password", re.compile(r"(?i)(password|passwd|pwd)")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----")),
    ("client_secret", re.compile(r"(?i)client[_-]?secret")),
    # High-entropy credential blobs: long hex/base64 strings typical of tokens.
    ("token_blob", re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")),
    # Common cloud provider credential prefixes.
    ("aws", re.compile(r"(?i)AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"(?i)gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"(?i)xox[baprs]-[A-Za-z0-9-]{10,}")),
    # "password = value" / "key: value" assignment shapes.
    ("assignment", re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S+")),
]


@dataclass(frozen=True)
class SecretMatch:
    kind: str
    pattern: str
    snippet: str


class SecretDetector:
    """Detects credential-like content so it is never persisted as memory."""

    def __init__(self, *, min_length: int = 8) -> None:
        self._min_length = min_length

    def detect(self, text: str) -> list[SecretMatch]:
        if not text or len(text.strip()) < self._min_length:
            return []
        matches: list[SecretMatch] = []
        for kind, pattern in _SECRET_PATTERNS:
            for m in pattern.finditer(text):
                snippet = m.group(0)
                # Mask the matched value in the recorded snippet.
                masked = snippet[:4] + "***" if len(snippet) > 8 else "***"
                matches.append(SecretMatch(kind=kind, pattern=pattern.pattern, snippet=masked))
        return matches

    def contains_secret(self, text: str) -> bool:
        return bool(self.detect(text))


DEFAULT_SECRET_DETECTOR = SecretDetector()

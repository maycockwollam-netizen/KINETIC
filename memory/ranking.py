"""Ranking: combine retrieval signals into a final score.

Signals: semantic similarity, lexical overlap, recency, importance. Confidence
modulates the final score so more-trusted memories outrank less-trusted ones
when scores are close — this is how conflicting memories are resolved (stale,
low-confidence facts do not silently tie with fresh trusted facts).

No single signal can fully dominate unless its weight is explicitly set near 1
and the others near 0. Weights are normalized so they always sum to 1 before
scoring, keeping behavior predictable regardless of the raw config values.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from pydantic import BaseModel, model_validator

from memory.models import MemoryRecord, RankedMemory


class RankingWeights(BaseModel):
    """Configurable hybrid-retrieval weights (raw; normalized at scoring time)."""

    semantic: float = 0.4
    lexical: float = 0.3
    recency: float = 0.15
    importance: float = 0.15

    @model_validator(mode="after")
    def _non_negative(self) -> RankingWeights:
        for name in ("semantic", "lexical", "recency", "importance"):
            v = getattr(self, name)
            if v < 0:
                raise ValueError(f"{name} weight must be non-negative")
        if self.semantic + self.lexical + self.recency + self.importance <= 0:
            raise ValueError("at least one retrieval weight must be positive")
        return self

    def normalized(self) -> dict[str, float]:
        total = self.semantic + self.lexical + self.recency + self.importance
        return {
            "semantic": self.semantic / total,
            "lexical": self.lexical / total,
            "recency": self.recency / total,
            "importance": self.importance / total,
        }


# Recency half-life: a memory accessed ~7 days ago scores ~0.5.
RECENCY_HALF_LIFE_DAYS = 7.0


def recency_score(record: MemoryRecord, *, now: datetime) -> float:
    """Exponential decay over time since last access/update, in [0, 1]."""
    ref = record.updated_at or record.created_at
    ts = _parse_iso(ref)
    if ts is None:
        return 0.0
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Ranker:
    """Combines per-candidate signals into a ranked list.

    Candidates carry pre-computed signals in a dict keyed by signal name
    ('semantic', 'lexical'); recency and importance are derived from the
    record itself. Confidence modulates the final score.
    """

    def __init__(self, weights: RankingWeights | None = None) -> None:
        self._weights = weights or RankingWeights()

    def rank(
        self,
        candidates: list[tuple[MemoryRecord, dict[str, float]]],
        *,
        now: datetime | None = None,
        limit: int = 10,
    ) -> list[RankedMemory]:
        now = now or datetime.now(UTC)
        nw = self._weights.normalized()
        ranked: list[RankedMemory] = []
        for record, signals in candidates:
            sem = float(signals.get("semantic", 0.0))
            lex = float(signals.get("lexical", 0.0))
            rec = recency_score(record, now=now)
            imp = float(record.importance)
            base = (
                sem * nw["semantic"]
                + lex * nw["lexical"]
                + rec * nw["recency"]
                + imp * nw["importance"]
            )
            # Confidence modulates without zeroing a genuinely relevant memory.
            final = base * (0.5 + 0.5 * float(record.confidence))
            sources = [k for k, v in signals.items() if v > 0.0]
            ranked.append(
                RankedMemory(
                    record=record,
                    final_score=final,
                    components={
                        "semantic": sem,
                        "lexical": lex,
                        "recency": rec,
                        "importance": imp,
                        "confidence": float(record.confidence),
                        "base": base,
                    },
                    source_matches=sources,
                )
            )
        ranked.sort(key=lambda r: r.final_score, reverse=True)
        return ranked[:limit]

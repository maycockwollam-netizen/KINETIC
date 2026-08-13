"""Candidate retrieval: gather lexical + semantic candidates, then rank.

The Retriever is the bridge between the store (which knows about persistence)
and the ranker (which combines signals). It does NOT make policy decisions —
scope/isolation is enforced by the MemoryFilter passed through to the store.

Failure safety: a failing signal (e.g. a missing embedding) yields an empty
candidate set for that signal rather than aborting the whole query. The manager
decides what to do with a degraded result.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kinetic.memory.embeddings import EmbeddingProvider
from kinetic.memory.metadata import MemoryFilter
from kinetic.memory.models import RankedMemory
from kinetic.memory.ranking import Ranker
from kinetic.memory.store import MemoryStore


class Retriever:
    """Hybrid candidate retriever over a :class:`MemoryStore`."""

    def __init__(
        self,
        store: MemoryStore,
        embeddings: EmbeddingProvider,
        ranker: Ranker | None = None,
        *,
        candidate_limit: int = 50,
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._ranker = ranker or Ranker()
        self._candidate_limit = candidate_limit

    @property
    def ranker(self) -> Ranker:
        return self._ranker

    async def retrieve(
        self,
        query: str,
        *,
        filter: MemoryFilter | None = None,
        limit: int = 10,
    ) -> list[RankedMemory]:
        """Run hybrid retrieval and return ranked memories.

        Candidates from lexical and semantic search are merged by id; each
        candidate carries the max signal value observed across sources (so a
        memory found by both channels is not double-counted but keeps its best
        score per signal).
        """
        candidates: dict[str, tuple[Any, dict[str, float]]] = {}

        # Lexical signal. A store-level failure propagates so callers can
        # degrade gracefully (no fabricated results); a per-signal anomaly
        # (e.g. empty query) simply yields no candidates.
        try:
            for rec, score in self._store.search_lexical(
                query, filter, limit=self._candidate_limit
            ):
                entry = candidates.setdefault(rec.id, (rec, {}))
                entry[1]["lexical"] = max(entry[1].get("lexical", 0.0), score)
        except Exception:
            # Re-raise store/backend failures so the manager can emit + raise
            # MemoryError and the context engine can degrade. We do NOT swallow
            # them silently — that would hide a broken backend as "no results".
            raise

        # Semantic signal.
        query_vec = self._embeddings.embed(query)
        for rec, score in self._store.search_vector(
            query_vec, filter, limit=self._candidate_limit
        ):
            entry = candidates.setdefault(rec.id, (rec, {}))
            entry[1]["semantic"] = max(entry[1].get("semantic", 0.0), score)

        merged = [(rec, signals) for rec, signals in candidates.values()]
        return self._ranker.rank(merged, now=datetime.now(UTC), limit=limit)

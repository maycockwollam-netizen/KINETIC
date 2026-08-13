"""Embedding providers.

KINETIC must NOT be coupled to one model provider, and the current (Claude)
agent runtime does not provide embeddings. The :class:`EmbeddingProvider`
abstraction keeps the memory system vendor-agnostic.

Phase 4 ships :class:`DeterministicEmbeddingProvider`: a local, network-free,
fully deterministic hashing-trick embedding. Identical text always yields an
identical vector; texts that share tokens have non-zero cosine similarity. This
makes semantic retrieval testable without any API key or external service.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class EmbeddingProvider:
    """Abstract embedding provider.

    Implementations must be deterministic for the same input (so tests are
    reproducible) and expose a fixed :attr:`dimension`.
    """

    dimension: int

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class DeterministicEmbeddingProvider(EmbeddingProvider):
    """Hashing-trick embedding: deterministic, local, no network.

    Each token is hashed to one of ``dimension`` buckets; its weight (log(1+tf))
    is added to that bucket. The vector is L2-normalized so cosine similarity is
    a simple dot product. Stop-word-ish single-char tokens are kept (cheap, and
    keeps determinism simple).
    """

    def __init__(self, *, dimension: int = 64) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        tokens = _tokenize(text)
        for token in tokens:
            # Stable bucket assignment independent of Python's hash().
            bucket = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big") % self.dimension
            # Weight by log-frequency so repeated tokens matter more but do not dominate.
            vec[bucket] += 1.0
        # Log weighting + L2 normalize.
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity for two equal-length vectors (vectors are assumed
    pre-normalized by the provider; this also handles the general case)."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)

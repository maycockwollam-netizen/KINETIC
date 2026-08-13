"""Pluggable memory providers (embeddings, vector backends).

Phase 4 ships only the deterministic local embedding provider; this package is
the extension point for future production providers (e.g. OpenAI/Anthropic
embeddings, a hosted vector DB). No provider here requires a paid external
service, and none are auto-imported.
"""

from kinetic.memory.embeddings import DeterministicEmbeddingProvider, EmbeddingProvider

__all__ = ["DeterministicEmbeddingProvider", "EmbeddingProvider"]

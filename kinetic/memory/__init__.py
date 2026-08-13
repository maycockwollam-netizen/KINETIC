"""Memory & Context Engine.

A selective, hybrid-retrieval memory system. Memory is NOT a raw conversation
dump: only validated facts become persistent memory. Retrieval combines lexical,
semantic, metadata, and recency signals behind a pluggable store/embedding
abstraction so no specific vector-DB vendor is required.
"""

from kinetic.memory.embeddings import (
    DeterministicEmbeddingProvider,
    EmbeddingProvider,
)
from kinetic.memory.lifecycle import MemoryManager
from kinetic.memory.models import MemoryRecord, MemoryScope
from kinetic.memory.ranking import Ranker, RankingWeights
from kinetic.memory.retrieval import Retriever
from kinetic.memory.store import MemoryStore, SQLiteStore

__all__ = [
    "DeterministicEmbeddingProvider",
    "EmbeddingProvider",
    "MemoryManager",
    "MemoryRecord",
    "MemoryScope",
    "MemoryStore",
    "Ranker",
    "RankingWeights",
    "Retriever",
    "SQLiteStore",
]

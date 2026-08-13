"""Memory & Context Engine.

A selective, hybrid-retrieval memory system. Memory is NOT a raw conversation
dump: only validated facts become persistent memory. Retrieval combines lexical,
semantic, metadata, and recency signals behind a pluggable store/embedding
abstraction so no specific vector-DB vendor is required.
"""

from memory.embeddings import (
    DeterministicEmbeddingProvider,
    EmbeddingProvider,
)
from memory.lifecycle import MemoryManager
from memory.models import MemoryRecord, MemoryScope
from memory.ranking import Ranker, RankingWeights
from memory.retrieval import Retriever
from memory.store import MemoryStore, SQLiteStore

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

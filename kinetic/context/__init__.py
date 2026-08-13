"""Context engine: assemble a bounded, selective context package for the agent.

The context engine must NEVER dump the entire conversation, repository, memory
database, or event history into the model context. It collects current task
context, retrieves relevant memories, gathers project metadata + workspace
state + recent events, then ranks and trims to a configurable budget —
recording what was omitted.

Failure safety: if memory retrieval fails, a degraded but valid context (empty
memories, with a note) is returned. The engine never fabricates memories.
"""

from kinetic.context.budget import ContextBudget
from kinetic.context.engine import ContextEngine
from kinetic.context.models import ContextPackage, ContextSection, OmissionRecord

__all__ = [
    "ContextBudget",
    "ContextEngine",
    "ContextPackage",
    "ContextSection",
    "OmissionRecord",
]

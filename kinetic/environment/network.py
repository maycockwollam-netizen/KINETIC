"""Network policy for sandboxed environments.

The policy is *enforced by the runtime*, not merely communicated to the agent.
``DENY`` is the safe default. A runtime that cannot enforce a requested policy
must fail closed rather than pretend isolation exists.
"""

from __future__ import annotations

from enum import StrEnum


class NetworkPolicy(StrEnum):
    """How much network access a sandboxed process may have.

    * ``DENY`` — no outbound network at all (default).
    * ``ALLOW`` — unrestricted outbound network.
    * ``RESTRICTED`` — outbound only to an explicit allowlist of hosts/ports.
      The allowlist is provider-agnostic; the runtime decides how to realize it
      (e.g. a custom Docker network, an egress proxy). No single provider is
      hardcoded.
    """

    DENY = "deny"
    ALLOW = "allow"
    RESTRICTED = "restricted"


DEFAULT_NETWORK_POLICY = NetworkPolicy.DENY


class NetworkRule:
    """A single egress rule for ``RESTRICTED`` mode.

    ``host`` may be a hostname or IP; ``port`` 0/None means any port.
    Kept intentionally simple — the runtime maps this onto its isolation
    mechanism. No proxy infrastructure is mandated.
    """

    __slots__ = ("host", "port")

    def __init__(self, host: str, port: int | None = None) -> None:
        if not host:
            raise ValueError("NetworkRule requires a non-empty host")
        self.host = host
        self.port = int(port) if port else None

    def to_dict(self) -> dict[str, object]:
        return {"host": self.host, "port": self.port}

"""Environment-variable policy for sandboxed processes.

The host environment is NEVER forwarded automatically. Only explicitly allowed
variables are injected; denied variables are stripped even if allowed. Secret
patterns are filtered out of any value that *is* forwarded, so credentials are
never leaked into a sandbox unless explicitly configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Patterns that look like credentials. Matching variable *names* are denied by
# default. This is defense-in-depth: even if a name is allowlisted, the value
# is scanned for obvious secret shapes and redacted.
SECRET_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i).*(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL).*$"),
)
# Patterns that look like secret *values* (kept conservative to avoid noise).
SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)

# Variables that are always denied regardless of allowlist, because they
# commonly carry host credentials or are gateways to them.
_DEFAULT_DENIED: frozenset[str] = frozenset(
    {
        # Cloud provider credentials
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        # Generic API/secret carriers
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "BITBUCKET_TOKEN",
        # SSH / host identity
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "HOME",
        "USER",
        "USERNAME",
        "LOGNAME",
    }
)


@dataclass
class EnvironmentVariablePolicy:
    """Controls which host variables reach a sandboxed process."""

    #: Names explicitly allowed to be forwarded. If non-empty, only these (minus
    #: denied/secret names) are forwarded. If empty, NO host variable is forwarded.
    allowed: set[str] = field(default_factory=set)
    #: Names always stripped, even if present in ``allowed``.
    denied: set[str] = field(default_factory=lambda: set(_DEFAULT_DENIED))
    #: Extra variables to inject verbatim (never sourced from the host env).
    inject: dict[str, str] = field(default_factory=dict)

    def filter(self, host_env: dict[str, str]) -> dict[str, str]:
        """Return the sanitized environment for a sandboxed process.

        The host environment is the input; the output contains only allowed,
        non-denied, non-secret-named values plus explicitly injected variables.
        Secret-*named* variables are denied even if allowlisted, so credentials
        are never forwarded unless explicitly *injected*.
        """
        out: dict[str, str] = {}
        for name, value in host_env.items():
            if name in self.denied:
                continue
            if self.is_secret_name(name):
                continue
            if name in self.allowed:
                out[name] = self._sanitize_value(name, value)
        # Explicit injections override host values but are still name-checked.
        for name, value in self.inject.items():
            if name in self.denied:
                continue
            out[name] = self._sanitize_value(name, value)
        return out

    @staticmethod
    def is_secret_name(name: str) -> bool:
        return any(p.match(name) for p in SECRET_NAME_PATTERNS)

    @staticmethod
    def _sanitize_value(name: str, value: str) -> str:
        if EnvironmentVariablePolicy.is_secret_name(name):
            return "<redacted:secret-named-var>"
        for p in SECRET_VALUE_PATTERNS:
            if p.search(value):
                return "<redacted:secret-value>"
        return value


DEFAULT_ENV_VAR_POLICY = EnvironmentVariablePolicy()

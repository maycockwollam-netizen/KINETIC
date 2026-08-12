"""Unit tests for the permission policy."""

from __future__ import annotations

import pytest

from kinetic.errors import PermissionDeniedError
from kinetic.security import Capability, PermissionPolicy
from kinetic.security.policy import FILE_WRITE, NETWORK, READ_ONLY, ToolPermission


def test_read_only_allowed():
    policy = PermissionPolicy(writable_roots=[])
    d = policy.evaluate("read_file", READ_ONLY, {"path": "x"})
    assert d.allowed


def test_network_denied_by_default():
    policy = PermissionPolicy(allow_network=False)
    d = policy.evaluate("fetch", NETWORK, {"url": "http://x"})
    assert not d.allowed
    assert "network" in d.reason.lower()


def test_network_allowed_when_enabled():
    policy = PermissionPolicy(allow_network=True)
    assert policy.evaluate("fetch", NETWORK, {"url": "http://x"}).allowed


def test_write_within_root_allowed(tmp_path):
    policy = PermissionPolicy(writable_roots=[tmp_path], allow_network=False)
    d = policy.evaluate("write_file", FILE_WRITE, {"path": str(tmp_path / "a.txt")})
    assert d.allowed


def test_write_outside_root_denied(tmp_path, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    policy = PermissionPolicy(writable_roots=[tmp_path], allow_network=False)
    d = policy.evaluate("write_file", FILE_WRITE, {"path": str(other / "evil.txt")})
    assert not d.allowed
    assert "writable" in d.reason.lower()


def test_path_traversal_denied(tmp_path):
    policy = PermissionPolicy(writable_roots=[tmp_path])
    d = policy.evaluate("write_file", FILE_WRITE, {"path": str(tmp_path / ".." / "escape")})
    # "../escape" resolves outside; the policy only checks writable roots, but
    # the filesystem tool enforces traversal too. Here we assert policy denies
    # anything not under a writable root when a root is configured.
    assert not d.allowed


def test_require_raises_on_deny(tmp_path):
    policy = PermissionPolicy(writable_roots=[tmp_path], allow_network=False)
    with pytest.raises(PermissionDeniedError):
        policy.require("fetch", NETWORK, {"url": "x"})


def test_capability_flags_compose():
    combo = Capability.WRITE_FS | Capability.READ_FS
    assert Capability.READ_FS in combo
    assert Capability.NETWORK not in combo
    perm = ToolPermission(capabilities=combo)
    assert Capability.WRITE_FS in perm.capabilities

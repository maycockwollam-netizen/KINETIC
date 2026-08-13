"""Unit tests for config + audit log."""

from __future__ import annotations

import json

from config import Settings
from security import AuditLog


def test_settings_defaults(tmp_path):
    s = Settings(
        workspace_root=tmp_path / "ws",
        session_root=tmp_path / "ss",
        audit_log_path=tmp_path / "audit.log",
    )
    s.ensure_directories()
    assert s.workspace_root.exists()
    assert s.session_root.exists()
    assert s.audit_log_path.parent.exists()


def test_settings_writable_roots(tmp_path):
    s = Settings(
        workspace_root=tmp_path / "ws",
        session_root=tmp_path / "ss",
        audit_log_path=tmp_path / "audit.log",
        allowed_writable_roots=[tmp_path / "extra"],
    )
    roots = s.writable_roots()
    assert (tmp_path / "extra").resolve() in roots


def test_audit_log_appends_jsonl(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record(session_id="s1", action="permission", tool="run_command", allowed=True)
    log.record(session_id="s1", action="permission", tool="write_file", allowed=False, reason="outside root")
    entries = log.read()
    assert len(entries) == 2
    assert entries[0]["allowed"] is True
    assert entries[1]["reason"] == "outside root"
    # Validate JSON-lines parseability.
    for line in (tmp_path / "audit.log").read_text().splitlines():
        json.loads(line)

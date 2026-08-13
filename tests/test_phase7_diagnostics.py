"""Phase 7 — environment diagnostics tests.

The diagnostic functions list/identify stale KINETIC-managed containers but
never destroy automatically. These tests use fakes where Docker is unavailable.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from kinetic.environment.diagnostics import (
    ContainerInfo,
    find_stale_containers,
    list_managed_containers,
)
from kinetic.tools.terminal import CommandResult


def _fake_result(stdout: str = "", exit_code: int = 0) -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr="", duration_ms=0)


class TestListManagedContainers:
    async def test_parses_json_output(self) -> None:
        containers_json = json.dumps({
            "ID": "abc123",
            "Names": "kinetic-env",
            "Image": "python:3.11-slim",
            "Status": "Up 5 minutes",
            "Labels": "kinetic.managed=true,kinetic.session_id=sess-1",
            "CreatedAt": "2024-01-01",
        })
        with patch("kinetic.environment.diagnostics.run_command") as mock_run:
            mock_run.return_value = _fake_result(stdout=containers_json)
            containers = await list_managed_containers()
        assert len(containers) == 1
        c = containers[0]
        assert c.container_id == "abc123"
        assert c.is_managed
        assert c.session_id == "sess-1"
        assert c.image == "python:3.11-slim"

    async def test_docker_unavailable_returns_empty(self) -> None:
        with patch("kinetic.environment.diagnostics.run_command") as mock_run:
            mock_run.return_value = _fake_result(exit_code=1)
            containers = await list_managed_containers()
        assert containers == []

    async def test_multiple_containers(self) -> None:
        lines = [
            json.dumps({"ID": "a", "Names": "n1", "Image": "img", "Status": "Up",
                        "Labels": "kinetic.managed=true"}),
            json.dumps({"ID": "b", "Names": "n2", "Image": "img", "Status": "Exited",
                        "Labels": "kinetic.managed=true,kinetic.session_id=s2"}),
        ]
        with patch("kinetic.environment.diagnostics.run_command") as mock_run:
            mock_run.return_value = _fake_result(stdout="\n".join(lines))
            containers = await list_managed_containers()
        assert len(containers) == 2

    async def test_invalid_json_skipped(self) -> None:
        with patch("kinetic.environment.diagnostics.run_command") as mock_run:
            mock_run.return_value = _fake_result(stdout="{bad json\n")
            containers = await list_managed_containers()
        assert containers == []


class TestFindStale:
    async def test_exited_containers_are_stale(self) -> None:
        lines = [
            json.dumps({"ID": "a", "Names": "n1", "Image": "img", "Status": "Exited (0)",
                        "Labels": "kinetic.managed=true"}),
            json.dumps({"ID": "b", "Names": "n2", "Image": "img", "Status": "Up 5 min",
                        "Labels": "kinetic.managed=true"}),
        ]
        with patch("kinetic.environment.diagnostics.run_command") as mock_run:
            mock_run.return_value = _fake_result(stdout="\n".join(lines))
            stale = await find_stale_containers()
        assert len(stale) == 1
        assert stale[0].container_id == "a"

    async def test_no_docker_returns_empty(self) -> None:
        with patch("kinetic.environment.diagnostics.run_command") as mock_run:
            mock_run.return_value = _fake_result(exit_code=1)
            stale = await find_stale_containers()
        assert stale == []


class TestContainerInfo:
    def test_is_managed_property(self) -> None:
        c = ContainerInfo(
            container_id="x", name="n", image="i", status="Up",
            labels={"kinetic.managed": "true", "kinetic.session_id": "s1"},
        )
        assert c.is_managed
        assert c.session_id == "s1"

    def test_not_managed(self) -> None:
        c = ContainerInfo(
            container_id="x", name="n", image="i", status="Up", labels={},
        )
        assert not c.is_managed
        assert c.session_id is None

# KINETIC

KINETIC is an autonomous coding agent built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

It understands a software project, reads and modifies files, executes terminal commands, runs tests/builds, uses Git, installs dependencies, and streams its activity to a web UI - all inside a controlled environment with explicit security and permission boundaries.

## Status

Phase 1 (core scaffolding) is implemented: SDK adapter, agent session, event bus, tool registry, terminal + filesystem tools, permission policy, audit logging, configuration, and a minimal CLI.

The architecture is deliberately layered so that a future "AI Company" layer (CEO agent, departments, worker pools, etc.) can sit *above* the coding-agent core without rewriting it.

## Layout

    src/kinetic/
        agent/        Claude Agent SDK adapter + agent sessions
        tools/        tool registry + terminal / filesystem / (git, browser, ...)
        security/     tool permissions, audit logging
        events/       internal event bus + serializable event types
        config/       layered configuration
        cli/          minimal CLI entrypoint

## Install (dev)

    uv sync --all-extras

## CLI

    kinetic run "inspect this repo and list its files"

## Tests

    uv run pytest

"""Shared fixtures.

The module under test reads its entire configuration from environment
variables at import time -- Config is a frozen dataclass whose field defaults
are evaluated when the class is created, so constructing another Config() later
will not pick up changed env vars. Tests that need a different configuration
must therefore reimport the module under a patched environment, which is what
`server_with` does.

Nothing here touches a cluster: subprocess.run is replaced wherever a test
would otherwise shell out.
"""

from __future__ import annotations

import importlib
import sys

import pytest

# Env vars the server reads. Cleared before every import so a developer's own
# shell configuration cannot change what the tests exercise.
_SERVER_ENV = [
    "MICROK8S_MCP_MODE",
    "MICROK8S_MCP_KUBECTL",
    "MICROK8S_MCP_KUBECONFIG",
    "MICROK8S_MCP_CONTEXT",
    "MICROK8S_MCP_SSH_HOST",
    "MICROK8S_MCP_SSH_KEY",
    "MICROK8S_MCP_SSH",
    "MICROK8S_MCP_MICROK8S",
    "MICROK8S_MCP_TIMEOUT",
    "MICROK8S_MCP_MAX_OUTPUT",
    "MICROK8S_MCP_NAMESPACES",
    "MICROK8S_MCP_PROTECTED_NAMESPACES",
    "MICROK8S_MCP_ALLOW_PROTECTED_WRITES",
    "MICROK8S_MCP_ALLOW_ADDON_CHANGES",
    "MICROK8S_MCP_ALLOW_NODE_OPS",
]


@pytest.fixture
def server_with(monkeypatch):
    """Import microk8s_mcp.server with a specific environment.

    Usage:
        srv = server_with(MICROK8S_MCP_MODE="read-write")
    """

    def _load(**env):
        for name in _SERVER_ENV:
            monkeypatch.delenv(name, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        sys.modules.pop("microk8s_mcp.server", None)
        module = importlib.import_module("microk8s_mcp.server")
        return importlib.reload(module)

    yield _load

    # Leave no reconfigured module behind for the next test to import.
    sys.modules.pop("microk8s_mcp.server", None)


@pytest.fixture
def server(server_with):
    """Default configuration: read-only, no SSH host, no kubeconfig."""
    return server_with()


@pytest.fixture
def captured_argv(monkeypatch):
    """Capture argv passed to subprocess.run instead of executing it.

    Returns a list that fills with each argv the code under test tried to run.
    """
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Completed()

    monkeypatch.setattr("subprocess.run", _fake_run)
    return calls

"""Backend selection.

This is where a bug escalates privileges rather than merely failing: if the
local kubectl is not used, calls fall back to `microk8s kubectl` on the node,
which runs under MicroK8s' own admin credential and is not limited by the
scoped kubeconfig or the bundled RBAC role. A configured kubeconfig must never
be taken as evidence that a kubectl binary exists.
"""

from __future__ import annotations

import pytest


class TestKubectlBackendSelection:
    def test_uses_local_kubectl_when_present(
        self, server_with, captured_argv, monkeypatch
    ):
        srv = server_with(
            MICROK8S_MCP_KUBECONFIG="/tmp/scoped.kubeconfig",
            MICROK8S_MCP_SSH_HOST="node.example",
        )
        monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/kubectl")
        srv.kubectl(["get", "nodes"])

        argv = captured_argv[0]
        assert argv[0] == "kubectl"
        assert "--kubeconfig" in argv
        assert "/tmp/scoped.kubeconfig" in argv
        assert "ssh" not in argv[0]

    def test_falls_back_to_node_when_kubectl_missing(
        self, server_with, captured_argv, monkeypatch
    ):
        srv = server_with(
            MICROK8S_MCP_KUBECONFIG="/tmp/scoped.kubeconfig",
            MICROK8S_MCP_SSH_HOST="node.example",
        )
        monkeypatch.setattr(srv.shutil, "which", lambda _: None)
        srv.kubectl(["get", "nodes"])

        argv = captured_argv[0]
        assert argv[0] == "ssh", "a configured kubeconfig must not force the local path"
        assert any("microk8s kubectl" in part for part in argv)

    def test_kubeconfig_alone_does_not_select_local_kubectl(
        self, server_with, captured_argv, monkeypatch
    ):
        """Regression: the check was `kubeconfig or which(bin)`.

        With a kubeconfig set and no binary, that ran a nonexistent executable
        and made the documented fallback unreachable.
        """
        srv = server_with(
            MICROK8S_MCP_KUBECONFIG="/tmp/scoped.kubeconfig",
            MICROK8S_MCP_KUBECTL="/nonexistent/kubectl",
            MICROK8S_MCP_SSH_HOST="node.example",
        )
        monkeypatch.setattr(srv.shutil, "which", lambda _: None)
        srv.kubectl(["get", "nodes"])
        assert captured_argv[0][0] == "ssh"

    def test_fallback_warns_that_scoping_is_lost(
        self, server_with, captured_argv, monkeypatch, caplog
    ):
        srv = server_with(
            MICROK8S_MCP_KUBECONFIG="/tmp/scoped.kubeconfig",
            MICROK8S_MCP_SSH_HOST="node.example",
        )
        monkeypatch.setattr(srv.shutil, "which", lambda _: None)
        with caplog.at_level("WARNING"):
            srv.kubectl(["get", "nodes"])
        assert any(
            "falling back" in r.message or "falling back" in r.getMessage()
            for r in caplog.records
        ), "a silent privilege change is the failure mode being guarded against"

    def test_context_flag_passed_through(self, server_with, captured_argv, monkeypatch):
        srv = server_with(MICROK8S_MCP_CONTEXT="prod")
        monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/kubectl")
        srv.kubectl(["get", "nodes"])
        argv = captured_argv[0]
        assert "--context" in argv and "prod" in argv


class TestNodeBackend:
    def test_ssh_used_when_host_configured(self, server_with, captured_argv):
        srv = server_with(MICROK8S_MCP_SSH_HOST="user@node.example")
        srv.node_exec(["status"])
        argv = captured_argv[0]
        assert argv[0] == "ssh"
        assert "user@node.example" in argv
        assert "-o" in argv and "BatchMode=yes" in argv

    def test_ssh_key_included_when_set(self, server_with, captured_argv):
        srv = server_with(
            MICROK8S_MCP_SSH_HOST="user@node.example",
            MICROK8S_MCP_SSH_KEY="/home/u/.ssh/id_ed25519",
        )
        srv.node_exec(["status"])
        argv = captured_argv[0]
        assert "-i" in argv and "/home/u/.ssh/id_ed25519" in argv

    def test_remote_command_is_quoted_per_argument(self, server_with, captured_argv):
        srv = server_with(MICROK8S_MCP_SSH_HOST="node.example")
        srv.node_exec(["kubectl", "get", "pods -n evil; rm -rf /"])
        remote = captured_argv[0][-1]
        # The dangerous argument must survive as one quoted token, not as shell
        # syntax the remote shell would act on.
        assert "'pods -n evil; rm -rf /'" in remote

    def test_no_backend_raises_actionable_error(self, server_with, monkeypatch):
        srv = server_with()  # no SSH host
        monkeypatch.setattr(srv.shutil, "which", lambda _: None)
        monkeypatch.setattr(srv.os.path, "exists", lambda _: False)
        with pytest.raises(srv.ToolError, match="MICROK8S_MCP_SSH_HOST"):
            srv.node_exec(["status"])


class TestRunErrors:
    def test_missing_binary_becomes_toolerror(self, server, monkeypatch):
        def _boom(*a, **k):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr("subprocess.run", _boom)
        with pytest.raises(server.ToolError, match="Command not found"):
            server._run(["definitely-not-a-binary"])

    def test_nonzero_exit_becomes_toolerror_with_detail(self, server, monkeypatch):
        class _Failed:
            returncode = 1
            stdout = ""
            stderr = "boom from the server"

        monkeypatch.setattr("subprocess.run", lambda *a, **k: _Failed())
        with pytest.raises(server.ToolError, match="boom from the server"):
            server._run(["kubectl", "get", "nodes"])

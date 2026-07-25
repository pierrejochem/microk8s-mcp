"""Write-mode gating.

Read-only is the advertised default, and `delete_resource` is the one tool that
can destroy something in a single call. Both behaviours are asserted here
because a regression in either is silent: the tool still returns a string, and
the model has no way to tell a refusal from a success unless the text says so.
"""

from __future__ import annotations

import pytest


class TestReadOnlyMode:
    def test_read_only_is_the_default(self, server):
        assert server.WRITES_ENABLED is False

    @pytest.mark.parametrize("mode", ["read-write", "rw", "write"])
    def test_write_aliases(self, server_with, mode):
        assert server_with(MICROK8S_MCP_MODE=mode).WRITES_ENABLED is True

    @pytest.mark.parametrize("mode", ["read-only", "readonly", "ro", "", "nonsense"])
    def test_anything_else_stays_read_only(self, server_with, mode):
        assert server_with(MICROK8S_MCP_MODE=mode).WRITES_ENABLED is False

    def test_mode_is_case_insensitive(self, server_with):
        assert server_with(MICROK8S_MCP_MODE="READ-WRITE").WRITES_ENABLED is True

    def test_require_write_raises_when_read_only(self, server):
        with pytest.raises(server.ToolError, match="read-only mode"):
            server._require_write("scale a workload")

    def test_require_write_passes_when_enabled(self, server_with):
        srv = server_with(MICROK8S_MCP_MODE="read-write")
        srv._require_write("scale a workload")  # must not raise


class TestGuardDecorator:
    """_guard turns ToolError into text so the model sees a message, not a trace."""

    def test_converts_toolerror_to_text(self, server):
        @server._guard
        def boom():
            raise server.ToolError("nope")

        assert boom() == "ERROR: nope"

    def test_lets_other_exceptions_through(self, server):
        @server._guard
        def boom():
            raise ValueError("programming error")

        with pytest.raises(ValueError):
            boom()


class TestMutatingToolsRefuseInReadOnly:
    """Every mutating tool must refuse before it builds a command."""

    def test_delete_refuses(self, server, captured_argv):
        out = server.delete_resource(
            kind="configmap", name="x", namespace="apps", confirm=True
        )
        assert out.startswith("ERROR:")
        assert "read-only" in out
        assert captured_argv == [], "must refuse before shelling out"

    def test_scale_refuses(self, server, captured_argv):
        out = server.scale_workload(
            kind="deployment", name="web", namespace="apps", replicas=3
        )
        assert out.startswith("ERROR:")
        assert captured_argv == []

    def test_apply_refuses(self, server, captured_argv):
        out = server.apply_manifest(manifest_yaml="kind: ConfigMap\n")
        assert out.startswith("ERROR:")
        assert captured_argv == []


class TestDeleteConfirmation:
    def test_delete_requires_confirm(self, server_with, captured_argv):
        srv = server_with(MICROK8S_MCP_MODE="read-write")
        out = srv.delete_resource(kind="configmap", name="x", namespace="apps")
        assert out.startswith("ERROR:")
        assert "confirm=True" in out
        assert captured_argv == [], "must not delete before confirmation"

    def test_delete_proceeds_with_confirm(self, server_with, captured_argv):
        srv = server_with(
            MICROK8S_MCP_MODE="read-write", MICROK8S_MCP_KUBECTL="/bin/true"
        )
        srv.delete_resource(
            kind="configmap", name="doomed", namespace="apps", confirm=True
        )
        assert len(captured_argv) == 1
        argv = captured_argv[0]
        assert "delete" in argv and "configmap" in argv and "doomed" in argv

    def test_delete_rejects_injected_name_even_when_confirmed(
        self, server_with, captured_argv
    ):
        srv = server_with(MICROK8S_MCP_MODE="read-write")
        out = srv.delete_resource(
            kind="configmap", name="--all", namespace="apps", confirm=True
        )
        assert out.startswith("ERROR:")
        assert captured_argv == [], "--all must never reach kubectl"

    def test_delete_blocked_in_protected_namespace(self, server_with, captured_argv):
        srv = server_with(MICROK8S_MCP_MODE="read-write")
        out = srv.delete_resource(
            kind="configmap", name="x", namespace="kube-system", confirm=True
        )
        assert out.startswith("ERROR:")
        assert "protected" in out
        assert captured_argv == []


class TestCapabilitySwitches:
    def test_addon_changes_off_by_default(self, server_with):
        srv = server_with(MICROK8S_MCP_MODE="read-write")
        assert srv.CFG.allow_addon_changes is False

    def test_node_ops_off_by_default(self, server_with):
        srv = server_with(MICROK8S_MCP_MODE="read-write")
        assert srv.CFG.allow_node_ops is False

    def test_protected_writes_off_by_default(self, server_with):
        srv = server_with(MICROK8S_MCP_MODE="read-write")
        assert srv.CFG.allow_protected_writes is False

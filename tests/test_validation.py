"""Input validation: the layer that stops model output becoming kubectl flags.

The threat is not shell injection -- commands are built as argv lists -- but
argument injection: a "name" of `--all` or `-n=kube-system` would be read by
kubectl as a flag rather than as a value.
"""

from __future__ import annotations

import pytest


class TestTok:
    """_tok guards kinds, names and anything else spliced into argv."""

    @pytest.mark.parametrize(
        "value",
        [
            "nginx",
            "my-deployment",
            "deployment/nginx",
            "app.kubernetes.io",
            "web_1",
            "a",
            "0abc",
        ],
    )
    def test_accepts_ordinary_identifiers(self, server, value):
        assert server._tok(value, "name") == value

    @pytest.mark.parametrize(
        "value",
        [
            "--all",  # would delete everything
            "-n",  # would rebind the namespace
            "--kubeconfig=/etc/passwd",
            "-o=json",
            "",
            "   ",
            "name with space",
            "name;rm -rf /",
            "name$(id)",
            "name`id`",
            "name|tee",
            "name&",
            "../../escape",  # leading dot fails the first-char class
            ".hidden",
        ],
    )
    def test_rejects_flags_and_metacharacters(self, server, value):
        with pytest.raises(server.ToolError):
            server._tok(value, "name")

    def test_strips_surrounding_whitespace(self, server):
        assert server._tok("  nginx  ", "name") == "nginx"

    def test_error_names_the_field(self, server):
        with pytest.raises(server.ToolError, match="Invalid kind"):
            server._tok("--all", "kind")


class TestNamespaceValidation:
    """_ns enforces the RFC-1123 shape, the allowlist, and protected writes."""

    def test_empty_means_unset_not_invalid(self, server):
        assert server._ns(None) is None
        assert server._ns("") is None

    @pytest.mark.parametrize("value", ["default", "kube-system", "a", "a" * 63])
    def test_accepts_valid_names(self, server, value):
        assert server._ns(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "Default",  # uppercase
            "-leading",
            "under_score",
            "a" * 64,  # one over the limit
            "ns --all",
            "--all",
        ],
    )
    def test_rejects_invalid_names(self, server, value):
        with pytest.raises(server.ToolError):
            server._ns(value)

    def test_allowlist_blocks_other_namespaces(self, server_with):
        srv = server_with(MICROK8S_MCP_NAMESPACES="apps,staging")
        assert srv._ns("apps") == "apps"
        with pytest.raises(srv.ToolError, match="not in the allowlist"):
            srv._ns("production")

    def test_star_allows_everything(self, server_with):
        srv = server_with(MICROK8S_MCP_NAMESPACES="*")
        assert srv._ns("anything") == "anything"

    def test_protected_namespaces_are_readable(self, server):
        # Reads are permitted; only writes are gated.
        assert server._ns("kube-system") == "kube-system"

    def test_protected_namespaces_reject_writes(self, server):
        with pytest.raises(server.ToolError, match="protected"):
            server._ns("kube-system", write=True)

    def test_allow_protected_writes_opens_it(self, server_with):
        srv = server_with(MICROK8S_MCP_ALLOW_PROTECTED_WRITES="true")
        assert srv._ns("kube-system", write=True) == "kube-system"

    def test_default_is_protected(self, server):
        # `default` ships in the protected list, which surprises people.
        with pytest.raises(server.ToolError, match="protected"):
            server._ns("default", write=True)

    def test_allowlist_checked_before_protection(self, server_with):
        srv = server_with(
            MICROK8S_MCP_NAMESPACES="apps",
            MICROK8S_MCP_ALLOW_PROTECTED_WRITES="true",
        )
        with pytest.raises(srv.ToolError, match="not in the allowlist"):
            srv._ns("kube-system", write=True)


class TestEnvParsing:
    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on", " true "])
    def test_bool_truthy(self, server_with, truthy):
        srv = server_with(MICROK8S_MCP_ALLOW_NODE_OPS=truthy)
        assert srv.CFG.allow_node_ops is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "", "maybe"])
    def test_bool_falsy(self, server_with, falsy):
        srv = server_with(MICROK8S_MCP_ALLOW_NODE_OPS=falsy)
        assert srv.CFG.allow_node_ops is False

    def test_list_strips_and_drops_blanks(self, server_with):
        srv = server_with(MICROK8S_MCP_NAMESPACES=" apps , , staging ")
        assert srv.CFG.allowed_namespaces == ("apps", "staging")


class TestClip:
    def test_short_output_untouched(self, server):
        assert server._clip("hello") == "hello"

    def test_long_output_is_clipped_and_says_so(self, server_with):
        srv = server_with(MICROK8S_MCP_MAX_OUTPUT="100")
        clipped = srv._clip("x" * 5000)
        assert "characters omitted" in clipped
        assert len(clipped) < 5000

    def test_clip_keeps_both_ends(self, server_with):
        srv = server_with(MICROK8S_MCP_MAX_OUTPUT="100")
        clipped = srv._clip("A" * 500 + "Z" * 500)
        assert clipped.startswith("A")
        assert clipped.endswith("Z")

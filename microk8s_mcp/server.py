"""
MicroK8s MCP server.

Exposes a MicroK8s cluster to an MCP client (e.g. the `claude` CLI) as a set of
typed tools. Read-only by default; mutating tools must be explicitly unlocked.

Two execution backends:

  * kubectl backend  - talks to the cluster API server using a kubeconfig.
                       Used for every kubectl-level tool.
  * node backend     - runs `microk8s ...` on the node itself, either locally
                       (server runs on the node) or over SSH. Used for addon
                       management and snap-level status.

Configuration is entirely via environment variables; see README.md.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# --------------------------------------------------------------------------
# Logging (stderr only -- stdout is the MCP transport)
# --------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("MICROK8S_MCP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s microk8s-mcp %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("microk8s-mcp")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    mode: str = os.environ.get("MICROK8S_MCP_MODE", "read-only").strip().lower()
    kubectl_bin: str = os.environ.get("MICROK8S_MCP_KUBECTL", "kubectl")
    kubeconfig: str = os.environ.get("MICROK8S_MCP_KUBECONFIG", "")
    context: str = os.environ.get("MICROK8S_MCP_CONTEXT", "")
    ssh_host: str = os.environ.get("MICROK8S_MCP_SSH_HOST", "")
    ssh_key: str = os.environ.get("MICROK8S_MCP_SSH_KEY", "")
    ssh_bin: str = os.environ.get("MICROK8S_MCP_SSH", "ssh")
    microk8s_bin: str = os.environ.get("MICROK8S_MCP_MICROK8S", "microk8s")
    timeout: int = int(os.environ.get("MICROK8S_MCP_TIMEOUT", "60"))
    max_output: int = int(os.environ.get("MICROK8S_MCP_MAX_OUTPUT", "24000"))
    # Namespace policy
    allowed_namespaces: tuple[str, ...] = tuple(
        _env_list("MICROK8S_MCP_NAMESPACES", "*")
    )
    protected_namespaces: tuple[str, ...] = tuple(
        _env_list(
            "MICROK8S_MCP_PROTECTED_NAMESPACES",
            "kube-system,kube-public,kube-node-lease,default",
        )
    )
    allow_protected_writes: bool = _env_bool("MICROK8S_MCP_ALLOW_PROTECTED_WRITES")
    allow_addon_changes: bool = _env_bool("MICROK8S_MCP_ALLOW_ADDON_CHANGES")
    allow_node_ops: bool = _env_bool("MICROK8S_MCP_ALLOW_NODE_OPS")


CFG = Config()
WRITES_ENABLED = CFG.mode in {"read-write", "rw", "write"}


class ToolError(Exception):
    """Raised for policy or validation failures; surfaced to the model as text."""


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

_SAFE_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
_SAFE_NS = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _tok(value: str, what: str) -> str:
    """Reject anything that could be read by kubectl as a flag or shell metachar."""
    value = value.strip()
    if not value or not _SAFE_TOKEN.match(value):
        raise ToolError(f"Invalid {what}: {value!r}")
    return value


def _ns(namespace: str | None, *, write: bool = False) -> str | None:
    if namespace is None or namespace == "":
        return None
    namespace = namespace.strip()
    if not _SAFE_NS.match(namespace):
        raise ToolError(f"Invalid namespace: {namespace!r}")
    if "*" not in CFG.allowed_namespaces and namespace not in CFG.allowed_namespaces:
        raise ToolError(
            f"Namespace {namespace!r} is not in the allowlist "
            f"(MICROK8S_MCP_NAMESPACES={','.join(CFG.allowed_namespaces)})."
        )
    if write and namespace in CFG.protected_namespaces and not CFG.allow_protected_writes:
        raise ToolError(
            f"Namespace {namespace!r} is protected. Set "
            f"MICROK8S_MCP_ALLOW_PROTECTED_WRITES=true to permit writes there."
        )
    return namespace


def _require_write(action: str) -> None:
    if not WRITES_ENABLED:
        raise ToolError(
            f"Refusing to {action}: server is in read-only mode. "
            f"Restart it with MICROK8S_MCP_MODE=read-write to enable mutating tools."
        )


def _clip(text: str) -> str:
    if len(text) <= CFG.max_output:
        return text
    keep = CFG.max_output // 2
    omitted = len(text) - 2 * keep
    return (
        text[:keep]
        + f"\n\n... [{omitted} characters omitted -- narrow the query, "
        f"use a label selector, or reduce tail_lines] ...\n\n"
        + text[-keep:]
    )


# --------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------


def _run(argv: list[str], stdin: str | None = None, timeout: int | None = None) -> str:
    timeout = timeout or CFG.timeout
    log.info("exec: %s", " ".join(shlex.quote(a) for a in argv))
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Command not found: {argv[0]} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        detail = f"{argv[0]} {argv[1]}" if len(argv) > 1 else argv[0]
        raise ToolError(f"Command timed out after {timeout}s: {detail}") from exc

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = err or out or "(no output)"
        raise ToolError(f"Command failed (exit {proc.returncode}):\n{detail}")
    if err and not out:
        return _clip(err)
    if err:
        return _clip(f"{out}\n\n[stderr]\n{err}")
    return _clip(out) if out else "(no output)"


def _ssh_prefix() -> list[str]:
    argv = [CFG.ssh_bin, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if CFG.ssh_key:
        argv += ["-i", CFG.ssh_key]
    argv.append(CFG.ssh_host)
    return argv


def kubectl(args: list[str], stdin: str | None = None, timeout: int | None = None) -> str:
    """Run kubectl against the cluster.

    A local kubectl is the only path that honours the scoped kubeconfig, so it
    wins whenever the binary exists. Presence of a kubeconfig is not evidence
    that it does - test the binary itself, or a configured kubeconfig sends
    every call to an executable that is not there.

    The node fallback runs `microk8s kubectl` under MicroK8s' own admin
    credential, which is broader than the scoped identity and is not limited by
    the bundled RBAC role. Install kubectl locally if that distinction matters.
    """
    if shutil.which(CFG.kubectl_bin):
        argv = [CFG.kubectl_bin]
        if CFG.kubeconfig:
            argv += ["--kubeconfig", CFG.kubeconfig]
        if CFG.context:
            argv += ["--context", CFG.context]
        return _run(argv + args, stdin=stdin, timeout=timeout)

    # No local kubectl: fall back to `microk8s kubectl` on the node.
    if CFG.kubeconfig:
        log.warning(
            "kubectl (%s) not found on PATH; falling back to `microk8s kubectl` on the "
            "node. MICROK8S_MCP_KUBECONFIG cannot be honoured on that path, so commands "
            "run with the node's admin credential rather than the scoped identity.",
            CFG.kubectl_bin,
        )
    return node_exec(["kubectl", *args], stdin=stdin, timeout=timeout)


def node_exec(
    args: list[str], stdin: str | None = None, timeout: int | None = None
) -> str:
    """Run `microk8s <args>` on the node (locally or over SSH)."""
    if CFG.ssh_host:
        remote = " ".join(shlex.quote(a) for a in [CFG.microk8s_bin, *args])
        return _run(_ssh_prefix() + [remote], stdin=stdin, timeout=timeout)
    if shutil.which(CFG.microk8s_bin) or os.path.exists(CFG.microk8s_bin):
        return _run([CFG.microk8s_bin, *args], stdin=stdin, timeout=timeout)
    raise ToolError(
        "No node backend available. This tool needs to run `microk8s` on the node: "
        "either run this MCP server on the MicroK8s host, or set "
        "MICROK8S_MCP_SSH_HOST=user@host (with key-based auth)."
    )


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

mcp = FastMCP(
    "microk8s",
    instructions=(
        "Manage a MicroK8s cluster. Prefer cluster_overview() to orient yourself "
        "before drilling into namespaces. Always inspect resources before mutating "
        "them, and run apply_manifest with dry_run=True first. Mutating tools fail "
        "unless the server was started in read-write mode."
    ),
)

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, openWorldHint=False
)


def _guard(fn):
    """Convert ToolError into a clean message instead of a stack trace."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return f"ERROR: {exc}"

    return wrapper


# ------------------------------ read tools --------------------------------


@mcp.tool(annotations=READ)
@_guard
def cluster_overview() -> str:
    """Snapshot of cluster health: nodes, MicroK8s addons, namespaces,
    workloads that are not Running/Completed, and recent warning events.
    Start here when asked an open-ended question about the cluster."""
    sections: list[str] = []

    def section(title: str, fn) -> None:
        try:
            sections.append(f"### {title}\n{fn()}")
        except ToolError as exc:
            sections.append(f"### {title}\n(unavailable: {exc})")

    section("Nodes", lambda: kubectl(["get", "nodes", "-o", "wide"]))
    section("MicroK8s status", lambda: node_exec(["status"]))
    section("Namespaces", lambda: kubectl(["get", "namespaces"]))
    section(
        "Pods not Running/Succeeded",
        lambda: kubectl(
            [
                "get", "pods", "--all-namespaces",
                "--field-selector=status.phase!=Running,status.phase!=Succeeded",
                "-o", "wide",
            ]
        ),
    )
    section(
        "Recent warning events",
        lambda: kubectl(
            [
                "get", "events", "--all-namespaces",
                "--field-selector=type=Warning",
                "--sort-by=.lastTimestamp",
            ]
        ),
    )
    return _clip("\n\n".join(sections))


@mcp.tool(annotations=READ)
@_guard
def list_resources(
    kind: str,
    namespace: str = "",
    all_namespaces: bool = False,
    selector: str = "",
    field_selector: str = "",
    output: Literal["wide", "name", "json", "yaml"] = "wide",
) -> str:
    """List Kubernetes resources of a given kind (pods, deployments, svc,
    ingress, pvc, nodes, crds, ...). Use `selector` for label selectors like
    'app=web'. Prefer output='wide' unless you need full specs."""
    args = ["get", _tok(kind, "kind")]
    if all_namespaces:
        args.append("--all-namespaces")
    else:
        ns = _ns(namespace)
        if ns:
            args += ["-n", ns]
    if selector:
        args += ["-l", selector]
    if field_selector:
        args += [f"--field-selector={field_selector}"]
    args += ["-o", output]
    return kubectl(args)


@mcp.tool(annotations=READ)
@_guard
def get_resource(
    kind: str,
    name: str,
    namespace: str = "",
    output: Literal["yaml", "json"] = "yaml",
) -> str:
    """Fetch the full manifest of a single resource."""
    args = ["get", _tok(kind, "kind"), _tok(name, "name")]
    ns = _ns(namespace)
    if ns:
        args += ["-n", ns]
    args += ["-o", output]
    return kubectl(args)


@mcp.tool(annotations=READ)
@_guard
def describe_resource(kind: str, name: str, namespace: str = "") -> str:
    """`kubectl describe` a resource: status, conditions, and its recent events.
    Best first step when something is failing."""
    args = ["describe", _tok(kind, "kind"), _tok(name, "name")]
    ns = _ns(namespace)
    if ns:
        args += ["-n", ns]
    return kubectl(args)


@mcp.tool(annotations=READ)
@_guard
def get_logs(
    pod: str,
    namespace: str,
    container: str = "",
    tail_lines: int = 200,
    since: str = "",
    previous: bool = False,
) -> str:
    """Container logs for a pod. `since` accepts durations like '15m' or '2h'.
    Set previous=True to read the logs of a crashed previous container."""
    args = ["logs", _tok(pod, "pod name"), "-n", _ns(namespace) or "default"]
    if container:
        args += ["-c", _tok(container, "container name")]
    args += ["--tail", str(max(1, min(int(tail_lines), 5000)))]
    if since:
        if not re.match(r"^\d+[smhd]$", since.strip()):
            raise ToolError("`since` must look like 30s, 15m, 2h or 1d")
        args += [f"--since={since.strip()}"]
    if previous:
        args.append("--previous")
    return kubectl(args)


@mcp.tool(annotations=READ)
@_guard
def get_events(
    namespace: str = "",
    all_namespaces: bool = False,
    only_warnings: bool = True,
) -> str:
    """Cluster events, newest last. Warnings only by default."""
    args = ["get", "events"]
    if all_namespaces:
        args.append("--all-namespaces")
    else:
        ns = _ns(namespace)
        if ns:
            args += ["-n", ns]
    if only_warnings:
        args.append("--field-selector=type=Warning")
    args.append("--sort-by=.lastTimestamp")
    return kubectl(args)


@mcp.tool(annotations=READ)
@_guard
def top(scope: Literal["nodes", "pods"] = "nodes", namespace: str = "") -> str:
    """CPU/memory usage for nodes or pods. Requires the metrics-server addon."""
    args = ["top", scope]
    if scope == "pods":
        ns = _ns(namespace)
        args += ["-n", ns] if ns else ["--all-namespaces"]
    return kubectl(args)


@mcp.tool(annotations=READ)
@_guard
def api_resources() -> str:
    """List every resource kind the cluster knows about, including CRDs.
    Use this when unsure what `kind` to pass to the other tools."""
    return kubectl(["api-resources", "--verbs=list", "-o", "wide"])


@mcp.tool(annotations=READ)
@_guard
def rollout_status(kind: str, name: str, namespace: str) -> str:
    """Current rollout state of a deployment, statefulset or daemonset."""
    return kubectl(
        [
            "rollout", "status", f"{_tok(kind, 'kind')}/{_tok(name, 'name')}",
            "-n", _ns(namespace) or "default", "--timeout=20s",
        ]
    )


@mcp.tool(annotations=READ)
@_guard
def microk8s_status() -> str:
    """MicroK8s snap status: whether the cluster is running, HA state, and which
    addons are enabled or disabled."""
    return node_exec(["status"])


# ------------------------------ write tools -------------------------------


@mcp.tool(annotations=WRITE)
@_guard
def apply_manifest(manifest_yaml: str, namespace: str = "", dry_run: bool = True) -> str:
    """Apply a YAML manifest (supports multi-document YAML). Defaults to a
    server-side dry run -- call once with dry_run=True, show the user the diff,
    then call again with dry_run=False to commit."""
    _require_write("apply a manifest")
    if not manifest_yaml.strip():
        raise ToolError("Empty manifest.")
    args = ["apply", "-f", "-"]
    ns = _ns(namespace, write=True)
    if ns:
        args += ["-n", ns]
    if dry_run:
        args += ["--dry-run=server"]
    result = kubectl(args, stdin=manifest_yaml)
    if dry_run:
        try:
            diff = kubectl(
                ["diff", "-f", "-"] + (["-n", ns] if ns else []), stdin=manifest_yaml
            )
        except ToolError as exc:
            # kubectl diff exits 1 when a diff exists; surface the body anyway.
            diff = str(exc)
        return f"DRY RUN (nothing changed)\n{result}\n\n### Diff\n{_clip(diff)}"
    return result


@mcp.tool(annotations=WRITE)
@_guard
def scale_workload(kind: str, name: str, namespace: str, replicas: int) -> str:
    """Scale a deployment, statefulset or replicaset to a replica count."""
    _require_write("scale a workload")
    if not 0 <= int(replicas) <= 100:
        raise ToolError("replicas must be between 0 and 100")
    return kubectl(
        [
            "scale", f"{_tok(kind, 'kind')}/{_tok(name, 'name')}",
            f"--replicas={int(replicas)}",
            "-n", _ns(namespace, write=True) or "default",
        ]
    )


@mcp.tool(annotations=WRITE)
@_guard
def rollout_restart(kind: str, name: str, namespace: str) -> str:
    """Trigger a rolling restart of a deployment, statefulset or daemonset."""
    _require_write("restart a workload")
    return kubectl(
        [
            "rollout", "restart", f"{_tok(kind, 'kind')}/{_tok(name, 'name')}",
            "-n", _ns(namespace, write=True) or "default",
        ]
    )


@mcp.tool(annotations=DESTRUCTIVE)
@_guard
def delete_resource(kind: str, name: str, namespace: str, confirm: bool = False) -> str:
    """Delete one named resource. Requires confirm=True. Bulk/selector deletes
    are deliberately not supported -- delete one object at a time."""
    _require_write("delete a resource")
    if not confirm:
        raise ToolError(
            "Deletion requires confirm=True. Show the user exactly what will be "
            "deleted and get their agreement first."
        )
    return kubectl(
        [
            "delete", _tok(kind, "kind"), _tok(name, "name"),
            "-n", _ns(namespace, write=True) or "default",
        ]
    )


@mcp.tool(annotations=WRITE)
@_guard
def manage_addon(
    action: Literal["enable", "disable"],
    addon: str,
    arguments: str = "",
) -> str:
    """Enable or disable a MicroK8s addon (dns, ingress, storage, metrics-server,
    cert-manager, registry, ...). `arguments` is passed after a colon, e.g.
    addon='registry', arguments='size=40Gi'."""
    _require_write(f"{action} an addon")
    if not CFG.allow_addon_changes:
        raise ToolError(
            "Addon changes are disabled. Set MICROK8S_MCP_ALLOW_ADDON_CHANGES=true "
            "to permit them."
        )
    spec = _tok(addon, "addon name")
    if arguments:
        if not re.match(r"^[a-zA-Z0-9=.,:/_\- ]+$", arguments):
            raise ToolError(f"Invalid addon arguments: {arguments!r}")
        spec = f"{spec}:{arguments}"
    return node_exec([action, spec], timeout=max(CFG.timeout, 300))


@mcp.tool(annotations=DESTRUCTIVE)
@_guard
def node_maintenance(
    action: Literal["cordon", "uncordon", "drain"],
    node: str,
    confirm: bool = False,
) -> str:
    """Cordon, uncordon or drain a node. Draining evicts workloads -- on a
    single-node MicroK8s install this takes the cluster's workloads down."""
    _require_write(f"{action} a node")
    if not CFG.allow_node_ops:
        raise ToolError(
            "Node operations are disabled. Set MICROK8S_MCP_ALLOW_NODE_OPS=true "
            "to permit them."
        )
    if action in {"drain", "cordon"} and not confirm:
        raise ToolError(f"{action} requires confirm=True after checking with the user.")
    args = [action, _tok(node, "node name")]
    if action == "drain":
        args += ["--ignore-daemonsets", "--delete-emptydir-data", "--timeout=120s"]
    return kubectl(args, timeout=max(CFG.timeout, 180))


# -------------------------------- prompts ---------------------------------


@mcp.prompt(title="Triage a namespace")
def triage_namespace(namespace: str) -> str:
    """Walk through diagnosing a broken namespace."""
    return (
        f"Triage namespace '{namespace}' on the MicroK8s cluster.\n"
        f"1. List pods, deployments, services and ingresses there.\n"
        f"2. For anything not Ready, describe it and pull recent logs "
        f"(including previous-container logs if it is crash-looping).\n"
        f"3. Check warning events for the namespace.\n"
        f"4. Report the likely root cause and propose a fix. Do not change "
        f"anything without asking me first."
    )


@mcp.prompt(title="Cluster health report")
def health_report() -> str:
    """Produce a short health report for the cluster."""
    return (
        "Give me a health report for the MicroK8s cluster: node conditions and "
        "capacity, enabled addons, workloads that are not healthy, PVCs that are "
        "not Bound, and anything in the warning events worth acting on. Keep it "
        "short and lead with anything that needs attention."
    )


# --------------------------------------------------------------------------


def main() -> None:
    log.info(
        "starting microk8s-mcp: mode=%s kubeconfig=%s node_backend=%s namespaces=%s",
        "read-write" if WRITES_ENABLED else "read-only",
        CFG.kubeconfig or "(default)",
        f"ssh:{CFG.ssh_host}" if CFG.ssh_host else "local",
        ",".join(CFG.allowed_namespaces),
    )
    mcp.run(transport=os.environ.get("MICROK8S_MCP_TRANSPORT", "stdio"))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()

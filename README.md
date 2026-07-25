![microk8s-mcp — Model Context Protocol server](media/microk8s-mcp-lockup-reverse.svg)

An MCP server that lets the `claude` CLI operate a MicroK8s cluster: inspect
workloads, read logs, triage failures, and — when you unlock it — scale, apply
manifests, restart rollouts, and manage addons.

The server is read-only by default, and every mutating capability sits behind a
separate switch.

> [!WARNING]
> **`rbac.yaml` in this repo grants cluster-wide write.** The
> `claude-mcp-operator` ClusterRoleBinding is active, so `kubectl apply -f
> rbac.yaml` gives the ServiceAccount `create/update/patch/delete` on workloads
> **in every namespace**, plus node cordon/drain and pod eviction. Secrets stay
> excluded, and the credential cannot escalate its own permissions.
>
> If you want read-only, comment out that binding *before* applying — and if
> you already applied it, `apply` will not remove the binding for you:
>
> ```bash
> kubectl delete clusterrolebinding claude-mcp-operator
> ```
>
> To narrow instead of removing, replace the ClusterRoleBinding with a
> `RoleBinding` per namespace. That is the only limit the API server enforces;
> `MICROK8S_MCP_NAMESPACES` is what this server is *willing* to do, not what the
> credential is *able* to do. Audit the real answer with:
>
> ```bash
> kubectl --kubeconfig ~/.kube/claude-mcp.kubeconfig auth can-i --list
> ```

---

## Topologies

The server has two backends and you can use either or both.

**kubectl backend** talks to the API server over the network using a
kubeconfig. This drives every `kubectl`-level tool.

**node backend** runs `microk8s ...` on the node itself, for addon management
and snap-level status. It runs locally if the server is on the node, otherwise
over SSH.

```
A)  workstation                                  microk8s node
    claude CLI ── stdio ── mcp server ── :16443 ── kube-apiserver
                                      └─ ssh ───── microk8s snap

B)  workstation                                  microk8s node
    claude CLI ── ssh ──────────────────────────── mcp server ── localhost
```

Topology **A** is the better default: the MCP server runs on your workstation,
holds a scoped kubeconfig, and only needs SSH for addon commands. If you skip
the SSH config, everything still works except `microk8s_status` and
`manage_addon`.

---

## Install

On whichever machine runs the server:

```bash
git clone <this-repo> microk8s-mcp && cd microk8s-mcp
make install
```

That is an *editable* install into `./.venv` — the venv links back to the
checkout, so edits are live and the source must stay put. For an install that
outlives the checkout, put the venv somewhere stable and copy the package in:

```bash
make install VENV=~/.local/share/microk8s-mcp/venv EDITABLE=0
```

`EDITABLE=0` copies rather than links, so the repo can later be moved or
deleted. The cost is that source edits need a reinstall; the target
force-reinstalls every run, because pip would otherwise treat the same version
as already satisfied and leave stale code in place while reporting success.
Pass the same `VENV=` to `check`, `register` and `register-rw`.

`kubectl` needs to be on PATH for the kubectl backend. If it isn't, the server
falls back to `microk8s kubectl` via the node backend — but note that fallback
runs under MicroK8s' own admin credential, so the scoped kubeconfig and the
bundled reader role stop applying. The server logs a warning when it happens.
Install kubectl locally if you want the RBAC scoping to mean anything;
`make register` then bakes its absolute path in as `MICROK8S_MCP_KUBECTL`.

`make help` lists every setup target; `make doctor` reports on prerequisites.
Step-by-step instructions, including both topologies, are in `install.txt`.

## Give it a scoped identity

Don't hand it your admin kubeconfig.

If the node is another machine — the usual case, and topology A — do it all
from your workstation over SSH:

```bash
make node-setup SSH_HOST=pierre@k8s.lan
```

That applies `rbac.yaml` on the node, mints the kubeconfig there, and writes it
to `~/.kube/claude-mcp.kubeconfig` mode 600 on this machine. Nothing is
installed on the node.

`APISERVER` defaults to `https://<node>:16443`, where the node is `SSH_HOST` put
through `ssh -G` — so an `~/.ssh/config` alias resolves to the address kubectl
will actually need, since kubectl doesn't read that file. Override it when the
node answers on a different address than you reach it at. The address is
validated before anything is minted: unresolvable is a hard stop, unreachable
is a warning. `make node-check SSH_HOST=...` runs those preconditions without
changing anything.

Sitting on the node already? The same two steps, locally:

```bash
make rbac
make kubeconfig APISERVER=https://k8s.lan:16443
```

Or without make:

```bash
microk8s kubectl apply -f rbac.yaml
APISERVER=https://k8s.lan:16443 bash make-kubeconfig.sh > claude-mcp.kubeconfig
```

In that case copy the file to the machine running the server and `chmod 600` it.

The bundled reader role grants get/list/watch across the cluster **except
Secrets** — deliberately, so cluster credentials can never end up in a model
context.

As noted at the top, the `claude-mcp-operator` binding is active in this repo,
so applying `rbac.yaml` unchanged grants cluster-wide write. Three ways to shape
that grant, in descending order of how much they actually protect you:

| Approach | Enforced by | Effect |
|---|---|---|
| Comment out the `claude-mcp-operator` binding | API server | No writes at all |
| Replace it with per-namespace `RoleBinding`s | API server | Writes only in the namespaces you name |
| `MICROK8S_MCP_NAMESPACES=apps,staging` | This server | Server declines elsewhere; credential could still do it |

Only the first two constrain the credential itself. The third is what the
server is *willing* to do — useful, but it is not a security boundary. Set both
layers.

## Register with the Claude CLI

`make register SSH_HOST=user@host` and `make register-rw SSH_HOST=user@host
NAMESPACES=apps,staging` wrap the two commands below with absolute paths filled
in — including `MICROK8S_MCP_KUBECTL`, which matters because Claude Code
launches the server with its own `PATH`.

`register-rw` takes four switches, and prints the resulting grant before it
acts:

| Variable | Default | Effect |
|---|---|---|
| `RW_SERVER_NAME` | `microk8s-rw` | Pass `microk8s` to replace the read-only entry instead of running both |
| `ALLOW_ADDON_CHANGES` | `true` | `manage_addon` may enable/disable addons — snap-level root on the node, outside RBAC entirely |
| `ALLOW_NODE_OPS` | `false` | `node_maintenance` may cordon/drain. On a single-node cluster a drain evicts everything |
| `ALLOW_PROTECTED_WRITES` | `false` | Permits writes to `kube-system`, `kube-public`, `kube-node-lease`, `default` |

```bash
make register-rw SSH_HOST=user@host NAMESPACES=apps,staging ALLOW_NODE_OPS=true
```

The long forms, if you'd rather be explicit:

Read-only, which is where you should start:

```bash
claude mcp add --transport stdio microk8s \
  --env MICROK8S_MCP_KUBECONFIG=/home/pierre/.kube/claude-mcp.kubeconfig \
  --env MICROK8S_MCP_SSH_HOST=pierre@k8s.lan \
  -- /home/pierre/microk8s-mcp/.venv/bin/microk8s-mcp
```

Full management, scoped to a couple of namespaces:

```bash
claude mcp add --transport stdio microk8s \
  --env MICROK8S_MCP_KUBECONFIG=/home/pierre/.kube/claude-mcp.kubeconfig \
  --env MICROK8S_MCP_SSH_HOST=pierre@k8s.lan \
  --env MICROK8S_MCP_MODE=read-write \
  --env MICROK8S_MCP_NAMESPACES=apps,staging \
  --env MICROK8S_MCP_ALLOW_ADDON_CHANGES=true \
  -- /home/pierre/microk8s-mcp/.venv/bin/microk8s-mcp
```

All flags go before the server name; `--` separates them from the command.
Use `--scope user` (or `make register SCOPE=user`) to make it available in every
project rather than just the current directory. Verify with `claude mcp list`,
then `/mcp` inside a session to see the tool inventory.

**If `claude mcp list` doesn't show it**, check these before assuming it's
broken:

- **Scope.** A local-scope entry only resolves when your working directory is
  inside that project. Run `claude mcp get microk8s` to see where it landed.
- **Restart.** Claude Code loads MCP servers at session start, so a server
  registered mid-session won't appear in `/mcp` until you restart — regardless
  of scope.

A user-scope entry pointing into a repo `.venv` is worth avoiding: deleting or
moving that checkout then breaks the server in every project at once. Pair
`SCOPE=user` with an out-of-repo `EDITABLE=0` install.

Two servers side by side works well: register a `microk8s` read-only entry
pointed at everything, and a `microk8s-rw` entry scoped to the namespaces you
actually deploy to.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MICROK8S_MCP_MODE` | `read-only` | Set `read-write` to unlock mutating tools |
| `MICROK8S_MCP_KUBECONFIG` | *(kubectl default)* | Path to the scoped kubeconfig |
| `MICROK8S_MCP_CONTEXT` | *(current)* | kubeconfig context to use |
| `MICROK8S_MCP_KUBECTL` | `kubectl` | Path to the kubectl binary |
| `MICROK8S_MCP_SSH_HOST` | *(unset)* | `user@host` for the node backend |
| `MICROK8S_MCP_SSH_KEY` | *(agent/default)* | Identity file for SSH |
| `MICROK8S_MCP_NAMESPACES` | `*` | Comma-separated namespace allowlist |
| `MICROK8S_MCP_PROTECTED_NAMESPACES` | `kube-system,kube-public,kube-node-lease,default` | Readable, but writes blocked |
| `MICROK8S_MCP_ALLOW_PROTECTED_WRITES` | `false` | Permit writes to protected namespaces |
| `MICROK8S_MCP_ALLOW_ADDON_CHANGES` | `false` | Permit enable/disable of addons |
| `MICROK8S_MCP_ALLOW_NODE_OPS` | `false` | Permit cordon/uncordon/drain |
| `MICROK8S_MCP_TIMEOUT` | `60` | Per-command timeout, seconds |
| `MICROK8S_MCP_MAX_OUTPUT` | `24000` | Output clipped past this many chars |
| `MICROK8S_MCP_LOG_LEVEL` | `INFO` | Every command is logged to stderr |

## Tools

**Read** — `cluster_overview`, `list_resources`, `get_resource`,
`describe_resource`, `get_logs`, `get_events`, `top`, `api_resources`,
`rollout_status`, `microk8s_status`

**Write** — `apply_manifest` (server-side dry run by default, returns a diff),
`scale_workload`, `rollout_restart`, `delete_resource` (single named object,
requires `confirm=True`), `manage_addon`, `node_maintenance`

**Prompts** — `triage_namespace`, `health_report`

---

## Security notes

A few things are omitted on purpose:

- **No `kubectl exec`, no port-forward, no raw shell.** An exec tool would be
  arbitrary code execution inside your cluster driven by model output. If you
  want it, add it yourself and understand what you're accepting.
- **No Secret reads** in the bundled RBAC role.
- **No bulk deletes.** `delete_resource` takes one kind and one name; there is
  no `--all` and no selector path.
- **No shell interpolation.** Commands are built as argv lists, never strings;
  kinds, names and namespaces are regex-validated so nothing can be smuggled in
  as a kubectl flag. Remote SSH commands are `shlex.quote`d per argument.

The layers are independent: RBAC is what the credential is *able* to do, and the
env vars are what this server is *willing* to do. Set both. If the token is
bound only to the reader role, `MICROK8S_MCP_MODE=read-write` still can't hurt
you — the API server refuses.

The asymmetry matters, and it only runs one way. A narrow RBAC binding makes a
permissive env var harmless. A narrow env var does **not** make a permissive
binding safe: `MICROK8S_MCP_NAMESPACES=apps` against a cluster-wide
ClusterRoleBinding still leaves a token that can write anywhere, and anything
else holding that file ignores this server's opinion entirely. Audit the layer
that is actually enforced:

```bash
kubectl --kubeconfig ~/.kube/claude-mcp.kubeconfig auth can-i --list
kubectl --kubeconfig ~/.kube/claude-mcp.kubeconfig auth can-i list secrets -A   # expect: no
```

One more path worth knowing: if the server can't find a local `kubectl`, it
falls back to `microk8s kubectl` on the node, which runs under that node's
**admin** credential — the scoped kubeconfig and both roles stop applying. It
logs a warning when this happens. `MICROK8S_MCP_KUBECTL` with an absolute path
(what `make register` sets) keeps you on the scoped path.

Every command executed is logged to stderr. Capture it if you want an audit
trail:

```bash
--env MICROK8S_MCP_LOG_LEVEL=INFO
```

## Troubleshooting

**Server shows `disconnected` in `claude mcp list`** — Claude Code launches
subprocesses with a different environment than your shell. Use absolute paths
to the venv binary, as in the examples above.

**`connection refused` on :16443** — MicroK8s only binds the API server to
addresses in its certificate SANs. If you're connecting by hostname or a
non-primary IP, add it to `/var/snap/microk8s/current/certs/csr.conf.template`
and run `microk8s refresh-certs --cert server.crt`.

**`microk8s: command not found` over SSH** — the snap bin directory isn't in a
non-interactive shell's PATH. Point at it directly:
`--env MICROK8S_MCP_MICROK8S=/snap/bin/microk8s`.

**`metrics-server` errors from `top`** — enable the addon:
`microk8s enable metrics-server`.

---

## Development

```bash
make dev     # install with the dev extras (pytest, ruff)
make ci      # lint + tests + dependency audit, exactly what CI runs
```

`make test` runs pytest from `/` on purpose: `python -c` and pytest both prepend
the working directory to `sys.path`, so running from the repo root imports the
local source and would pass even with nothing installed in the venv. The same
reasoning applies to `make check`, which also prints `resolved from:` so you can
see which copy of the code answered.

The suite covers the places where a bug is dangerous rather than merely wrong:
argument injection through `_tok`/`_ns` (a name of `--all` must never reach
kubectl), read-only mode refusing every mutating tool before it shells out,
`delete_resource` requiring `confirm=True`, and backend selection — a
mis-selected backend silently runs under the node's admin credential instead of
the scoped one.

### CI

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | push to `main`, PRs | ruff, pytest on Python 3.10–3.13, build + wheel smoke test, `pip-audit` |
| `release.yml` | pushing a `v*` tag | verifies tag matches `pyproject` version, re-runs lint and tests, builds, creates a GitHub Release with sdist + wheel |

Cut a release with `git tag v0.2.0 && git push origin v0.2.0`. The tag/version
check runs first, so a mismatched tag fails before anything is published.

Dependabot watches `pip` and `github-actions` weekly; alerts and automated
security fixes are enabled on the repo.

---

## License

MIT — see [LICENSE](LICENSE).

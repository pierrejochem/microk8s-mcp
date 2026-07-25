# Releasing

Pushing a `v*` tag runs `.github/workflows/release.yml`, which verifies the tag
against `pyproject.toml`, runs lint and tests, builds once, publishes that build
to PyPI, and attaches the same files to a GitHub Release.

```bash
# 1. bump the version in pyproject.toml, commit it
# 2. tag and push
git tag v0.2.0 && git push origin v0.2.0
```

The tag must equal `v` + the `project.version` in `pyproject.toml`. A mismatch
fails before anything is published.

---

## One-time setup: tell PyPI to trust this workflow

Publishing uses **Trusted Publishing** (OIDC). There is no API token stored in
this repository — PyPI verifies a short-lived token that GitHub mints for this
specific workflow, and nothing else can use it. That setup has to be done by
hand, once, while signed in to PyPI.

The project does not exist on PyPI yet, so register a **pending** publisher.

### PyPI

1. Sign in at <https://pypi.org> and open
   <https://pypi.org/manage/account/publishing/>.
2. Under *Add a new pending publisher*, choose **GitHub** and fill in:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `microk8s-mcp` |
   | Owner | `pierrejochem` |
   | Repository name | `microk8s-mcp` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

3. Save. The first successful publish creates the project and converts the
   pending publisher into a normal one.

### TestPyPI (optional, recommended for a dry run)

Repeat the same steps at <https://test.pypi.org/manage/account/publishing/>,
with the environment name **`testpypi`**.

Then rehearse without touching real PyPI:

*Actions → Release → Run workflow*, set **tag** to an existing tag and tick
**testpypi**. That publishes to TestPyPI and skips the GitHub Release.

Install the rehearsal to confirm it works:

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ microk8s-mcp
```

The extra index is needed because the `mcp` dependency lives on real PyPI.

### GitHub environments

The `pypi` and `testpypi` environments are created automatically on first use.
You do not need to pre-create them — but if you want a release to pause for
human approval, add yourself as a required reviewer under
*Settings → Environments → pypi*.

---

## What each job does

| Job | Purpose |
|---|---|
| `verify` | Tag matches `pyproject.toml`, and exports the version |
| `test` | ruff + pytest, run from outside the checkout so a stale install cannot pass |
| `build` | `python -m build`, `twine check --strict`, and a clean-venv install of the wheel |
| `pypi` | Publishes the built artifacts via Trusted Publishing |
| `release` | Attaches the same artifacts to a GitHub Release with generated notes |

`build` uploads its `dist/` as a workflow artifact and both `pypi` and
`release` download it, so the files on PyPI and the files on the Release are
byte-for-byte the same.

## If a publish fails

- **`invalid-publisher` / 403 from PyPI** — the pending publisher does not match.
  Every field is compared exactly, including the environment name and the
  workflow filename (`release.yml`, not the display name `Release`).
- **`File already exists`** — that version was already uploaded. PyPI does not
  allow re-uploading a version, even after deletion. Bump the version and tag
  again; there is no way around this.
- **The GitHub Release was skipped** — `release` needs `pypi`, so a failed
  publish stops it. Fix the cause and re-run the workflow with the same tag via
  *Run workflow*; `gh release create` will fail if the Release already exists,
  in which case delete it first.

## What ships in the wheel

Only the `microk8s_mcp` package and its entry point. The cluster-side setup —
`rbac.yaml`, `make-kubeconfig.sh` and the `make` targets — lives in the
repository, because it is operator tooling rather than library code. A user who
installs from PyPI still clones the repo for
[the identity step](https://pierrejochem.github.io/microk8s-mcp/install.html#identity).
# Getting Started

codex-lb runs with zero configuration — every setting has a working default, and Docker vs. host paths are auto-detected.

## Quick Start

```bash
# Docker (recommended)
docker volume create codex-lb-data
docker run -d --name codex-lb \
  -p 2455:2455 -p 1455:1455 \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/soju06/codex-lb:latest

# or uvx
uvx codex-lb

# or Nix
nix run github:Soju06/codex-lb
```

Open [localhost:2455](http://localhost:2455) → **Add account**:

- **Claude OAuth seat** — start the login from the dashboard, complete it at
  claude.ai, and paste the `CODE#STATE` callback value back into the dashboard.
  The copy/paste callback is the primary flow (Anthropic's authorize page shows
  the code instead of redirecting); a localhost callback listener is also
  started as a convenience when reachable from your browser.
- **Anthropic API key** — import a `sk-ant-…` key with
  `POST /api/accounts/import-api-key` (a dashboard import path is planned).

> **Fork status:** claude-lb is a work-in-progress fork of codex-lb. Account
> pooling (OAuth seats + API keys) is implemented; the `/v1/messages` proxy
> surface lands with roadmap increment 3
> (`openspec/changes/anthropic-upstream-adaptation/`). Until then the bundled
> Codex proxy core is inert reference code.

Next: point your coding agent at the proxy — see [Client Setup](client-setup.md) *(Codex-era page; being re-scoped with the proxy rewrite)*.

## Remote setup (bootstrap token)

When accessing the dashboard remotely for the first time, a bootstrap token is required to set the initial password.

**Auto-generated (default):** On first startup (no password configured), the server generates a one-time token and prints it to logs:

```bash
docker logs codex-lb
# ============================================
#   Dashboard bootstrap token (first-run):
#   <token>
# ============================================
```

Open the dashboard → enter the token + new password → done. The token is shared across replicas and remains valid until a password is set. In multi-replica setups, replicas must share the same encryption key (the Helm chart default) for restart recovery to work — see [Kubernetes deployment](deployment/kubernetes.md).

**Manual token:** To use a fixed token instead, set the env var before starting:

```bash
docker run -d --name codex-lb \
  -e CODEX_LB_DASHBOARD_BOOTSTRAP_TOKEN=your-secret-token \
  -p 2455:2455 -p 1455:1455 \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/soju06/codex-lb:latest
```

**Local access** (localhost) bypasses bootstrap entirely — no token needed.

Running behind a reverse proxy or exposing codex-lb to other machines? See [Remote Access](deployment/remote.md) and [Authentication](authentication.md).

---

*Spec: [deployment-installation](https://github.com/Soju06/codex-lb/tree/main/openspec/specs/deployment-installation)*

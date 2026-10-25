# agents-helmfile

Part of the claude-code-tatara project (generic).

A minimal helmfile repo that manages the Kubernetes namespace `agents`: a CNPG
Postgres (`agent-orchestrator-db`) and the `agent-orchestrator` Helm chart.

- Namespace: `agents` (created by helm on first install).
- Releases: `agent-orchestrator-db` (CNPG database) and `agent-orchestrator`.
- Host: `https://lab.szymonrichert.pl`. The ingress is public (cluster node IPs
  denied) and the api pod sits behind Keycloak SSO: the cluster's oauth2-proxy
  admission webhook injects a sidecar from pod annotations, reading the
  `agent-orchestrator-proxy` Secret that the infrastructure repo's `terraform/keycloak`
  creates. The api admits only 127.0.0.1 (that sidecar, or a `kubectl port-forward`).
- Every new agent session starts with `any-mcp` preinstalled.

## Tooling

Tools are pinned in `mise.toml`, mirrored from the infrastructure helmfile
(helm, helmfile, sops, kubectl, uv, shellcheck, gitleaks). Run `mise install`
once. An optional local `.env` is loaded through the mise `[env]` block; it is
gitignored, absent is fine, and its contents are never printed.

## Secrets

Secrets (the Claude OAuth token and the orchestrator master key) are
SOPS-encrypted under `values/<release>/default.secrets.yaml` and
`raw/regcred.pre.secrets.yaml`. They are created once with
`scripts/bootstrap-secrets.sh`, which pipes values straight into SOPS
encryption. The mise tasks never call it and never decrypt to disk.

## Operating

Applies are manual and local, run from this directory.

- `mise run lint` - ruff, shellcheck and gitleaks.
- `mise run test` - pytest over the rendered templates and the repo checks.
- `mise run diff` - `helmfile diff --detailed-exitcode --suppress-secrets`.
  Exit code 2 means there are changes (passed through, not an error); 0 means
  none.
- `mise run apply` - `helmfile apply --suppress-secrets`. Depends on `lint`,
  `test` and `kube-context`, so a broken tree cannot be applied. Failed upgrades
  roll back (`helmDefaults`); the hook creates the regcred and raw manifests.
- `mise run smoke` - proves the UI answers, that a fresh session lists `any-mcp`, and that the
  cluster's init scripts ran once and succeeded (`GET .../init-scripts`).
  Without `HOST` it port-forwards to the api (the public host needs SSO).
- `mise run kube-context` - guard used by diff and apply: fails unless a kube
  context is set and the API answers; prints only the context name.
- `mise run fmt` - format code.

Limit diff or apply to one release by passing the selector after `--`:
`mise run diff -- --selector name=agent-orchestrator`.

# agents-helmfile

Part of the claude-code-tatara project (generic).

A minimal helmfile repo that manages the Kubernetes namespace `agents`: a CNPG
Postgres (`agent-orchestrator-db`), the `agent-orchestrator` Helm chart and the
`agents-web` UI chart.

- Namespace: `agents` (created by helm on first install).
- Releases: `agent-orchestrator-db` (CNPG database), `agent-orchestrator` (API) and
  `agents-web` (static UI, image and chart pinned to the same sha7; sits behind the same
  oauth2-proxy secret and lists the orchestrator at `/api/v1` in its runtime config).
- Host: `https://lab.szymonrichert.pl`. The ingress is public (cluster node IPs
  denied) and the api pod sits behind Keycloak SSO: the cluster's oauth2-proxy
  admission webhook injects a sidecar from pod annotations, reading the
  `agent-orchestrator-proxy` Secret that the infrastructure repo's `terraform/keycloak`
  creates. The api admits only 127.0.0.1 (that sidecar, or a `kubectl port-forward`).
- URL layout of the host: `/` is the `agents-web` Ingress, `/api` is the `agent-orchestrator`
  Ingress. Both share the `agent-orchestrator-tls` secret; only the orchestrator Ingress carries
  the cert-manager annotations.
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
- `mise run smoke` - proves the UI on agents-web answers with the agents-web index and a `config.json` listing the orchestrator
  backend, that the host routes `/` to agents-web and `/api` to the orchestrator, that a fresh session lists `any-mcp`, that the
  cluster is created with wrapper settings, `leader_lease_seconds` and `autoclose_after_seconds` (3600) and its view echoes them with `closes_at` equal to `created_at` plus 3600 s and a `leader_since` once led (the trap discards it long before the autoclose), that the
  cluster's init scripts ran once and succeeded (`GET .../init-scripts`), then scales a 1-replica cluster (cap 2) to 2 and back to 1
  through `POST /api/v1/clusters/{id}/scale`, failing on a non-202 answer, a timeout or a restarted pod-0.
  Without `HOST` it port-forwards to the api and to `deploy/agents-web` (bypassing its SSO sidecar) (the public host needs SSO).
  It also checks the webhook service (see `## agents-webhook`) without starting a cluster, and deletes its temporary endpoint on every exit.
- `mise run kube-context` - guard used by diff and apply: fails unless a kube
  context is set and the API answers; prints only the context name.
- `mise run fmt` - format code.

Limit diff or apply to one release by passing the selector after `--`:
`mise run diff -- --selector name=agent-orchestrator`.

## agents-webhook

Releases: `agents-webhook-db` (CNPG database) and `agents-webhook` (deployments `agents-webhook-ingest`, `-api` and
`-worker`). The api pod sits behind the same SSO as the orchestrator and is served at `https://lab.szymonrichert.pl/webhooks/api`
(the Webhooks page of agents-web); the ingest pod is public at `https://lab.szymonrichert.pl/webhook/<suffix>` and protected by
per-endpoint basic auth instead. The worker reaches the orchestrator service port (`agent-orchestrator-services:8002`) with a
service token (`values/agent-orchestrator/service-token-agents-webhook.secrets.yaml` holds its hash, `values/agents-webhook/default.secrets.yaml` the token).

Create an endpoint in the Webhooks page of agents-web (or `POST /webhooks/api/v1/endpoints` with a type, a preset and an optional suffix).
The answer carries the endpoint URL, a username and a password. **The password is shown once**: copy it then; if it is lost, rotate it
(`POST /webhooks/api/v1/endpoints/{id}/rotate`) to get a new pair.

- GitHub (repository Settings, Webhooks, Add webhook): Payload URL `https://<username>:<password>@lab.szymonrichert.pl/webhook/<suffix>`
  (GitHub sends the userinfo as basic auth), content type `application/json`, events Issues, Issue comments, Pull requests and
  review events as the endpoint type needs (`github-issue` or `github-pr`).
- GitLab (project Settings, Webhooks): URL `https://<username>:<password>@lab.szymonrichert.pl/webhook/<suffix>`, triggers
  Issues and Comments, or Merge request events (`gitlab-issue` or `gitlab-mr`).
- Grafana (contact point of type Webhook): URL `https://lab.szymonrichert.pl/webhook/<suffix>`, HTTP method POST, and the
  username and password in the contact point's basic authentication fields (`grafana-alert`).

Use one endpoint per repository and type. The smoke creates and deletes its own `smoke-<random>` endpoint and never touches yours.

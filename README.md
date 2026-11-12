# agents-helmfile

Part of the claude-code-tatara project (generic).

A minimal helmfile repo that manages the Kubernetes namespace `agents`: a CNPG
Postgres (`agents-orchestrator-db`), the `agents-orchestrator` Helm chart, the
`agents-webhook` service and the `agents-web` UI chart.

- Namespace: `agents` (created by helm on first install).
- Releases: `agents-orchestrator-db` (CNPG database), `agents-orchestrator` (API; Deployments
  `agents-orchestrator-api`, `-dispatcher`, `-ingester` and `-reconciler`; Services
  `agents-orchestrator`, `agents-orchestrator-agents` on 8001 and `agents-orchestrator-services` on
  8002) and `agents-web` (static UI, image and chart pinned to the same sha7; sits behind the same
  oauth2-proxy secret and lists the orchestrator at `/api/v1` in its runtime config).
- The orchestrator's unit is a session: its API is `/api/v1/sessions`, and every agent pod carries
  the label `agents.szymonrichert.pl/session=<slug>`.
- Host: `https://lab.szymonrichert.pl`. The ingress is public (Kubernetes node IPs
  denied) and the api pod sits behind Keycloak SSO: the oauth2-proxy admission webhook
  injects a sidecar from pod annotations, reading the `agents-orchestrator-proxy` Secret that the
  infrastructure repo's `terraform/keycloak` creates. The api admits only 127.0.0.1 (that sidecar,
  or a `kubectl port-forward`).
- URL layout of the host: `/` is the `agents-web` Ingress, `/api` is the `agents-orchestrator`
  Ingress. Both share the `agents-orchestrator-tls` secret; only the orchestrator Ingress carries
  the cert-manager annotations.
- Every new agent session starts with `any-mcp` preinstalled (`ORCH_ANY_MCP_REF`); its orchestrator
  tools come from the orchestrator plugin, installed from
  https://github.com/szymonrychu/any-mcp-tatara-plugins at `ORCH_ANY_MCP_PLUGINS_REF`.

## Tooling

Tools are pinned in `mise.toml`, mirrored from the infrastructure helmfile
(helm, helmfile, sops, kubectl, uv, shellcheck, gitleaks). Run `mise install`
once. An optional local `.env` is loaded through the mise `[env]` block; it is
gitignored, absent is fine, and its contents are never printed.

Run `pre-commit install` once per clone to enable the gitleaks hook.

## Secrets

Secrets (the Claude OAuth token and the orchestrator master key) are
SOPS-encrypted under `values/<release>/default.secrets.yaml` and
`raw/regcred.pre.secrets.yaml`. They are created once with
`scripts/bootstrap-secrets.sh`, which pipes values straight into SOPS
encryption. The mise tasks never call it and never decrypt to disk.

The chat token of the agent chat relay is one token in two files, both created by
`bootstrap_chat_token` in the same script: `values/agents-orchestrator/chat-token.secrets.yaml`
(`chat.token`, the bearer the orchestrator sends to the connector's agent listener) and
`values/agents-matrix-connector/agent-token.secrets.yaml` (`agentApi.tokenSha256`, its sha256,
which is all the connector holds). The script writes both or neither, refuses when exactly one
exists, never rotates an existing pair and never prints either value. Each file is listed on its
release in `helmfile.yaml.gotmpl`, after that release's `default.secrets.yaml`.

The `agents-orchestrator` chart renders them as the Secrets `agents-orchestrator-claude`,
`agents-orchestrator-master-key` and `agents-orchestrator-services` (the service token hashes);
the database URI comes from the CNPG Secret `agents-orchestrator-db-cluster-app`.

## Cutover to agents-orchestrator

The rename to `agents-orchestrator` is a new release set, applied in selector-scoped steps:

1. `mise run apply -- --selector name=agent-orchestrator` uninstalls the retired `agent-orchestrator`
   release (declared with `installed: false`).
2. `mise run apply -- --selector name=agents-orchestrator-db` creates the new database, which imports
   the data of the old one.
3. `mise run apply` brings up `agents-orchestrator`, `agents-webhook` and `agents-web` on the new names;
   `mise run diff` then shows no drift and `mise run smoke` is green.

The old database release `agent-orchestrator-db` and its values dir `values/agent-orchestrator-db/`
stay declared and, once the smoke is green, are hibernated with the CNPG `cnpg.io/hibernation: "on"`
annotation: its pods stop, its volumes and backups stay, and it is the rollback point.

## CI

`.github/workflows/ci.yaml` runs on every pull request to main and every push to main (and by hand):
`mise run lint`, `mise run check-pins` (with the workflow `GITHUB_TOKEN` as `GH_TOKEN`; the pinned repos are public)
and `mise run test` (the whole suite, including `tests/test_orchestrator_release.py`). Actions are pinned by full
commit sha, checkout keeps no token in the git config and the token has `contents: read` only.

The release tests run `helmfile build` and `helmfile template`, which pull the charts from Harbor and decrypt the
SOPS/PGP values files (`.sops.yaml`, key `D39E46932A270AA3BA490B9DB9FE928D3E8BCED8`); `tests/test_secrets.py`
decrypts the token files in-process.

User decision 2026-11-02: the master key, and any SOPS/PGP key, never goes into GitHub. CI therefore has no
decryption key. The 25 tests that decrypt carry the marker `needs_sops_key`; `tests/conftest.py` probes once whether
the SOPS key opens `values/agents-orchestrator/default.secrets.yaml` and, when it does not, skips each marked test
with the reason `SOPS decryption key not available (CI by design, user decision 2026-11-02); covered by the mise run
apply gate`. CI runs pytest with `-rs`, so the log lists every skip and its reason. In an agent pod (the key exists)
nothing is skipped, and `mise run apply` runs the whole suite as its gate, so these tests still guard every deploy.

The job needs two repository secrets and fails first, naming whichever is missing:

- `HARBOR_USERNAME` and `HARBOR_PASSWORD`: a read-only chart-pull robot account of `harbor.szymonrichert.pl`
  (project `charts`); the job runs `helm registry login` with them.

### Trust model

The repository is public; `tests/test_ci_workflow.py` pins each rule below.

- Which events get secrets. The job `ci`, the only one that references a secret, runs for `push`,
  `workflow_dispatch` and a `pull_request` whose head repo is this repository (job-level `if` on
  `github.event.pull_request.head.repo.full_name == github.repository`). A pull request from a fork runs `fork-lint`
  instead: `mise run lint` only, no secret, and a notice in its log that the full suite runs after a maintainer
  merges or re-pushes the change from a same-repo branch. `pull_request_target` is never used.
- Order. Lint and check-pins run before the Harbor login, so the login is on the runner only for `mise run test`.
- Cleanup. A final `if: always()` step runs `helm registry logout` on the Harbor host, also after a failed step.
- No key. No SOPS, PGP or master key is stored as a GitHub secret or imported on the runner.
- No decrypted value is printed. Without the key none is decrypted in CI; the flags stay as defence in depth, because
  pytest prints decrypted values on a failure: assertion diffs such as `assert digest == hashlib.sha256(token.encode()).hexdigest()` show the token,
  failure messages carry the helmfile stderr, YAML errors quote the offending line, and with `CI` set pytest
  truncates nothing. CI therefore runs `mise run test -- --tb=no --show-capture=no --disable-warnings -rs -vv`: the
  log names each test and its outcome, plus the reason of each skip, and nothing else; run `mise run test` with the key locally to see why a test
  failed. No step uses xtrace, and no step echoes a rendered manifest.
- No Kubernetes API. Under `helmfile build` and `helmfile template` the `prepare` hook of `.hook.sh` runs, and it
  diffs raw/ (piping the decrypted `raw/regcred.pre.secrets.yaml` into `kubectl diff`) whenever
  `kubectl cluster-info` answers. The tests set `KUBECONFIG=/dev/null`, but kubectl then falls back to the
  in-cluster service account when `KUBERNETES_SERVICE_HOST` is set (a runner in a pod). The workflow sets
  `KUBECONFIG=/dev/null` and empties `KUBERNETES_SERVICE_HOST` and `KUBERNETES_SERVICE_PORT`, so the hook prints
  `No reachable cluster during 'prepare'` and skips raw/; a step refuses the run, before any secret is used, if
  `kubectl cluster-info` still answers. No step runs apply, sync, diff or a smoke.

## Operating

Applies are manual and local, run from this directory.

- `mise run lint` - ruff, shellcheck and gitleaks.
- `mise run test` - pytest over the rendered templates and the repo checks.
- `mise run diff` - `helmfile diff --detailed-exitcode --suppress-secrets`.
  Exit code 2 means there are changes (passed through, not an error); 0 means
  none.
- `mise run check-pins` - `uv run scripts/check_pins.py`: for every pinned main-built commit (the
  `agents-orchestrator`, `agents-web`, `agents-webhook`, `agents-schedule`, `agents-matrix-connector` and
  `agents-usage` chart versions, the orchestrator, web and agents-usage `image.tag`, and the claude-code-wrapper `agentImage` and
  `ORCH_AGENT_IMAGES`) it resolves the commit (`0.1.0-main.<sha7>` or an image sha7 is that commit, a
  release `X.Y.Z` goes through tag `vX.Y.Z`) and lists its check runs with `gh api` (paginated). A pin
  passes only when at least one check run exists and every one is completed with conclusion `success`,
  `neutral` or `skipped`. It prints `pin <release> <version> <commit>: ok|refused (<reasons>)` per pin
  and exits non-zero naming each refused pin; a missing or unparsable pin is refused. No flag or
  environment variable skips it. Needs `gh` logged in (`gh auth status`).
- `mise run apply` - `helmfile apply --suppress-secrets`. Depends on `lint`,
  `test`, `check-pins` and `kube-context`, so a broken tree or a red build cannot be applied. Failed upgrades
  roll back (`helmDefaults`); the hook creates the regcred and raw manifests.
- `mise run smoke` - proves the UI on agents-web answers with the agents-web index and a `config.json` listing the orchestrator
  backend, that the host routes `/` to agents-web and `/api` to `agents-orchestrator`, that a fresh session lists `any-mcp`, that the
  session is created with wrapper settings and `autoclose_after_seconds` (3600) and its view echoes them with `closes_at` equal to `created_at` plus 3600 s and a `leader_since` once led (the trap discards it long before the autoclose), that the
  session's init scripts ran once and succeeded (`GET /api/v1/sessions/{id}/init-scripts`), that an agent pod reaches `/agent/v1/me`
  with `X-Orch-Session` from `ORCH_SESSION_ID`, then scales a 1-replica session (cap 2) to 2,
  makes pod-1's agent the leader (`POST /api/v1/sessions/{id}/leader`, 202) and scales back to 1 through `POST /api/v1/sessions/{id}/scale`:
  a scale-down below the leader must hand leadership to pod-0 (`leader_reason` `scale_down`) before pod-1 goes. It fails on a non-202 answer,
  a timeout, a leader that does not move with that reason or a restarted pod-0.
  `RELEASE` (default `agents-orchestrator`) names the orchestrator release.
  Without `HOST` it port-forwards to `deploy/agents-orchestrator-api` and to `deploy/agents-web` (bypassing their SSO sidecars) (the public host needs SSO).
  It also checks the webhook service (see `## agents-webhook`) without starting a session, and deletes its temporary endpoint on every exit.
  It also checks the matrix connector (`MATRIX_RELEASE`, default `agents-matrix-connector`) without sending any Matrix message or
  calling the homeserver: the `-api` and `-bot` Deployments are Available; through a port-forward to the api container (bypassing
  its SSO sidecar) `GET /matrix/api/v1/meta` answers `user_id` `@hermes:szymonrichert.pl` and `sync.healthy` true; a temporary deny
  rule `@smoke-<random>:invalid.example` is created, listed and deleted through `/rules` (deleted on every exit); `GET /rooms`
  answers 200; and the UI `config.json` lists the `matrix` backend.
  Last it checks the workspace cache: `GET /api/v1/meta` must report `cache.available`; session A (cache key `smoke-workspace-cache`)
  writes a random marker in its init script and is discarded (its save runs in the discard), then session B on the same key must
  restore and its init must find the marker, else the smoke fails with "workspace cache did not carry the marker". The object
  `workspace-cache/smoke-workspace-cache.tar.zst` stays and is overwritten on each run; both sessions are deleted on any failure.
  Plugin tools: in the hive session's pod (`kubectl exec -c ccw`) `/opt/any-mcp/bin/pip show any-mcp-tatara-orchestrator` must succeed,
  `/opt/any-mcp/etc/any-mcp.yaml` must name `any-mcp-tatara-plugins//orchestration/orchestrator?ref=<ORCH_ANY_MCP_PLUGINS_REF>` and
  `any-mcp --config /opt/any-mcp/etc/any-mcp.yaml --list-tools` must list `orchestrator_sessions_list`. Extra source: session
  `smoke-src-<ts>` (`any_mcp_sources` holding the `examples/petstore` source at the pinned any-mcp tag) gets a Ready pod whose
  `--list-tools` lists a `petstore_` tool; the trap deletes it on every exit. Neither step prints the pod config or tool lists.
  Then the session options. Kubernetes access: `GET /api/v1/meta` must report `kube_cluster_access.available`; session `smoke-kube-<ts>`
  (`kube_cluster_access` true) gets a Ready pod running as its own per-session ServiceAccount `agent-<slug>-<sid>` (slug `smoke-kube-<ts>`, sid 8 hex) with its token mounted, plus a ClusterRoleBinding labelled with the session slug naming `cluster-admin` (bound only for `kube_cluster_access` sessions), and from inside it
  (`kubectl exec -c ccw`, curl with the mounted token and CA against `KUBERNETES_SERVICE_HOST`/`PORT`, no kubectl in the pod) a
  SelfSubjectAccessReview for `*` on `*` must answer allowed true and `GET /api/v1/namespaces` 200, which is full Kubernetes access; the
  hive session's pod must have `automountServiceAccountToken` false and no token file. Sessions with `kube_namespace_access` (step 11a2, `smoke-kns-<ts>`, `{media: [read]}`) get RoleBindings per namespace tier instead, which the smoke checks from inside the pod and which are gone after the discard. Cross-session messaging: superleader
  `smoke-msg-a-<ts>` (`session_messaging` true) and plain `smoke-msg-b-<ts>`; from A's pod (its own `ORCH_AGENT_*` env, never printed)
  `GET /agent/v1/sessions` lists B and the hive session, `POST /agent/v1/sessions/<B>/messages` answers 202 with a `message_id`, the
  message reaches `sent` and B's events carry a `user_message` starting `<session-message from="<A>"`; from B's pod the list shows A and
  not the hive session and a send to the hive session answers 403 `project_forbidden`: B is created in the project `smoke-proj-<ts>` (made through `POST /api/v1/projects`, the session's `project` and the project's `archived` flag are read back, the project is archived on every exit) while the hive session sits in `default`; same-project sessions are peers, so only a different project walls B off. The three sessions are deleted on every exit.
  Last, session continuity: session `smoke-cont-<ts>` (1 replica) gets a Ready pod and one completed turn (a message that reaches `sent`
  and a stored `turn_completed`); the smoke reads the agent's `wrapper_session_id` from `GET /api/v1/sessions/{id}/events?agent_id=...`,
  deletes that pod and only that pod (`kubectl delete pod/agent-smoke-cont-<ts>-0`, refused unless the name is a smoke-cont pod), and
  requires a new pod uid and Ready, an agent view state that is not `dead`, a second message that reaches `sent`, and stored events after
  the restart that carry the same `wrapper_session_id`. Each of those waits has its own `SMOKE_TIMEOUT_S` window; message text, events and
  uids are matched, never printed. The session is deleted on every exit. The deployed orchestrator must carry the ingester re-attach fix.
- `mise run chat-smoke` - `bash scripts/chat-smoke.sh`, the live chat smoke S1-S4 in the owner DM
  (`CHAT_SMOKE_ROOM`, default `!iLWrAwaTMYnINJsyNv:szymonrichert.pl`). **It posts real messages to the owner DM from the owner's
  Facebook account**, so it is never part of `mise run smoke`; run it after the connector and orchestrator applies and after setting
  `agents_may_initiate` on the owner DM. It reads the DM's `binding` from the connector UI API (`GET /matrix/api/v1/rooms/{room_id}`,
  port-forward to `deploy/agents-matrix-connector-api` on `SMOKE_MATRIX_API_LOCAL_PORT`, default 18004) and picks its caller (1:1
  room-session binding): when the DM is bound, the leader pod of the bound session (hive: the pod of `leader_agent_id` in
  `GET /api/v1/sessions/{id}/agents`; alone: its only agent), using only that pod's own `ORCH_AGENT_*` env, with no message to or
  change of that session and nothing created or deleted; when it is unbound, a throwaway alone session `chat-smoke-<ts>` (the spec of
  `CHAT_SMOKE_PRESET`, a preset id or name, else the default preset, with `replicas` and `max_replicas` 1) whose S1 initiates and
  binds the DM; it is deleted at the end and the script waits until the connector releases the binding. A DM reserved by a first run
  in progress (a binding without a session) stops it. Before S1 it prints the hours since the DM's newest inbound message. From the
  caller pod, through the orchestrator agent routes `/agent/v1/chat/*`, the room list must name the DM, then S1 sends a message with no
  inbound message in the run, S2 replies to S1's `s<n>`, S3 reacts with a thumbs-up to it and S4 with a second, different emoji; each
  send's status is polled to `sent`, `failed` or `refused` with its own `CHAT_SMOKE_TIMEOUT_S` deadline (default 600 s; a timeout
  exits 1 naming the step). S1-S3 must end `sent`; S4 is printed with its status and reason (or the api refusal) and never fails the
  run. It prints the caller (`caller: bound <slug>` or `caller: throwaway <slug>`), the binding before, during (throwaway) and after,
  and every id, status and reason. It owns its port-forwards (`SMOKE_LOCAL_PORT`, default 18000, to `deploy/agents-orchestrator-api`):
  a stale `kubectl port-forward` on either port is killed first, and the trap kills its own and deletes the throwaway session (only
  one it created) on success, failure and interrupt. No token or `ORCH_AGENT_*` value is printed.
- `mise run kube-context` - guard used by diff and apply: fails unless a kube
  context is set and the API answers; prints only the context name.
- `mise run fmt` - format code.

Limit diff or apply to one release by passing the selector after `--`:
`mise run diff -- --selector name=agents-orchestrator`.

## agents-webhook

Releases: `agents-webhook-db` (CNPG database) and `agents-webhook` (deployments `agents-webhook-ingest`, `-api` and
`-worker`). The api pod sits behind the same SSO as the orchestrator and is served at `https://lab.szymonrichert.pl/webhooks/api`
(the Webhooks page of agents-web); the ingest pod is public at `https://lab.szymonrichert.pl/webhook/<suffix>` and protected by
per-endpoint basic auth instead. The worker reaches the orchestrator service port (`agents-orchestrator-services:8002`) with a
service token (`values/agents-orchestrator/service-token-agents-webhook.secrets.yaml` holds its hash, `values/agents-webhook/default.secrets.yaml` the token).

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

## agents-usage

Releases: `agents-usage-db` (CNPG database `usage`) and `agents-usage` (deployments `agents-usage-api`, `-prober` and `-watcher`).
The api pod sits behind the same SSO as the orchestrator and is served at `https://lab.szymonrichert.pl/usage`. Its two token
listeners are not on the ingress: the service port (the orchestrator's usage gate) and the ingest port (the agents' statusLine
samples) are reached in-cluster through the `agents-usage` Service.

`scripts/bootstrap-secrets.sh` creates its secrets, each written once and never rotated or printed:

- `bootstrap_usage_token` (both files or neither): the raw Secret `agents-usage-service-tokens`
  (`values/agents-usage/raw/service-tokens.agents-usage.pre.secrets.yaml`, key `tokens`) and the orchestrator's copy
  `values/agents-orchestrator/usage-token.secrets.yaml` (`usage.token`, the same token).
- `bootstrap_service_token agents-usage` (both files or neither): the watcher's token `values/agents-usage/default.secrets.yaml`
  (`orchestrator.token`) and its sha256 `values/agents-orchestrator/service-token-agents-usage.secrets.yaml`, which the
  orchestrator release lists once the orchestrator learns the usage service.
- `bootstrap_usage_ingest_token`: the single shared token of the agents, the raw Secret `agents-usage-ingest`
  (`values/agents-usage/raw/ingest-token.agents-usage.pre.secrets.yaml`, key `token`, 64 hex characters, no newline). The agent
  pods read the same Secret as `CCW_USAGE_TOKEN`.

The prober reads the orchestrator's Secret `agents-orchestrator-claude` (key `CLAUDE_CODE_OAUTH_TOKEN`). The backup credentials
`values/agents-usage-db/default.secrets.yaml` are a byte copy of the other databases' file, as for `agents-schedule-db`.

## agents-history

Releases: `agents-history-db` (CNPG database `history`, 10Gi) and `agents-history` (one api pod with three ports: UI 8000, service 8001,
ingest 8002). The api sits behind the same SSO as the orchestrator and is served at `https://lab.szymonrichert.pl/history`. The service
listener (the orchestrator's read and search relay) and the ingest listener (the orchestrator's session export at discard) are not on
the ingress and are reached in-cluster through the `agents-history` Service.

`scripts/bootstrap-secrets.sh` creates its secrets, each written once and never rotated or printed:

- `bootstrap_history_token` (both files or neither): the raw Secret `agents-history-service-tokens`
  (`values/agents-history/raw/service-tokens.agents-history.pre.secrets.yaml`, key `tokens`) and the orchestrator's copy
  `values/agents-orchestrator/history-token.secrets.yaml` (`admin.history.token`, the same token).
- `bootstrap_history_ingest_token`: the raw Secret `agents-history-ingest`
  (`values/agents-history/raw/ingest-token.agents-history.pre.secrets.yaml`, key `token`, 64 hex characters, no newline). It differs
  from the service token (the service exits 78 otherwise).

The backup credentials `values/agents-history-db/default.secrets.yaml` are a byte copy of the other databases' file. The first
migration creates the `pg_trgm` extension; if CNPG refuses it, set `cluster.initdb.postInitApplicationSQL` on the db release.

## agents-taskboard

Releases: `agents-taskboard-db` (CNPG database `taskboard`) and `agents-taskboard` (deployments `-web`, `-service` and `-notifier`).
The web pod sits behind the same SSO as the orchestrator and is served at `https://lab.szymonrichert.pl/taskboard`; agents-web lists
it as the `taskboard` backend (`/taskboard/api/v1`). The service listener (the orchestrator relay, port 8002) is reached in-cluster
through the `agents-taskboard-service` Service.

`scripts/bootstrap-secrets.sh` creates its secrets, each written once and never rotated or printed:

- `bootstrap_service_token agents-taskboard` (both files or neither): the notifier's token `values/agents-taskboard/default.secrets.yaml`
  (`orchestrator.token`, also used for the session label lookup) and its sha256
  `values/agents-orchestrator/service-token-agents-taskboard.secrets.yaml`.
- `bootstrap_taskboard_relay_token` (both files or neither): the orchestrator relay's bearer, `relayToken` in
  `values/agents-taskboard/relay-token.secrets.yaml` and the same value as `taskboard.token` in
  `values/agents-orchestrator/taskboard-token.secrets.yaml`. It differs from the notifier's service token.

The backup credentials `values/agents-taskboard-db/default.secrets.yaml` are a byte copy of the other databases' file, as for `agents-schedule-db`.

## agents-app-connector

Releases: `agents-app-connector-db` (CNPG database `appconn`) and `agents-app-connector` (an admin pod behind the orchestrator's SSO and a
singleton app pod with the sketch PVC). Only two paths are routed on `https://lab.szymonrichert.pl`: `/app/v1/sketch` (device API,
bearer device tokens, public) and `/app/admin` (pairing, SSO). `bootstrap_service_token agents-app-connector` (both files or neither)
writes `values/agents-app-connector/default.secrets.yaml` (`orchestrator.token`) and its sha256 in
`values/agents-orchestrator/service-token-agents-app-connector.secrets.yaml`. The backup credentials
`values/agents-app-connector-db/default.secrets.yaml` are a byte copy of the other databases' file.

Since connector 2.0.1 (needs orchestrator 4.4.0) agent tokens are verified through the orchestrator's service port (`orchestrator.url`, 8002), so the connector has no `orchestrator.agentUrl` value.

## agents-vault

Releases: `agents-vault-db` (CNPG database `vault`, 5Gi) and `agents-vault` (deployments `-web`, `-service` and `-sweeper`). The web
pod (port 8000) sits behind the same SSO as the orchestrator and is served at `https://lab.szymonrichert.pl/vault`; the service
listener (port 8002, bearer tokens, for the orchestrator) is reached in-cluster through the `agents-vault-service` Service. The sweeper
has no port. The vault stores secret values only as immutable Kubernetes Secrets; its Role allows create, patch and delete on
Secrets, never get, list or watch.

`scripts/bootstrap-secrets.sh` creates its secrets, each written once and never rotated or printed. The two tokens are generated
independently, so they differ (`serve-service` exits 78 otherwise):

- `bootstrap_service_token agents-vault` (both files or neither): the vault's own token toward the orchestrator
  `values/agents-vault/default.secrets.yaml` (`orchestrator.token`) and its sha256
  `values/agents-orchestrator/service-token-agents-vault.secrets.yaml`, listed on the orchestrator release.
- `bootstrap_vault_token` (both files or neither): the orchestrator's bearer toward the vault service listener, the raw Secret
  `agents-vault-service-tokens` (`values/agents-vault/raw/service-tokens.agents-vault.pre.secrets.yaml`, key `tokens`) and the same
  value as `vault.token` in `values/agents-orchestrator/vault-token.secrets.yaml`. That file is deliberately not listed on the
  orchestrator release until stage 2 (the orchestrator chart has no `vault.*` values yet).

Restart gotcha: the vault reaches the orchestrator only after the `agents-orchestrator` release is applied (it renders the new hash
into the Secret `agents-orchestrator-services`) and `kubectl -n agents rollout restart deploy/agents-orchestrator-api` makes the pods
read it. Until then the vault gets 401 from the orchestrator (the sweeper releases nothing, which is the safe failure).

The backup credentials `values/agents-vault-db/default.secrets.yaml` are a byte copy of the other databases' file (the S3 path
differs through values).

Projects stage 1 apply order: agents-orchestrator (6.8.1), then agents-taskboard (0.4.0, needs the orchestrator's `X-Orch-Projects` header), then agents-web (1.6.0). Releases are git tags: verify a pin with `git ls-remote --tags https://github.com/szymonrychu/<repo> v<ver>`.

## CNPG plugin backups and standard image

All ten `*-db` releases (chart `cnpg/cluster` 0.9.0) back up through the Barman Cloud plugin
(`barman-cloud.cloudnative-pg.io`, release `plugin-barman-cloud` in namespace `postgres-operator`), not the
deprecated in-tree `barmanObjectStore` (removed in CNPG 1.32.0). Values per release: `cluster.plugins`
(`isWALArchiver: true`), `backups.method: plugin` with `pluginConfiguration`, and the same on each scheduled backup.
Bucket, path, region, retention and `wal.maxParallel` are unchanged; the chart builds the `ObjectStore` from them.

Per-cluster steps (one cluster at a time; each cluster has one instance, so each switch is a short outage of that database).
Order: unused/low-risk first, `agents-orchestrator-db` and `agents-history-db` last.

1. `kubectl -n agents get cluster <release>-cluster` is healthy; on-demand backup first
   (`kubectl cnpg backup <release>-cluster -n agents`) and wait for `completed`.
2. `mise run diff -- --selector name=<release>`: expect Cluster gains `plugins` and loses `backup.barmanObjectStore`, an
   `ObjectStore` appears, ScheduledBackup method changes, nothing else.
3. `mise run apply -- --selector name=<release>`; the Cluster rolls once.
4. Check `ContinuousArchiving` is true, then take a plugin backup (`kubectl cnpg backup <release>-cluster -n agents -m plugin
   --plugin-name barman-cloud.cloudnative-pg.io`) and confirm it completes.

Image move to `standard` (separate change, after the plugin is live on that cluster): `standard` images carry no Barman
binaries, so the plugin must come first. Before each switch run `SELECT datname, datcollate, datctype FROM pg_database`
(all checked databases are C/C). Rollback order: revert the image first, then the backup method.

Metric rename: `cnpg_collector_last_failed_backup_timestamp` becomes
`barman_cloud_cloudnative_pg_io_last_failed_backup_timestamp` (and two more); update alerts and dashboards that read them.
`agent-orchestrator-db` is hibernated: its values change but nothing rolls until it is woken.

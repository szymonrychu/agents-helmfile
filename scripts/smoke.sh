#!/usr/bin/env bash
# End-to-end smoke of a deployed agents-orchestrator and its UI: the UI (agents-web release) answers with the agents-web index and a
# config.json listing the orchestrator backend, the host routes / to agents-web and /api to the orchestrator, a 1-replica hive session (cap 2)
# with an init script yields a Ready pod, a succeeded init and a leader, that pod's `claude mcp list` shows any-mcp,
# it reaches /agent/v1/me through the agents Service with 200 (proves the pod source IP survives) and
# /agent/v1/activity?limit=5 with 200 and a JSON body whose `lines` is an array,
# the view echoes the wrapper, lease and autoclose settings with closes_at = created_at + 3600s and a leader_since,
# and the public scale route grows the session to 2 pods, makes pod-1's agent the leader, then scales back to 1: the scale-down below the
# leader must hand leadership to pod-0 (leader_reason scale_down) before pod-1 goes, and pod-0 never restarts.
# Parameters by env: HOST, RELEASE, WEB_RELEASE, NAMESPACE, SMOKE_TIMEOUT_S, SMOKE_LOCAL_PORT,
# SMOKE_WEB_LOCAL_PORT (SCHEME for tests only).
# Without HOST it port-forwards the UI to deploy/agents-web (the nginx container, bypassing the SSO sidecar) and the API to the orchestrator: the public host is behind SSO, and the api's
# source allowlist admits only 127.0.0.1 (the oauth2-proxy sidecar, or a port-forward).
# Webhook steps (agents-webhook release): the three deployments are Available; a temporary github-issue endpoint is created through the
# webhook API (port-forward to the api container, bypassing its SSO sidecar; its password stays in a shell variable and the trap deletes
# the endpoint), the ingest Service answers /healthz, accepts a closed-issue delivery with the credentials (202 ignored/action_ignored) and
# rejects a wrong password and an unknown suffix with the same 401; the events API lists the event; the orchestrator service port answers
# 200 with the webhook token and 401 without it from an agent pod; the public host answers 401 with the Basic realm. Extra env:
# WEBHOOK_RELEASE, SMOKE_WEBHOOK_API_LOCAL_PORT, SMOKE_WEBHOOK_INGEST_LOCAL_PORT, SMOKE_PUBLIC_URL (tests only).
# Schedule steps (agents-schedule release): a disabled schedule smoke-<ts> on the default preset (cron 0 3 * * *, so it never fires on its own) is
# created through the schedule API (port-forward to the api container, bypassing its SSO sidecar), /readyz answers, the cron preview lists
# 3 fire times, run-now answers 202 and the run reaches in-progress with a session_id; the trap deletes that session through the orchestrator
# API and the schedule through the schedule API. Extra env: SCHEDULE_RELEASE, SMOKE_SCHEDULE_API_LOCAL_PORT.
# Workspace cache steps: GET /api/v1/meta reports cache.available; session A (cache key smoke-workspace-cache) writes a random marker
# in its init script and is discarded (its save runs in the discard); session B with the same key must restore before its init and its
# init greps the marker. The object workspace-cache/smoke-workspace-cache.tar.zst stays and is overwritten on each run.
# Matrix connector steps (agents-matrix-connector release, never sends a Matrix message nor calls the homeserver): the -api and -bot
# deployments are Available; through a port-forward to the api container (bypassing its SSO sidecar) GET /matrix/api/v1/meta answers
# user_id @hermes:szymonrichert.pl and sync.healthy true (proves the token, the homeserver path and the sync loop); a deny rule
# {list: deny, kind: user, value: @smoke-<random>:invalid.example} is created (POST /rules), listed (GET /rules) and deleted
# (DELETE /rules/{id}, the trap deletes it on failure); GET /rooms answers 200; the UI config.json lists the matrix backend.
# Extra env: MATRIX_RELEASE, SMOKE_MATRIX_API_LOCAL_PORT.
# Session option steps (after the workspace cache steps): GET /api/v1/meta reports kube_access.available; a session smoke-kube-<ts> with
# kube_access true gets a Ready pod running as a ServiceAccount ending -kube-access with its token mounted, and from inside it (curl with the
# mounted token and CA against KUBERNETES_SERVICE_HOST/PORT, no kubectl in the pod) a SelfSubjectAccessReview for verb, group and resource *
# answers allowed true and GET /api/v1/namespaces answers 200, which is full Kubernetes access; the hive session's pod has no mounted token.
# Cross-session messaging: superleader smoke-msg-a-<ts> (session_messaging true) and plain smoke-msg-b-<ts> get Ready pods; from A's pod
# (its own ORCH_AGENT_* env, never printed) GET /agent/v1/sessions lists B and the hive session, a send to B answers 202 with a message_id,
# the message reaches sent and B's events carry a user_message framed <session-message from="<A slug>"; from B's pod the list shows A and
# not the hive session and a send to the hive session answers 403 target_not_superleader. The trap deletes all three sessions.
# Plugin tools steps: in the hive session's pod (container ccw) the any-mcp-tatara-orchestrator distribution is installed (pip show), the
# rendered /opt/any-mcp/etc/any-mcp.yaml names any-mcp-tatara-plugins//orchestration/orchestrator at the pinned ORCH_ANY_MCP_PLUGINS_REF and
# `any-mcp --list-tools` lists orchestrator_sessions_list and orchestrator_activity; a session smoke-src-<ts> created with any_mcp_sources holding the petstore example at
# the pinned any-mcp tag gets a Ready pod whose --list-tools lists a petstore_ tool. The trap deletes smoke-src-<ts>; outputs (a JSON array of tools) are only matched by tool name.
# Session continuity (last step): session smoke-cont-<ts> gets a Ready pod and one completed turn; its agent's wrapper_session_id is read from
# GET /api/v1/sessions/{id}/events?agent_id=..., then only that session's pod is deleted (kubectl delete pod/agent-smoke-cont-<ts>-0): the
# pod returns Ready with a new uid, the agent view state is not dead, a second message reaches sent and the stored events after the restart
# carry the same wrapper_session_id. Each wait has its own SMOKE_TIMEOUT_S window; the trap deletes the session.
# Prints no secrets: only status lines, pod events and the mcp list.
set -euo pipefail

# The any-mcp the sessions install: the orchestrator release's pinned ORCH_ANY_MCP_REF, leading v stripped.
ORCH_VALUES="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/values/agents-orchestrator/common.yaml"
ANY_MCP_VERSION=$(sed -n 's/^[[:space:]]*ORCH_ANY_MCP_REF:[[:space:]]*v\{0,1\}\([^[:space:]#]*\).*/\1/p' "$ORCH_VALUES" 2> /dev/null | head -n1)
if [ -z "$ANY_MCP_VERSION" ]; then
  echo "smoke: FAIL: no ORCH_ANY_MCP_REF in ${ORCH_VALUES}" >&2
  exit 1
fi

# The plugins repo ref the sessions install the orchestrator plugin from (kept with its leading v, as in the pod config).
PLUGINS_REF=$(sed -n 's/^[[:space:]]*ORCH_ANY_MCP_PLUGINS_REF:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$ORCH_VALUES" 2> /dev/null | head -n1)
if [ -z "$PLUGINS_REF" ]; then
  echo "smoke: FAIL: no ORCH_ANY_MCP_PLUGINS_REF in ${ORCH_VALUES}" >&2
  exit 1
fi

NAMESPACE="${NAMESPACE:-agents}"
SMOKE_TIMEOUT_S="${SMOKE_TIMEOUT_S:-600}"
RELEASE="${RELEASE:-agents-orchestrator}"
WEB_RELEASE="${WEB_RELEASE:-agents-web}"
PF_PID=""
WEB_PF_PID=""
WH_PF_PIDS=""
WEBHOOK_RELEASE="${WEBHOOK_RELEASE:-agents-webhook}"
SCHEDULE_RELEASE="${SCHEDULE_RELEASE:-agents-schedule}"
MATRIX_RELEASE="${MATRIX_RELEASE:-agents-matrix-connector}"
MATRIX_PF_PID=""
MATRIX_RULE_ID=""
SCHED_PF_PID=""
SCHEDULE_ID=""
SCHED_RUN_ID=""
SCHED_SESSION_ID=""
CACHE_KEY="smoke-workspace-cache"
CACHE_SESSION_IDS=""
OPTION_SESSION_IDS=""
if [ -z "${HOST:-}" ]; then
  HOST="127.0.0.1:${SMOKE_LOCAL_PORT:-18000}"
  WEB_HOST="127.0.0.1:${SMOKE_WEB_LOCAL_PORT:-18080}"
  SCHEME=http
  kubectl -n "$NAMESPACE" port-forward "deploy/${WEB_RELEASE}" "${WEB_HOST#*:}:8080" > /dev/null 2>&1 &
  WEB_PF_PID=$!
  kubectl -n "$NAMESPACE" port-forward "deploy/${RELEASE}-api" "${HOST#*:}:8000" > /dev/null 2>&1 &
  PF_PID=$!
fi
BASE="${SCHEME:-https}://${HOST}"
WEB_BASE="${SCHEME:-https}://${WEB_HOST:-$HOST}"
SLUG="smoke-$(date +%s)"
SESSION_ID=""
ENDPOINT_ID=""
WH_API_BASE="$BASE"
WH_INGEST_BASE="$BASE"
SCHED_API_BASE="$BASE"
MATRIX_API_BASE="$BASE"
PUBLIC_URL="${SMOKE_PUBLIC_URL:-https://lab.szymonrichert.pl}"

export SMOKE_LOG_PREFIX=smoke
# shellcheck source=lib/smoke-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/smoke-common.sh"

cleanup() {
  local rc=$?
  trap - EXIT
  if [ -n "$SESSION_ID" ]; then
    if curl -fsS --max-time 20 -X DELETE -o /dev/null "${BASE}/api/v1/sessions/${SESSION_ID}"; then
      log "discarded session ${SLUG} (${SESSION_ID})"
    else
      echo "smoke: cleanup failed, API unreachable; delete session '${SLUG}' (id ${SESSION_ID}) manually" >&2
    fi
  fi
  for oid in $OPTION_SESSION_IDS; do
    if curl -fsS --max-time 20 -X DELETE -o /dev/null "${BASE}/api/v1/sessions/${oid}"; then
      log "discarded session ${oid}"
    else
      echo "smoke: cleanup failed, API unreachable; delete option session id ${oid} manually" >&2
    fi
  done
  for cid in $CACHE_SESSION_IDS; do
    if curl -fsS --max-time 20 -X DELETE -o /dev/null "${BASE}/api/v1/sessions/${cid}"; then
      log "discarded cache session ${cid}"
    else
      echo "smoke: cleanup failed, API unreachable; delete cache session id ${cid} manually" >&2
    fi
  done
  if [ -n "$ENDPOINT_ID" ]; then
    if curl -fsS --max-time 20 -X DELETE -o /dev/null "${WH_API_BASE}/webhooks/api/v1/endpoints/${ENDPOINT_ID}"; then
      log "deleted webhook endpoint ${ENDPOINT_ID}"
    else
      echo "smoke: cleanup failed, webhook API unreachable; delete webhook endpoint id ${ENDPOINT_ID} manually" >&2
    fi
  fi
  if [ -n "$SCHEDULE_ID" ]; then
    # A run that was still waiting may have created its session since the last poll.
    if [ -z "$SCHED_SESSION_ID" ] && [ -n "$SCHED_RUN_ID" ]; then
      SCHED_SESSION_ID=$(curl -fsS --max-time 20 "${SCHED_API_BASE}/schedules/api/v1/runs/${SCHED_RUN_ID}" 2> /dev/null | jq -r '.session_id // empty' 2> /dev/null) || SCHED_SESSION_ID=""
    fi
    if [ -n "$SCHED_SESSION_ID" ]; then
      if curl -fsS --max-time 20 -X DELETE -o /dev/null "${BASE}/api/v1/sessions/${SCHED_SESSION_ID}"; then
        log "discarded schedule session ${SCHED_SESSION_ID}"
      else
        echo "smoke: cleanup failed, API unreachable; delete schedule session id ${SCHED_SESSION_ID} manually" >&2
      fi
    fi
    if curl -fsS --max-time 20 -X DELETE -o /dev/null "${SCHED_API_BASE}/schedules/api/v1/schedules/${SCHEDULE_ID}"; then
      log "deleted schedule ${SCHEDULE_ID}"
    else
      echo "smoke: cleanup failed, schedule API unreachable; delete schedule id ${SCHEDULE_ID} manually" >&2
    fi
  fi
  if [ -n "$MATRIX_RULE_ID" ]; then
    if curl -fsS --max-time 20 -X DELETE -o /dev/null "${MATRIX_API_BASE}/matrix/api/v1/rules/${MATRIX_RULE_ID}"; then
      log "deleted matrix rule ${MATRIX_RULE_ID}"
    else
      echo "smoke: cleanup failed, matrix API unreachable; delete matrix rule id ${MATRIX_RULE_ID} manually" >&2
    fi
  fi
  [ -n "$MATRIX_PF_PID" ] && kill "$MATRIX_PF_PID" 2> /dev/null
  [ -n "$SCHED_PF_PID" ] && kill "$SCHED_PF_PID" 2> /dev/null
  for pid in $WH_PF_PIDS; do kill "$pid" 2> /dev/null || true; done
  [ -n "$PF_PID" ] && kill "$PF_PID" 2> /dev/null
  [ -n "$WEB_PF_PID" ] && kill "$WEB_PF_PID" 2> /dev/null
  exit "$rc"
}
trap cleanup EXIT

# 0. The port-forward, when used, is up.
if [ -n "$PF_PID" ]; then
  for _ in $(seq 1 20); do
    curl -s --max-time 2 -o /dev/null "${BASE}/healthz" && break
    sleep 1
  done
fi
if [ -n "$WEB_PF_PID" ]; then
  for _ in $(seq 1 20); do
    curl -s --max-time 2 -o /dev/null "${WEB_BASE}/" && break
    sleep 1
  done
fi

# 1. UI (agents-web) and its runtime config answer.
page=$(curl -fsS --max-time 20 "${WEB_BASE}/") || fail "UI ${WEB_BASE}/ not reachable"
case "$page" in
  *'<div id="root">'*) log "UI answers with the agents-web index" ;;
  *) fail "UI body is not the agents-web index (no <div id=\"root\">; an SSO page?)" ;;
esac
web_cfg=$(curl -fsS --max-time 20 "${WEB_BASE}/config.json") || fail "UI ${WEB_BASE}/config.json not reachable"
echo "$web_cfg" | jq -e '[.backends[]? | select(.id == "orchestrator" and .basePath == "/api/v1")] | length > 0' > /dev/null ||
  fail "config.json lists no backend orchestrator with basePath /api/v1: ${web_cfg}"
log "config.json lists backend orchestrator at /api/v1"

# 1a. API contract document answers.
curl -fsS --max-time 20 -o /dev/null "${BASE}/api/v1/openapi.json" || fail "openapi.json not reachable"
log "openapi.json answers"

# 1b. The agents Service (leader-to-agent traffic) exists on 8001 and is not an ingress backend.
AGENTS_SVC="${RELEASE}-agents"
agents_port=$(kubectl -n "$NAMESPACE" get service "$AGENTS_SVC" -o 'jsonpath={.spec.ports[0].port}' 2> /dev/null) ||
  fail "Service ${AGENTS_SVC} not found in ${NAMESPACE}"
[ "$agents_port" = "8001" ] || fail "Service ${AGENTS_SVC} port is ${agents_port}, expected 8001"
# shellcheck disable=SC2016  # go-template, not shell expansion
routes=$(kubectl -n "$NAMESPACE" get ingress -o 'go-template={{range .items}}{{range .spec.rules}}{{$h := .host}}{{range .http.paths}}{{$h}} {{.path}} {{.backend.service.name}}{{"\n"}}{{end}}{{end}}{{end}}') ||
  fail "cannot list ingresses in ${NAMESPACE}"
case " $(echo "$routes" | awk '{print $3}' | tr '\n' ' ') " in
  *" ${AGENTS_SVC} "*) fail "Service ${AGENTS_SVC} is an ingress backend" ;;
esac
log "Service ${AGENTS_SVC} on 8001, not an ingress backend"
# Path routing of the public host (when any Ingress exists): / to the UI, /api to the orchestrator.
if [ -n "$(echo "$routes" | tr -d '[:space:]')" ]; then
  route_host=lab.szymonrichert.pl
  echo "$routes" | grep -Fxq "${route_host} / ${WEB_RELEASE}" ||
    fail "Ingress host ${route_host} does not route / to Service ${WEB_RELEASE}"
  echo "$routes" | grep -Fxq "${route_host} /api ${RELEASE}" ||
    fail "Ingress host ${route_host} does not route /api to Service ${RELEASE}"
  log "Ingress ${route_host}: / to ${WEB_RELEASE}, /api to ${RELEASE}"
fi

# 2. Create a 1-replica session with cap 2 (a hive), an init script (the smoke makes its own, never touches existing ones), wrapper settings,
# a leader lease and an autoclose (an hour: the cleanup trap discards the session long before it fires) through the public API.
body=$(jq -n --arg slug "$SLUG" '{slug: $slug, spec: {replicas: 1, max_replicas: 2, init_scripts: ["echo smoke-init"],
  wrapper: {auto_compact: {idle_seconds: 1800}, hooks: {onTurnComplete: [{type: "command", run: "true"}]}},
  leader_lease_seconds: 3600, autoclose_after_seconds: 3600}}')
resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$body" "${BASE}/api/v1/sessions") ||
  fail "session create failed"
SESSION_ID=$(echo "$resp" | jq -er '.id') || fail "create response carries no id"
log "created session ${SLUG} (${SESSION_ID})"

# 2a. The session view echoes the configuration and closes_at is created_at plus the autoclose.
view=$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${SESSION_ID}") || fail "config check: session view not reachable"
echo "$view" | jq -e '.spec.wrapper.auto_compact.idle_seconds == 1800 and .spec.wrapper.hooks.onTurnComplete[0].run == "true"
  and .spec.leader_lease_seconds == 3600 and .spec.autoclose_after_seconds == 3600' > /dev/null ||
  fail "config check: view spec does not echo wrapper, leader_lease_seconds and autoclose_after_seconds: $(echo "$view" | jq -c '.spec')"
echo "$view" | jq -e '.closes_at != null' > /dev/null || fail "config check: view has no closes_at"
echo "$view" | jq -e 'def ts: sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601; (.closes_at | ts) - (.created_at | ts) == 3600' > /dev/null ||
  fail "config check: closes_at is not created_at + 3600s (created_at $(echo "$view" | jq -r '.created_at'), closes_at $(echo "$view" | jq -r '.closes_at'))"
log "config fields echoed in the view; closes_at is created_at + 3600s"

# 3. Wait for the agent pod to exist and be Ready.
label="agents.szymonrichert.pl/session=${SLUG}"
deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
pod=""
while [ -z "$pod" ]; do
  # By name: the init Job pod carries the same session label and is never the agent.
  pod=$(kubectl -n "$NAMESPACE" get pods -l "$label" -o name 2> /dev/null | grep -Fx "pod/agent-${SLUG}-0" || true)
  [ -n "$pod" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp 2>&1 | tail -n 30 >&2 || true
    fail "no agent pod appeared within ${SMOKE_TIMEOUT_S}s"
  fi
  sleep 5
done
remaining=$((deadline - $(date +%s)))
[ "$remaining" -lt 1 ] && remaining=1
if ! kubectl -n "$NAMESPACE" wait --for=condition=Ready "$pod" --timeout="${remaining}s"; then
  kubectl -n "$NAMESPACE" describe "$pod" 2>&1 | sed -n '/^Events:/,$p' >&2 || true
  kubectl -n "$NAMESPACE" get events --field-selector "involvedObject.name=${pod#pod/}" 2>&1 >&2 || true
  fail "pod ${pod} not Ready within ${SMOKE_TIMEOUT_S}s"
fi
log "pod ${pod} is Ready"

# 4. any-mcp must be preinstalled. claude-code-wrapper writes MCP servers into
# each session's own config dir (/sessions/<id>/config), so a bare
# `claude mcp list` reads the wrong config; point CLAUDE_CONFIG_DIR at it.
cfg=""
while [ -z "$cfg" ]; do
  cfg=$(kubectl -n "$NAMESPACE" exec "$pod" -- sh -c 'ls -d /sessions/*/config 2> /dev/null | head -n1' 2> /dev/null || true)
  [ -n "$cfg" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    fail "no /sessions/*/config dir in ${pod} within ${SMOKE_TIMEOUT_S}s"
  fi
  sleep 5
done
log "session config dir ${cfg}"
mcp=$(kubectl -n "$NAMESPACE" exec "$pod" -- env "CLAUDE_CONFIG_DIR=${cfg}" claude mcp list 2>&1) || true
if ! echo "$mcp" | grep 'any-mcp' | grep -q 'Connected'; then
  echo "smoke: claude mcp list (CLAUDE_CONFIG_DIR=${cfg}):" >&2
  echo "$mcp" >&2
  fail "any-mcp not Connected in claude mcp list"
fi
log "claude mcp list shows any-mcp Connected"
ver=$(kubectl -n "$NAMESPACE" exec "$pod" -- /opt/any-mcp/bin/any-mcp --version 2>&1) || true
case "$ver" in
  *" ${ANY_MCP_VERSION}" | "${ANY_MCP_VERSION}") log "any-mcp version ${ANY_MCP_VERSION}" ;;
  *) fail "any-mcp --version is not ${ANY_MCP_VERSION}: ${ver}" ;;
esac

# 4a. The orchestrator tools come from the plugins repo. Outputs are only matched, never printed (the config and tool list may carry env names).
ANY_MCP_CFG=/opt/any-mcp/etc/any-mcp.yaml
kubectl -n "$NAMESPACE" exec "$pod" -c ccw -- /opt/any-mcp/bin/pip show any-mcp-tatara-orchestrator > /dev/null 2>&1 ||
  fail "plugin tools: any-mcp-tatara-orchestrator is not installed in ${pod}"
pod_cfg=$(kubectl -n "$NAMESPACE" exec "$pod" -c ccw -- cat "$ANY_MCP_CFG" 2> /dev/null) || pod_cfg=""
plugin_source="any-mcp-tatara-plugins//orchestration/orchestrator?ref=${PLUGINS_REF}"
case "$pod_cfg" in
  *"$plugin_source"*) ;;
  *) fail "plugin tools: ${ANY_MCP_CFG} in ${pod} names no ${plugin_source}" ;;
esac
pod_tools=$(kubectl -n "$NAMESPACE" exec "$pod" -c ccw -- /opt/any-mcp/bin/any-mcp --config "$ANY_MCP_CFG" --list-tools 2> /dev/null) || pod_tools=""
echo "$pod_tools" | jq -e 'any(.[]?; .name == "orchestrator_sessions_list")' > /dev/null 2>&1 ||
  fail "plugin tools: any-mcp --list-tools in ${pod} lists no orchestrator_sessions_list"
echo "$pod_tools" | jq -e 'any(.[]?; .name == "orchestrator_activity")' > /dev/null 2>&1 ||
  fail "plugin tools: any-mcp --list-tools in ${pod} lists no orchestrator_activity"
log "orchestrator plugin installed from ${plugin_source}; --list-tools lists orchestrator_sessions_list and orchestrator_activity"

# 4b. The init scripts ran once and succeeded, and are visible through the public API.
init_json=""
while :; do
  init_json=$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${SESSION_ID}/init-scripts") || init_json=""
  state=$(echo "$init_json" | jq -r '.status.state // empty' 2> /dev/null || true)
  case "$state" in
    succeeded) break ;;
    failed)
      fail "init scripts failed: reason=$(echo "$init_json" | jq -r '.status.reason // ""') exit_code=$(echo "$init_json" | jq -r '.status.exit_code // ""') log_tail=$(echo "$init_json" | jq -r '.status.log_tail // ""')"
      ;;
  esac
  if [ "$(date +%s)" -ge "$deadline" ]; then
    fail "init scripts did not succeed within ${SMOKE_TIMEOUT_S}s (state '${state}')"
  fi
  sleep 3
done
log "init scripts state succeeded"
echo "$init_json" | jq -e '.status.log_tail | contains("smoke-init")' > /dev/null ||
  fail "init log tail lacks smoke-init: $(echo "$init_json" | jq -r '.status.log_tail // ""')"
log "init log tail contains smoke-init"
echo "$init_json" | jq -e '.scripts == ["echo smoke-init"]' > /dev/null ||
  fail "init scripts are not [\"echo smoke-init\"]: $(echo "$init_json" | jq -c '.scripts')"
log "init scripts succeeded and visible: [\"echo smoke-init\"]"

# 5. The hive has a leader (the dispatcher elects the lowest-ordinal ready agent).
leader=""
while [ -z "$leader" ]; do
  leader=$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${SESSION_ID}" | jq -r '.leader_agent_id // empty') || leader=""
  [ -n "$leader" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    fail "session ${SLUG} has no leader within ${SMOKE_TIMEOUT_S}s"
  fi
  sleep 3
done
log "session ${SLUG} leader ${leader}"
curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${SESSION_ID}" | jq -e '.leader_since != null' > /dev/null ||
  fail "config check: session ${SLUG} has a leader but no leader_since"
log "leader_since is set"

# 6. From the agent pod, with its own env, GET /agent/v1/me on the agents Service: 200 only if
# the token, the session and the pod-IP binding all hold (the CNI kept the source IP).
me_py='import os, urllib.request, urllib.error
req = urllib.request.Request(os.environ["ORCH_AGENT_URL"].rstrip("/") + "/agent/v1/me", headers={"Authorization": "Bearer " + os.environ["ORCH_AGENT_TOKEN"], "X-Orch-Session": os.environ["ORCH_SESSION_ID"]})
try:
    print(urllib.request.urlopen(req, timeout=15).status)
except urllib.error.HTTPError as e:
    print(e.code)'
me_status=$(kubectl -n "$NAMESPACE" exec "$pod" -- python3 -c "$me_py" 2>&1 | tail -n1) || true
[ "$me_status" = "200" ] || fail "GET /agent/v1/me from ${pod} (via ORCH_AGENT_URL, ORCH_AGENT_TOKEN, ORCH_SESSION_ID) answered '${me_status}', expected 200"
log "GET /agent/v1/me from ${pod} through the agents Service: 200"

# 6a. From the same pod and env, GET /agent/v1/activity?limit=5 (read-only): 200 and a JSON object whose `lines` is an array.
# The in-pod python prints "<status> <has_lines>" and nothing else; the body is never printed.
activity_py='import os, json, urllib.request, urllib.error
req = urllib.request.Request(os.environ["ORCH_AGENT_URL"].rstrip("/") + "/agent/v1/activity?limit=5", headers={"Authorization": "Bearer " + os.environ["ORCH_AGENT_TOKEN"], "X-Orch-Session": os.environ["ORCH_SESSION_ID"]})
try:
    resp = urllib.request.urlopen(req, timeout=15)
    status, raw = resp.status, resp.read()
except urllib.error.HTTPError as e:
    status, raw = e.code, b""
try:
    body = json.loads(raw)
except ValueError:
    body = None
print(status, isinstance(body, dict) and isinstance(body.get("lines"), list))'
activity_result=$(kubectl -n "$NAMESPACE" exec "$pod" -- python3 -c "$activity_py" 2>&1 | tail -n1) || true
[ "$activity_result" = "200 True" ] || fail "GET /agent/v1/activity?limit=5 from ${pod} (via ORCH_AGENT_URL, ORCH_AGENT_TOKEN, ORCH_SESSION_ID) answered '${activity_result}', expected '200 True' (status 200 and a lines array)"
log "GET /agent/v1/activity from ${pod} through the agents Service: 200 with a lines array"

# 8. The webhook service (agents-webhook release). No session is started: the only side effects are one temporary endpoint
# (deleted by the trap) and one ignored event row.
# 8a. The three deployments are Available.
for part in ingest api worker; do
  kubectl -n "$NAMESPACE" wait --for=condition=Available "deploy/${WEBHOOK_RELEASE}-${part}" --timeout="${SMOKE_TIMEOUT_S}s" > /dev/null ||
    fail "deploy/${WEBHOOK_RELEASE}-${part} is not Available within ${SMOKE_TIMEOUT_S}s"
done
log "deploy/${WEBHOOK_RELEASE}-ingest, -api and -worker are Available"

# 8b. Port-forwards: the api container port (8000, bypassing the SSO sidecar on the Service port) and the ingest Service.
if [ -n "$PF_PID" ]; then
  WH_API_HOST="127.0.0.1:${SMOKE_WEBHOOK_API_LOCAL_PORT:-18001}"
  WH_INGEST_HOST="127.0.0.1:${SMOKE_WEBHOOK_INGEST_LOCAL_PORT:-18002}"
  kubectl -n "$NAMESPACE" port-forward "deploy/${WEBHOOK_RELEASE}-api" "${WH_API_HOST#*:}:8000" > /dev/null 2>&1 &
  WH_PF_PIDS="$WH_PF_PIDS $!"
  kubectl -n "$NAMESPACE" port-forward "svc/${WEBHOOK_RELEASE}-ingest" "${WH_INGEST_HOST#*:}:80" > /dev/null 2>&1 &
  WH_PF_PIDS="$WH_PF_PIDS $!"
  WH_API_BASE="http://${WH_API_HOST}"
  WH_INGEST_BASE="http://${WH_INGEST_HOST}"
  for base in "$WH_API_BASE" "$WH_INGEST_BASE"; do
    for _ in $(seq 1 20); do
      curl -s --max-time 2 -o /dev/null "${base}/healthz" && break
      sleep 1
    done
  done
fi

# 8c. Create a temporary github-issue endpoint on the default preset. The username and password live only in these variables.
preset_id=$(curl -fsS --max-time 20 "${BASE}/api/v1/presets" | jq -er '[.[] | select(.is_default)][0].id') ||
  fail "webhook: no default preset in GET /api/v1/presets"
WH_SUFFIX="smoke-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
deny_filters='{"deny":[{"field":"title","match":"regex","values":["^smoke-deny"]}]}'
ep_body=$(jq -n --arg preset "$preset_id" --arg suffix "$WH_SUFFIX" --argjson filters "$deny_filters" \
  '{type: "github-issue", preset_id: $preset, suffix: $suffix, filters: $filters}')
ep_resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$ep_body" "${WH_API_BASE}/webhooks/api/v1/endpoints") ||
  fail "webhook: endpoint create failed (POST /webhooks/api/v1/endpoints)"
ENDPOINT_ID=$(echo "$ep_resp" | jq -er '.id') || fail "webhook: endpoint create response carries no id"
WH_USER=$(echo "$ep_resp" | jq -er '.username') || fail "webhook: endpoint create response carries no username"
WH_PASS=$(echo "$ep_resp" | jq -er '.password') || fail "webhook: endpoint create response carries no password"
echo "$ep_resp" | jq -e '.filters.deny[0].field == "title"' > /dev/null || fail "webhook: endpoint create did not store the filters"
unset ep_resp
log "created webhook endpoint ${ENDPOINT_ID} (suffix ${WH_SUFFIX})"
log "endpoint stored the deny filter"

# 8d. Ingest: healthz, a closed-issue delivery with the credentials, a wrong password, an unknown suffix. Credentials go to curl on
# stdin, never on the command line.
curl -fsS --max-time 20 -o /dev/null "${WH_INGEST_BASE}/healthz" || fail "webhook: ingest healthz not 200"
log "ingest healthz 200"
issue_payload='{"action":"closed","issue":{"number":1,"title":"smoke","body":"","user":{"login":"smoke"},"labels":[]},"repository":{"full_name":"smoke/smoke"},"sender":{"login":"smoke"}}'
deliver() { # $1 suffix, $2 password; prints "<status> <body>"
  local out
  out=$(printf 'user = "%s:%s"\n' "$WH_USER" "$2" |
    curl -sS --max-time 20 -K - -X POST -H 'Content-Type: application/json' -H 'X-GitHub-Event: issues' -d "$issue_payload" \
      -w '\n%{http_code}' "${WH_INGEST_BASE}/webhook/$1") || return 1
  printf '%s %s' "${out##*$'\n'}" "${out%$'\n'*}"
}
good=$(deliver "$WH_SUFFIX" "$WH_PASS") || fail "webhook: delivery request failed"
[ "${good%% *}" = "202" ] || fail "webhook: delivery with credentials answered ${good%% *}, expected 202"
echo "${good#* }" | jq -e '.status == "ignored" and .reason == "action_ignored"' > /dev/null ||
  fail "webhook: delivery is not ignored/action_ignored: ${good#* }"
EVENT_ID=$(echo "${good#* }" | jq -er '.id') || fail "webhook: delivery answer carries no id"
log "ingest accepted the closed-issue delivery: 202 ignored (action_ignored)"
bad=$(deliver "$WH_SUFFIX" "wrong-$(date +%s)") || fail "webhook: wrong password request failed"
[ "${bad%% *}" = "401" ] || fail "webhook: wrong password answered ${bad%% *}, expected 401"
log "ingest rejected a wrong password: 401"
unknown=$(deliver "smoke-nosuch-$(date +%s)" "$WH_PASS") || fail "webhook: unknown suffix request failed"
[ "${unknown%% *}" = "401" ] || fail "webhook: unknown suffix answered ${unknown%% *}, expected 401"
[ "${unknown#* }" = "${bad#* }" ] || fail "webhook: unknown suffix 401 body differs from the wrong password 401 body"
log "ingest rejected an unknown suffix: 401 with the same body"
unset WH_PASS

# 8e. The events API lists the delivery as ignored.
events=$(curl -fsS --max-time 20 "${WH_API_BASE}/webhooks/api/v1/events?endpoint_id=${ENDPOINT_ID}&limit=5") ||
  fail "webhook: events API not reachable"
echo "$events" | jq -e --argjson id "$EVENT_ID" 'any(.[]; .id == $id and .status == "ignored")' > /dev/null ||
  fail "webhook: events API does not list event ${EVENT_ID} as ignored for endpoint ${ENDPOINT_ID}: $(echo "$events" | jq -c '[.[] | {id, status, reason}]' 2> /dev/null)"
log "events API lists event ${EVENT_ID} as ignored"

# 8e2. Filters: a preview (nothing is ingested, so no session can start) is denied by the stored rule, then the PUT clears the rules.
pv_body=$(jq -n --argjson filters "$deny_filters" \
  '{type: "github-issue", example: {action: "opened", issue: {number: 2, title: "smoke-deny check", body: "", user: {login: "smoke"}, labels: []}, repository: {full_name: "smoke/smoke"}, sender: {login: "smoke"}}, filters: $filters}')
pv_resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$pv_body" "${WH_API_BASE}/webhooks/api/v1/templates/preview") ||
  fail "webhook: filter preview failed (POST /webhooks/api/v1/templates/preview)"
echo "$pv_resp" | jq -e '.filters.passed == false and .filters.reason == "filter_denied: title" and .prompt == null' > /dev/null ||
  fail "webhook: filter preview was not denied with filter_denied: title: $(echo "$pv_resp" | jq -c '{prompt, filters}' 2> /dev/null)"
log "preview denied by the filter (filter_denied: title)"
put_resp=$(curl -fsS --max-time 20 -X PUT -H 'Content-Type: application/json' -d '{"allow":[],"deny":[]}' \
  "${WH_API_BASE}/webhooks/api/v1/endpoints/${ENDPOINT_ID}/filters") ||
  fail "webhook: filters PUT failed (PUT /webhooks/api/v1/endpoints/${ENDPOINT_ID}/filters)"
echo "$put_resp" | jq -e '.filters.deny == []' > /dev/null || fail "webhook: filters PUT did not clear the deny rules"
log "filters replaced: PUT cleared the deny rules"

# 8f. The orchestrator service port (8002) from an agent pod: 200 with the webhook token, 401 without. The token is read from the
# agents-webhook Secret and handed to the pod on stdin.
SVC_URL="http://${RELEASE}-services:8002"
svc_py='import sys, urllib.request, urllib.error
token = sys.stdin.read().strip()
def status(headers):
    try:
        return urllib.request.urlopen(urllib.request.Request(sys.argv[1] + "/api/v1/presets", headers=headers), timeout=15).status
    except urllib.error.HTTPError as e:
        return e.code
print(status({"Authorization": "Bearer " + token}), status({}))'
wh_token=$(kubectl -n "$NAMESPACE" get secret "${WEBHOOK_RELEASE}-orchestrator" -o 'jsonpath={.data.token}' | base64 -d) ||
  fail "webhook: cannot read the ${WEBHOOK_RELEASE}-orchestrator token Secret"
svc_status=$(kubectl -n "$NAMESPACE" exec -i "$pod" -- python3 -c "$svc_py" "$SVC_URL" <<< "$wh_token" 2>&1 | tail -n1) || true
unset wh_token
[ "$svc_status" = "200 401" ] ||
  fail "webhook: GET ${SVC_URL}/api/v1/presets answered '${svc_status}' (with token, without token), expected '200 401'"
log "${SVC_URL}/api/v1/presets: 200 with the webhook token, 401 without"

# 8g. The public ingress routes /webhook to the ingest pod (not SSO): 401 with the Basic realm.
hdrs=$(curl -sS --max-time 20 -D - -o /dev/null -X POST -H 'Content-Type: application/json' -d '{}' "${PUBLIC_URL}/webhook/smoke-unknown") ||
  fail "webhook: public ${PUBLIC_URL}/webhook/smoke-unknown not reachable"
pub_status=$(echo "$hdrs" | awk 'NR==1 {print $2}')
[ "$pub_status" = "401" ] || fail "webhook: public POST /webhook/smoke-unknown answered '${pub_status}', expected 401 (an SSO redirect?)"
echo "$hdrs" | tr -d '\r' | grep -iqx 'www-authenticate: Basic realm="agents-webhook"' ||
  fail "webhook: public 401 lacks WWW-Authenticate: Basic realm=\"agents-webhook\""
log "public ${PUBLIC_URL}/webhook/smoke-unknown: 401 with WWW-Authenticate: Basic realm=\"agents-webhook\""

# 8h. The matrix connector (agents-matrix-connector release). It sends no Matrix message and calls no homeserver: the only side effect
# is one temporary deny rule (deleted below, and by the trap on failure).
for part in api bot; do
  kubectl -n "$NAMESPACE" wait --for=condition=Available "deploy/${MATRIX_RELEASE}-${part}" --timeout="${SMOKE_TIMEOUT_S}s" > /dev/null ||
    fail "matrix: deploy/${MATRIX_RELEASE}-${part} is not Available within ${SMOKE_TIMEOUT_S}s"
done
log "deploy/${MATRIX_RELEASE}-api and -bot are Available"

if [ -n "$PF_PID" ]; then
  MATRIX_API_HOST="127.0.0.1:${SMOKE_MATRIX_API_LOCAL_PORT:-18004}"
  kubectl -n "$NAMESPACE" port-forward "deploy/${MATRIX_RELEASE}-api" "${MATRIX_API_HOST#*:}:8000" > /dev/null 2>&1 &
  MATRIX_PF_PID=$!
  MATRIX_API_BASE="http://${MATRIX_API_HOST}"
  for _ in $(seq 1 20); do
    curl -s --max-time 2 -o /dev/null "${MATRIX_API_BASE}/healthz" && break
    sleep 1
  done
fi
MX="${MATRIX_API_BASE}/matrix/api/v1"

# The identity and the sync loop: user_id is the Hermes Matrix user and sync.healthy is true.
mx_meta=$(curl -fsS --max-time 20 "${MX}/meta") || fail "matrix: GET ${MX}/meta failed"
[ "$(echo "$mx_meta" | jq -r '.user_id')" = "@hermes:szymonrichert.pl" ] ||
  fail "matrix: meta user_id is not @hermes:szymonrichert.pl: $(echo "$mx_meta" | jq -c '{user_id}' 2> /dev/null)"
echo "$mx_meta" | jq -e '.sync.healthy == true' > /dev/null ||
  fail "matrix: meta sync is not healthy: $(echo "$mx_meta" | jq -c '{sync}' 2> /dev/null)"
log "matrix meta: user_id @hermes:szymonrichert.pl, sync healthy"

# A deny rule round trip: create, list, delete.
mx_value="@smoke-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n'):invalid.example"
mx_body=$(jq -cn --arg v "$mx_value" '{list: "deny", kind: "user", value: $v}')
mx_resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$mx_body" "${MX}/rules") ||
  fail "matrix: rule create failed (POST ${MX}/rules)"
MATRIX_RULE_ID=$(echo "$mx_resp" | jq -er '.id') || fail "matrix: rule create response carries no id"
mx_rules=$(curl -fsS --max-time 20 "${MX}/rules") || fail "matrix: GET ${MX}/rules failed"
echo "$mx_rules" | jq -e --argjson id "$MATRIX_RULE_ID" --arg v "$mx_value" 'any(.deny[]; .id == $id and .value == $v)' > /dev/null ||
  fail "matrix: GET /rules does not list the deny rule ${MATRIX_RULE_ID}"
curl -fsS --max-time 20 -X DELETE -o /dev/null "${MX}/rules/${MATRIX_RULE_ID}" || fail "matrix: DELETE ${MX}/rules/${MATRIX_RULE_ID} failed"
log "matrix rules: created, listed and deleted deny rule ${MATRIX_RULE_ID}"
MATRIX_RULE_ID=""

curl -fsS --max-time 20 -o /dev/null "${MX}/rooms" || fail "matrix: GET ${MX}/rooms did not answer 200"
log "matrix rooms: 200"

echo "$web_cfg" | jq -e '[.backends[]? | select(.id == "matrix" and .basePath == "/matrix/api/v1")] | length > 0' > /dev/null ||
  fail "matrix: config.json lists no backend matrix with basePath /matrix/api/v1: ${web_cfg}"
log "config.json lists backend matrix at /matrix/api/v1"

# 7. Scale 1 -> 2 -> 1 through the public route, with pod-1 as leader at the scale-down; pod-0 must never restart.
uid0=$(kubectl -n "$NAMESPACE" get "$pod" -o 'jsonpath={.metadata.uid}') || fail "cannot read uid of ${pod}"
[ -n "$uid0" ] || fail "uid of ${pod} is empty"
scale() {
  local code
  code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    -d "{\"replicas\": $1}" "${BASE}/api/v1/sessions/${SESSION_ID}/scale") || fail "scale to $1 request failed"
  [ "$code" = "202" ] || fail "scale to $1 answered ${code}, expected 202"
  log "scale to $1 accepted (202)"
}
pod_names() { kubectl -n "$NAMESPACE" get pods -l "$label" -o name 2> /dev/null | grep -E "^pod/agent-${SLUG}-[0-9]+\$" | sort || true; }

deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
scale 2
pod1=""
while [ -z "$pod1" ]; do
  pod1=$(pod_names | grep -vx "$pod" | head -n1 || true)
  [ -n "$pod1" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && fail "scale up: pod-1 did not appear within ${SMOKE_TIMEOUT_S}s"
  sleep 3
done
remaining=$((deadline - $(date +%s)))
[ "$remaining" -lt 1 ] && remaining=1
kubectl -n "$NAMESPACE" wait --for=condition=Ready "$pod1" --timeout="${remaining}s" ||
  fail "scale up: ${pod1} not Ready within ${SMOKE_TIMEOUT_S}s"
log "scaled up: ${pod1} is Ready"

session_leader() { curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${SESSION_ID}" | jq -r '(.leader_agent_id // "") + " " + (.leader_reason // "")'; }
agent_of() {
  curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${SESSION_ID}/agents" |
    jq -r --arg pod "$1" '[.[] | select(.pod_name == $pod and .ready == true and (.gone | not))][0].id // empty'
}
agent0=$(agent_of "${pod#pod/}") || agent0=""
[ -n "$agent0" ] || fail "scale down: no ready agent for ${pod} in the session agents list"
# Every wait below starts its own SMOKE_TIMEOUT_S window, so a slow pod-1 Ready cannot starve the waits after it.
deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
agent1=""
while [ -z "$agent1" ]; do
  agent1=$(agent_of "${pod1#pod/}") || agent1=""
  [ -n "$agent1" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && fail "scale down: no ready agent for ${pod1} in the session agents list within ${SMOKE_TIMEOUT_S}s"
  sleep 3
done
# The orchestrator rejects a /leader request for a not-ready target without retrying it, so re-POST until the leader moves.
make_leader() {
  local code
  code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    -d "{\"agent_id\": \"${agent1}\"}" "${BASE}/api/v1/sessions/${SESSION_ID}/leader") || fail "make ${pod1} leader: request failed"
  [ "$code" = "202" ] || fail "make ${pod1} leader answered ${code}, expected 202"
}
deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
make_leader
posted_at=$(date +%s)
while [ "$(session_leader | cut -d' ' -f1)" != "$agent1" ]; do
  [ "$(date +%s)" -ge "$deadline" ] && fail "scale down: leader did not become ${pod1} (${agent1}) within ${SMOKE_TIMEOUT_S}s"
  if [ "$(($(date +%s) - posted_at))" -ge "${SMOKE_LEADER_RETRY_S:-30}" ]; then
    make_leader
    posted_at=$(date +%s)
  fi
  sleep 3
done
log "scale down: ${pod1} (${agent1}) is the leader"

deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
scale 1
while [ "$(session_leader)" != "${agent0} scale_down" ]; do
  [ "$(date +%s)" -ge "$deadline" ] && fail "scale down: leader did not move to pod-0 with reason scale_down within ${SMOKE_TIMEOUT_S}s (now: $(session_leader || true))"
  sleep 3
done
log "scale down: leader is ${agent0} with reason scale_down"
deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
while [ -n "$(pod_names | grep -vx "$pod" || true)" ]; do
  [ "$(date +%s)" -ge "$deadline" ] && fail "scale down: ${pod1} still present after ${SMOKE_TIMEOUT_S}s"
  sleep 3
done
log "scaled down: ${pod1} is gone"

uid_now=$(kubectl -n "$NAMESPACE" get "$pod" -o 'jsonpath={.metadata.uid}') || fail "${pod} is gone after scaling"
[ "$uid_now" = "$uid0" ] || fail "pod-0 uid changed after scaling (${uid0} -> ${uid_now}): pod-0 restarted"
log "pod-0 kept uid ${uid0}"

# 9. The schedule service (agents-schedule release). The schedule is created disabled: only run-now starts a session.
# 9a. Port-forward to the api container (8000, bypassing the SSO sidecar on the Service port); /readyz answers.
if [ -n "$PF_PID" ]; then
  SCHED_API_HOST="127.0.0.1:${SMOKE_SCHEDULE_API_LOCAL_PORT:-18003}"
  kubectl -n "$NAMESPACE" port-forward "deploy/${SCHEDULE_RELEASE}-api" "${SCHED_API_HOST#*:}:8000" > /dev/null 2>&1 &
  SCHED_PF_PID=$!
  SCHED_API_BASE="http://${SCHED_API_HOST}"
  for _ in $(seq 1 20); do
    curl -s --max-time 2 -o /dev/null "${SCHED_API_BASE}/readyz" && break
    sleep 1
  done
fi
SCHED_BASE="${SCHED_API_BASE}/schedules/api/v1"
curl -fsS --max-time 20 -o /dev/null "${SCHED_API_BASE}/readyz" || fail "schedule: ${SCHED_API_BASE}/readyz not 200"
log "schedule API readyz 200"

# 9b. Create a disabled schedule on the default preset.
sched_preset=$(curl -fsS --max-time 20 "${BASE}/api/v1/presets" | jq -er '[.[] | select(.is_default)][0].id') ||
  fail "schedule: no default preset in GET /api/v1/presets"
sched_body=$(jq -n --arg name "$SLUG" --arg preset "$sched_preset" \
  '{name: $name, cron: "0 3 * * *", preset_id: $preset, prompt: "smoke check of the schedule service: reply with ok", enabled: false}')
sched_resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$sched_body" "${SCHED_BASE}/schedules") ||
  fail "schedule: create failed (POST /schedules/api/v1/schedules)"
SCHEDULE_ID=$(echo "$sched_resp" | jq -er '.id') || fail "schedule: create response carries no id"
log "created schedule ${SLUG} (${SCHEDULE_ID}), disabled"

# 9c. The cron preview lists three fire times.
pv=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d '{"cron":"0 3 * * *"}' "${SCHED_BASE}/schedules/preview") ||
  fail "schedule: preview failed (POST /schedules/api/v1/schedules/preview)"
echo "$pv" | jq -e '.next_fires | length == 3' > /dev/null || fail "schedule: preview does not list 3 fire times: ${pv}"
log "schedule preview lists 3 fire times"

# 9d. Run now answers 202 and the run reaches in-progress with a session.
run_out=$(curl -sS --max-time 20 -X POST -w '\n%{http_code}' "${SCHED_BASE}/schedules/${SCHEDULE_ID}/run") ||
  fail "schedule: run request failed"
[ "${run_out##*$'\n'}" = "202" ] || fail "schedule: run answered ${run_out##*$'\n'}, expected 202: ${run_out%$'\n'*}"
SCHED_RUN_ID=$(echo "${run_out%$'\n'*}" | jq -er '.id') || fail "schedule: run response carries no id"
log "schedule run ${SCHED_RUN_ID} accepted (202)"
deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
run_status=""
while :; do
  run_json=$(curl -fsS --max-time 20 "${SCHED_BASE}/runs/${SCHED_RUN_ID}") || run_json=""
  run_status=$(echo "$run_json" | jq -r '.status // empty' 2> /dev/null || true)
  SCHED_SESSION_ID=$(echo "$run_json" | jq -r '.session_id // empty' 2> /dev/null || true)
  case "$run_status" in
    failed | ignored) fail "schedule: run ${SCHED_RUN_ID} ${run_status}: $(echo "$run_json" | jq -r '.reason // ""')" ;;
    in-progress | handled) [ -n "$SCHED_SESSION_ID" ] && break ;;
  esac
  if [ "$(date +%s)" -ge "$deadline" ]; then
    fail "schedule: run ${SCHED_RUN_ID} not in-progress with a session within ${SMOKE_TIMEOUT_S}s (status '${run_status}')"
  fi
  sleep 3
done
log "schedule run ${SCHED_RUN_ID} is ${run_status} with session ${SCHED_SESSION_ID}"

# 10. Workspace cache: a marker written by session A's init is restored for session B (restore runs before init).
cache_available=$(curl -fsS --max-time 20 "${BASE}/api/v1/meta" | jq -r '.cache.available // false') || fail "cache: GET /api/v1/meta failed"
[ "$cache_available" = "true" ] || fail "workspace cache is not configured (GET /api/v1/meta cache.available is '${cache_available}')"
log "meta reports cache.available"
RUN_ID=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')

# cache_create <suffix> <init script>: creates a 1-replica raw-spec session on the shared cache key; sets CACHE_NEW_ID.
cache_create() {
  local body resp
  body=$(jq -n --arg slug "${SLUG}-cache-$1" --arg key "$CACHE_KEY" --arg script "$2" \
    '{slug: $slug, spec: {replicas: 1, cache: {key: $key}, init_scripts: [$script]}}')
  resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$body" "${BASE}/api/v1/sessions") ||
    fail "cache: create of session ${SLUG}-cache-$1 failed"
  CACHE_NEW_ID=$(echo "$resp" | jq -er '.id') || fail "cache: create response carries no id"
  CACHE_SESSION_IDS="$CACHE_SESSION_IDS $CACHE_NEW_ID"
  log "created cache session ${SLUG}-cache-$1 (${CACHE_NEW_ID})"
}

# cache_wait <id> <restore states, space separated>: waits for the init to finish and the restore state to be one of the given
# states. A failed init fails the smoke (B's with the marker message); a failed restore fails it with the reason.
cache_wait() {
  local id=$1 want=$2 deadline view init_json rstate istate
  deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
  while :; do
    view=$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${id}") || view=""
    rstate=$(echo "$view" | jq -r '.status.cache.restore.state // empty' 2> /dev/null || true)
    init_json=$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${id}/init-scripts") || init_json=""
    istate=$(echo "$init_json" | jq -r '.status.state // empty' 2> /dev/null || true)
    if [ "$rstate" = "failed" ]; then
      fail "cache: restore of ${id} failed: $(echo "$view" | jq -r '.status.cache.restore.reason // ""')"
    fi
    if [ "$istate" = "failed" ]; then
      if [ "$want" = "restored" ]; then
        fail "workspace cache did not carry the marker: restore '${rstate}', init log tail: $(echo "$init_json" | jq -r '.status.log_tail // ""')"
      fi
      fail "cache: init of ${id} failed: $(echo "$init_json" | jq -r '.status.log_tail // ""')"
    fi
    if [ "$istate" = "succeeded" ]; then
      case " $want " in *" $rstate "*) return 0 ;; esac
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      fail "cache: ${id} not ready within ${SMOKE_TIMEOUT_S}s (restore '${rstate}', init '${istate}', want restore '${want}')"
    fi
    sleep 3
  done
}

# cache_discard <id>: deletes the session and waits for 404 (its save ran in the discard).
cache_discard() {
  local id=$1 deadline code
  curl -fsS --max-time 20 -X DELETE -o /dev/null "${BASE}/api/v1/sessions/${id}" || fail "cache: delete of ${id} failed"
  deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
  while :; do
    code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "${BASE}/api/v1/sessions/${id}") || code=""
    [ "$code" = "404" ] && break
    if [ "$(date +%s)" -ge "$deadline" ]; then
      fail "cache: session ${id} still answers '${code}' ${SMOKE_TIMEOUT_S}s after delete"
    fi
    sleep 3
  done
  CACHE_SESSION_IDS=$(echo "$CACHE_SESSION_IDS" | tr ' ' '\n' | grep -vxF "$id" | tr '\n' ' ' || true)
  log "cache session ${id} discarded (404)"
}

cache_create a "printf '%s\\n' \"${RUN_ID}\" > cache-marker"
cache_a=$CACHE_NEW_ID
cache_wait "$cache_a" "restored miss"
log "cache session A init wrote the marker; restore '$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${cache_a}" | jq -r '.status.cache.restore.state')'"
cache_discard "$cache_a"

cache_create b "grep -qx \"${RUN_ID}\" cache-marker"
cache_b=$CACHE_NEW_ID
cache_wait "$cache_b" "restored"
log "cache session B restored the workspace and found the marker"
cache_discard "$cache_b"

# 11. Session options: full Kubernetes access and cross-session messaging. Sessions are raw specs; the trap deletes them.
TS=${SLUG#smoke-}
SA_DIR=/var/run/secrets/kubernetes.io/serviceaccount

# option_create <slug> <spec json>: creates the session and sets OPT_NEW_ID.
option_create() {
  local body resp
  body=$(jq -cn --arg slug "$1" --argjson spec "$2" '{slug: $slug, spec: $spec}')
  resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$body" "${BASE}/api/v1/sessions") ||
    fail "$3: create of session $1 failed"
  OPT_NEW_ID=$(echo "$resp" | jq -er '.id') || fail "$3: create response of $1 carries no id"
  OPTION_SESSION_IDS="$OPTION_SESSION_IDS $OPT_NEW_ID"
  log "created session $1 (${OPT_NEW_ID})"
}

# option_pod <slug> <check name>: waits for the session's agent pod to exist and be Ready; sets OPT_POD.
option_pod() {
  local deadline remaining
  deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
  OPT_POD=""
  while [ -z "$OPT_POD" ]; do
    OPT_POD=$(kubectl -n "$NAMESPACE" get pods -l "agents.szymonrichert.pl/session=$1" -o name 2> /dev/null | grep -Fx "pod/agent-$1-0" || true)
    [ -n "$OPT_POD" ] && break
    [ "$(date +%s)" -ge "$deadline" ] && fail "$2: no agent pod of $1 within ${SMOKE_TIMEOUT_S}s"
    sleep 5
  done
  remaining=$((deadline - $(date +%s)))
  [ "$remaining" -lt 1 ] && remaining=1
  kubectl -n "$NAMESPACE" wait --for=condition=Ready "$OPT_POD" --timeout="${remaining}s" > /dev/null ||
    fail "$2: ${OPT_POD} not Ready within ${SMOKE_TIMEOUT_S}s"
  log "pod ${OPT_POD} is Ready"
}

# 10b. Extra plugin source: a session with the petstore example source at the pinned any-mcp tag; its pod lists petstore_ tools.
SRC_SLUG="smoke-src-${TS}"
SRC_SOURCE="git::https://github.com/szymonrychu/any-mcp//examples/petstore?ref=v${ANY_MCP_VERSION}"
option_create "$SRC_SLUG" "$(jq -cn --arg src "$SRC_SOURCE" '{replicas: 1, any_mcp_sources: [$src]}')" "extra source"
option_pod "$SRC_SLUG" "extra source"
src_tools=$(kubectl -n "$NAMESPACE" exec "$OPT_POD" -c ccw -- /opt/any-mcp/bin/any-mcp --config "$ANY_MCP_CFG" --list-tools 2> /dev/null) || src_tools=""
echo "$src_tools" | jq -e 'any(.[]?; (.name // "") | startswith("petstore_"))' > /dev/null 2>&1 || fail "extra source: any-mcp --list-tools in ${OPT_POD} lists no petstore_ tool"
log "extra source ${SRC_SOURCE} installed in ${OPT_POD}; --list-tools lists petstore_ tools"

# 11a. Kubernetes access. No kubectl in the pod: curl with the mounted token and CA; the token is read inside the pod and never printed.
kube_access_available=$(curl -fsS --max-time 20 "${BASE}/api/v1/meta" | jq -r '.kube_access.available // false') ||
  fail "kube access: GET /api/v1/meta failed"
[ "$kube_access_available" = "true" ] || fail "kube access: GET /api/v1/meta kube_access.available is '${kube_access_available}', expected true"
log "meta reports kube_access.available"
KUBE_SLUG="smoke-kube-${TS}"
option_create "$KUBE_SLUG" '{"replicas": 1, "kube_access": true}' "kube access"
option_pod "$KUBE_SLUG" "kube access"
kube_pod=$OPT_POD
kube_sa=$(kubectl -n "$NAMESPACE" get "$kube_pod" -o 'jsonpath={.spec.serviceAccountName}') || fail "kube access: cannot read the ServiceAccount of ${kube_pod}"
case "$kube_sa" in
  *-kube-access) ;;
  *) fail "kube access: ${kube_pod} runs as ServiceAccount '${kube_sa}', expected one ending -kube-access" ;;
esac
kube_mount=$(kubectl -n "$NAMESPACE" get "$kube_pod" -o 'jsonpath={.spec.automountServiceAccountToken}') ||
  fail "kube access: cannot read automountServiceAccountToken of ${kube_pod}"
[ "$kube_mount" = "true" ] || fail "kube access: ${kube_pod} automountServiceAccountToken is '${kube_mount}', expected true"
log "${kube_pod} runs as ${kube_sa} with its token mounted"
# shellcheck disable=SC2016  # runs in the pod's shell
kube_sh='tok=$(cat '"$SA_DIR"'/token) || exit 1
api="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}"
review=$(curl -sS --max-time 15 --cacert '"$SA_DIR"'/ca.crt -H "Authorization: Bearer $tok" -H "Content-Type: application/json" -d "{\"apiVersion\":\"authorization.k8s.io/v1\",\"kind\":\"SelfSubjectAccessReview\",\"spec\":{\"resourceAttributes\":{\"verb\":\"*\",\"group\":\"*\",\"resource\":\"*\"}}}" "$api/apis/authorization.k8s.io/v1/selfsubjectaccessreviews")
allowed=$(printf "%s" "$review" | tr -d " \n" | grep -o "\"allowed\":[a-z]*" | head -n1 | cut -d: -f2)
code=$(curl -sS --max-time 15 --cacert '"$SA_DIR"'/ca.crt -H "Authorization: Bearer $tok" -o /dev/null -w "%{http_code}" "$api/api/v1/namespaces")
echo "${allowed:-none} ${code}"'
kube_probe=$(kubectl -n "$NAMESPACE" exec "$kube_pod" -c ccw -- sh -c "$kube_sh" 2> /dev/null | tail -n1) || kube_probe=""
[ "${kube_probe%% *}" = "true" ] ||
  fail "kube access: SelfSubjectAccessReview for * on * from ${kube_pod} answered allowed '${kube_probe%% *}', expected true"
[ "${kube_probe#* }" = "200" ] || fail "kube access: GET /api/v1/namespaces from ${kube_pod} answered '${kube_probe#* }', expected 200"
log "full Kubernetes access from ${kube_pod}: SelfSubjectAccessReview for * on * allowed, GET /api/v1/namespaces 200"
hive_mount=$(kubectl -n "$NAMESPACE" get "$pod" -o 'jsonpath={.spec.automountServiceAccountToken}') ||
  fail "kube access: cannot read automountServiceAccountToken of the hive pod ${pod}"
[ "$hive_mount" = "false" ] || fail "kube access: hive pod ${pod} automountServiceAccountToken is '${hive_mount}', expected false"
kubectl -n "$NAMESPACE" exec "$pod" -- test ! -e "$SA_DIR/token" ||
  fail "kube access: hive pod ${pod} has a ServiceAccount token file at ${SA_DIR}/token"
log "hive pod ${pod} has no ServiceAccount token"

# 11b. Cross-session messaging (superleader model): A is a superleader, B is a plain session, the hive session is plain.
A_SLUG="smoke-msg-a-${TS}"
B_SLUG="smoke-msg-b-${TS}"
option_create "$A_SLUG" '{"replicas": 1, "session_messaging": true}' "messaging"
option_create "$B_SLUG" '{"replicas": 1}' "messaging"
B_ID=$OPT_NEW_ID
option_pod "$A_SLUG" "messaging"
a_pod=$OPT_POD
option_pod "$B_SLUG" "messaging"
b_pod=$OPT_POD

a_list=$(agent_call "$a_pod" GET /agent/v1/sessions)
[ "${a_list%% *}" = "200" ] || fail "messaging: GET /agent/v1/sessions from ${a_pod} answered '${a_list%% *}', expected 200"
echo "${a_list#* }" | jq -e --arg b "$B_SLUG" --arg hive "$SLUG" '[.sessions[].slug] | (index($b) != null) and (index($hive) != null)' > /dev/null ||
  fail "messaging: superleader ${A_SLUG} lists ${B_SLUG} and ${SLUG}: not both in $(echo "${a_list#* }" | jq -c '[.sessions[]?.slug]' 2> /dev/null)"
log "superleader ${A_SLUG} lists ${B_SLUG} and ${SLUG}"

ping_text="smoke ping ${TS}"
send_body=$(jq -cn --arg text "$ping_text" '{text: $text}')
sent=$(agent_call "$a_pod" POST "/agent/v1/sessions/${B_SLUG}/messages" "$send_body")
[ "${sent%% *}" = "202" ] || fail "messaging: send from ${A_SLUG} to ${B_SLUG} answered '${sent%% *}', expected 202: ${sent#* }"
msg_id=$(echo "${sent#* }" | jq -er '.message_id') || fail "messaging: the 202 send answer carries no message_id: ${sent#* }"
log "send from ${A_SLUG} to ${B_SLUG} accepted (202), message ${msg_id}"

deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
while :; do
  msg_status=$(curl -fsS --max-time 20 "${BASE}/api/v1/messages/${msg_id}" | jq -r '.status // empty' 2> /dev/null) || msg_status=""
  case "$msg_status" in
    sent) break ;;
    failed | cancelled) fail "messaging: message ${msg_id} is ${msg_status}, expected sent" ;;
  esac
  [ "$(date +%s)" -ge "$deadline" ] && fail "messaging: message ${msg_id} not sent within ${SMOKE_TIMEOUT_S}s (status '${msg_status}')"
  sleep 3
done
log "message ${msg_id} reached sent"

frame_prefix="<session-message from=\"${A_SLUG}\""
deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
while :; do
  b_events=$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${B_ID}/events?types=user_message&limit=200") || b_events=""
  echo "$b_events" | jq -e --arg prefix "$frame_prefix" 'any(.events[]?; .type == "user_message" and ((.data.text // "") | startswith($prefix)))' > /dev/null 2>&1 && break
  [ "$(date +%s)" -ge "$deadline" ] &&
    fail "messaging: ${B_SLUG} has no user_message starting ${frame_prefix} within ${SMOKE_TIMEOUT_S}s"
  sleep 3
done
log "${B_SLUG} received a user_message framed from ${A_SLUG}"

b_list=$(agent_call "$b_pod" GET /agent/v1/sessions)
[ "${b_list%% *}" = "200" ] || fail "messaging: GET /agent/v1/sessions from ${b_pod} answered '${b_list%% *}', expected 200"
echo "${b_list#* }" | jq -e --arg a "$A_SLUG" --arg hive "$SLUG" '[.sessions[].slug] | (index($a) != null) and (index($hive) == null)' > /dev/null ||
  fail "messaging: plain ${B_SLUG} must list ${A_SLUG} and not the hive session ${SLUG}: $(echo "${b_list#* }" | jq -c '[.sessions[]?.slug]' 2> /dev/null)"
log "plain ${B_SLUG} lists ${A_SLUG} and not the hive session"
denied=$(agent_call "$b_pod" POST "/agent/v1/sessions/${SLUG}/messages" "$send_body")
if [ "${denied%% *}" != "403" ] || ! echo "${denied#* }" | jq -e '.error.code == "target_not_superleader"' > /dev/null 2>&1; then
  fail "messaging: send from ${B_SLUG} to the hive session ${SLUG} answered '${denied%% *}', expected 403 target_not_superleader: ${denied#* }"
fi
log "send from ${B_SLUG} to the hive session refused: 403 target_not_superleader"

# 12. Session continuity: a session the smoke creates (smoke-cont-<ts>, deleted by the trap) has one real turn, then only its own pod is
# deleted. The pod must come back Ready with a new uid, the agent must leave (or never enter) `dead`, a second message must reach `sent`
# and the stored events after the restart must carry the same wrapper session id. Each wait has its own SMOKE_TIMEOUT_S window; message
# text, events and uids are matched, never printed.
CONT_SLUG="smoke-cont-${TS}"
option_create "$CONT_SLUG" '{"replicas": 1}' "continuity"
CONT_ID=$OPT_NEW_ID
option_pod "$CONT_SLUG" "continuity"
cont_pod=$OPT_POD
case "$cont_pod" in
  "pod/agent-smoke-cont-"*"-0") ;;
  *) fail "continuity: refusing to delete ${cont_pod}: not the pod of a smoke-cont session" ;;
esac

# cont_post <text>: queues a message on the continuity session; sets CONT_MSG_ID.
cont_post() {
  local resp
  resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$(jq -cn --arg text "$1" '{text: $text}')" \
    "${BASE}/api/v1/sessions/${CONT_ID}/messages") || fail "continuity: posting a message to ${CONT_SLUG} failed"
  CONT_MSG_ID=$(echo "$resp" | jq -er '.message_id') || fail "continuity: the message answer carries no message_id"
}

# cont_wait_sent <message id> <when>: waits for the message to reach sent.
cont_wait_sent() {
  local deadline status=""
  deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
  while :; do
    status=$(curl -fsS --max-time 20 "${BASE}/api/v1/messages/$1" | jq -r '.status // empty' 2> /dev/null) || status=""
    case "$status" in
      sent) break ;;
      failed | cancelled) fail "continuity: message $1 is ${status} $2, expected sent" ;;
    esac
    [ "$(date +%s)" -ge "$deadline" ] && fail "continuity: message $1 not sent within ${SMOKE_TIMEOUT_S}s $2 (status '${status}')"
    sleep 3
  done
}

# cont_agent <field>: the continuity agent's field in the session agents view.
cont_agent() {
  curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${CONT_ID}/agents" |
    jq -r --arg pod "${cont_pod#pod/}" --arg f "$1" '[.[] | select(.pod_name == $pod and (.gone | not))][0] | if . == null then empty else .[$f] | tostring end'
}

cont_events() { curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${CONT_ID}/events?agent_id=${cont_agent_id}$1&limit=200"; }

cont_post "smoke continuity: answer with the single word ok"
cont_wait_sent "$CONT_MSG_ID" "before the restart"
log "continuity: message ${CONT_MSG_ID} reached sent before the restart"

deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
cont_agent_id=""
while [ -z "$cont_agent_id" ]; do
  cont_agent_id=$(cont_agent id) || cont_agent_id=""
  [ -n "$cont_agent_id" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && fail "continuity: no agent for ${cont_pod} in the session agents list within ${SMOKE_TIMEOUT_S}s"
  sleep 3
done

# A transcript exists once the first turn completed; a restart in the middle of a turn would test a crash, not a restart.
deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
while :; do
  cont_events "&types=turn_completed" | jq -e '(.events | length) > 0' > /dev/null 2>&1 && break
  [ "$(date +%s)" -ge "$deadline" ] && fail "continuity: ${CONT_SLUG} has no completed turn within ${SMOKE_TIMEOUT_S}s"
  sleep 3
done
before=$(cont_events "") || fail "continuity: cannot read the events of ${cont_agent_id}"
before_wrapper=$(echo "$before" | jq -r '.events | last | .wrapper_session_id // empty')
before_last_id=$(echo "$before" | jq -r '[.events[].id] | max // empty')
[ -n "$before_wrapper" ] && [ -n "$before_last_id" ] || fail "continuity: ${cont_agent_id} has no stored events before the restart"
log "continuity: ${cont_agent_id} has wrapper session id ${before_wrapper} after one turn"

cont_uid() { kubectl -n "$NAMESPACE" get "$cont_pod" -o 'jsonpath={.metadata.uid}' 2> /dev/null || true; }
cont_uid0=$(cont_uid)
[ -n "$cont_uid0" ] || fail "continuity: cannot read the uid of ${cont_pod}"
kubectl -n "$NAMESPACE" delete "$cont_pod" --wait=false > /dev/null || fail "continuity: cannot delete ${cont_pod}"
log "continuity: deleted ${cont_pod}"

deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
while :; do
  cont_uid1=$(cont_uid)
  [ -n "$cont_uid1" ] && [ "$cont_uid1" != "$cont_uid0" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && fail "continuity: ${cont_pod} has no new uid within ${SMOKE_TIMEOUT_S}s"
  sleep 3
done
kubectl -n "$NAMESPACE" wait --for=condition=Ready "$cont_pod" --timeout="${SMOKE_TIMEOUT_S}s" > /dev/null ||
  fail "continuity: ${cont_pod} not Ready within ${SMOKE_TIMEOUT_S}s after the restart"
log "continuity: pod ${cont_pod#pod/} returned Ready with a new uid"

deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
while :; do
  cont_state=$(cont_agent state) || cont_state=""
  [ -n "$cont_state" ] && [ "$cont_state" != "dead" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && fail "continuity: agent ${cont_agent_id} still dead ${SMOKE_TIMEOUT_S}s after the pod restart (state '${cont_state}')"
  sleep 3
done
log "continuity: agent ${cont_agent_id} is not dead after the restart"

cont_post "smoke continuity: answer with the single word ok again"
cont_wait_sent "$CONT_MSG_ID" "after the restart"
log "continuity: message ${CONT_MSG_ID} reached sent after the restart"

deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
while :; do
  after=$(cont_events "&after_id=${before_last_id}") || after=""
  if echo "$after" | jq -e '(.events | length) > 0' > /dev/null 2>&1; then
    echo "$after" | jq -e --arg w "$before_wrapper" 'all(.events[]; .wrapper_session_id == $w)' > /dev/null ||
      fail "continuity: wrapper session id changed after the restart (before ${before_wrapper}, after $(echo "$after" | jq -r '[.events[].wrapper_session_id] | unique | join(",")'))"
    break
  fi
  [ "$(date +%s)" -ge "$deadline" ] && fail "continuity: no event from the agent after the restart within ${SMOKE_TIMEOUT_S}s"
  sleep 3
done
log "continuity: wrapper session id ${before_wrapper} unchanged after the restart"

log "OK"

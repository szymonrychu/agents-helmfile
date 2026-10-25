#!/usr/bin/env bash
# End-to-end smoke of a deployed agents-orchestrator and its UI: the UI (agents-web release) answers with the agents-web index and a
# config.json listing the orchestrator backend, the host routes / to agents-web and /api to the orchestrator, a 1-replica hive session (cap 2)
# with an init script yields a Ready pod, a succeeded init and a leader, that pod's `claude mcp list` shows any-mcp,
# it reaches /agent/v1/me through the agents Service with 200 (proves the pod source IP survives),
# the view echoes the wrapper, lease and autoclose settings with closes_at = created_at + 3600s and a leader_since,
# and the public scale route grows the session to 2 pods and back to 1 without restarting pod-0.
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
# Prints no secrets: only status lines, pod events and the mcp list.
set -euo pipefail

# The any-mcp the sessions install: the orchestrator release's pinned ORCH_ANY_MCP_REF, leading v stripped.
ORCH_VALUES="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/values/agents-orchestrator/common.yaml"
ANY_MCP_VERSION=$(sed -n 's/^[[:space:]]*ORCH_ANY_MCP_REF:[[:space:]]*v\{0,1\}\([^[:space:]#]*\).*/\1/p' "$ORCH_VALUES" 2> /dev/null | head -n1)
if [ -z "$ANY_MCP_VERSION" ]; then
  echo "smoke: FAIL: no ORCH_ANY_MCP_REF in ${ORCH_VALUES}" >&2
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
PUBLIC_URL="${SMOKE_PUBLIC_URL:-https://lab.szymonrichert.pl}"

log() { echo "smoke: $*"; }
fail() {
  echo "smoke: FAIL: $*" >&2
  exit 1
}

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
  if [ -n "$ENDPOINT_ID" ]; then
    if curl -fsS --max-time 20 -X DELETE -o /dev/null "${WH_API_BASE}/webhooks/api/v1/endpoints/${ENDPOINT_ID}"; then
      log "deleted webhook endpoint ${ENDPOINT_ID}"
    else
      echo "smoke: cleanup failed, webhook API unreachable; delete webhook endpoint id ${ENDPOINT_ID} manually" >&2
    fi
  fi
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

# 7. Scale 1 -> 2 -> 1 through the public route; pod-0 must never restart.
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

deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
scale 1
while [ -n "$(pod_names | grep -vx "$pod" || true)" ]; do
  [ "$(date +%s)" -ge "$deadline" ] && fail "scale down: ${pod1} still present after ${SMOKE_TIMEOUT_S}s"
  sleep 3
done
log "scaled down: ${pod1} is gone"

uid_now=$(kubectl -n "$NAMESPACE" get "$pod" -o 'jsonpath={.metadata.uid}') || fail "${pod} is gone after scaling"
[ "$uid_now" = "$uid0" ] || fail "pod-0 uid changed after scaling (${uid0} -> ${uid_now}): pod-0 restarted"
log "pod-0 kept uid ${uid0}"
log "OK"

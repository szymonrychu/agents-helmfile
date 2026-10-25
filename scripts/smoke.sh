#!/usr/bin/env bash
# End-to-end smoke of a deployed agent-orchestrator: UI answers, a 2-replica
# cluster (a hive) yields a Ready pod and a leader, that pod's `claude mcp list` shows any-mcp,
# and it reaches /agent/v1/me through the agents Service with 200 (proves the pod source IP survives).
# Parameters by env: HOST, RELEASE, NAMESPACE, SMOKE_TIMEOUT_S, SMOKE_LOCAL_PORT (SCHEME for tests only).
# Without HOST it port-forwards to the api: the public host is behind SSO, and the api's
# source allowlist admits only 127.0.0.1 (the oauth2-proxy sidecar, or a port-forward).
# Prints no secrets: only status lines, pod events and the mcp list.
set -euo pipefail

NAMESPACE="${NAMESPACE:-agents}"
SMOKE_TIMEOUT_S="${SMOKE_TIMEOUT_S:-600}"
PF_PID=""
if [ -z "${HOST:-}" ]; then
  HOST="127.0.0.1:${SMOKE_LOCAL_PORT:-18000}"
  SCHEME=http
  kubectl -n "$NAMESPACE" port-forward deploy/agent-orchestrator-api "${HOST#*:}:8000" > /dev/null 2>&1 &
  PF_PID=$!
fi
BASE="${SCHEME:-https}://${HOST}"
SLUG="smoke-$(date +%s)"
CLUSTER_ID=""

log() { echo "smoke: $*"; }
fail() {
  echo "smoke: FAIL: $*" >&2
  exit 1
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [ -n "$CLUSTER_ID" ]; then
    if curl -fsS --max-time 20 -X DELETE -o /dev/null "${BASE}/api/v1/clusters/${CLUSTER_ID}"; then
      log "discarded cluster ${SLUG} (${CLUSTER_ID})"
    else
      echo "smoke: cleanup failed, API unreachable; delete cluster '${SLUG}' (id ${CLUSTER_ID}) manually" >&2
    fi
  fi
  [ -n "$PF_PID" ] && kill "$PF_PID" 2> /dev/null
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

# 1. UI and API contract document answer.
page=$(curl -fsS --max-time 20 "${BASE}/") || fail "UI ${BASE}/ not reachable"
case "$page" in
  *"<"*">"*) log "UI answers with HTML" ;;
  *) fail "UI body is not HTML" ;;
esac
curl -fsS --max-time 20 -o /dev/null "${BASE}/api/v1/openapi.json" || fail "openapi.json not reachable"
log "openapi.json answers"

# 1b. The agents Service (leader-to-agent traffic) exists on 8001 and is not an ingress backend.
AGENTS_SVC="${RELEASE:-agent-orchestrator}-agents"
agents_port=$(kubectl -n "$NAMESPACE" get service "$AGENTS_SVC" -o 'jsonpath={.spec.ports[0].port}' 2> /dev/null) ||
  fail "Service ${AGENTS_SVC} not found in ${NAMESPACE}"
[ "$agents_port" = "8001" ] || fail "Service ${AGENTS_SVC} port is ${agents_port}, expected 8001"
backends=$(kubectl -n "$NAMESPACE" get ingress -o 'jsonpath={.items[*].spec.rules[*].http.paths[*].backend.service.name}') ||
  fail "cannot list ingresses in ${NAMESPACE}"
case " ${backends} " in
  *" ${AGENTS_SVC} "*) fail "Service ${AGENTS_SVC} is an ingress backend" ;;
esac
log "Service ${AGENTS_SVC} on 8001, not an ingress backend"

# 2. Create a 2-replica cluster (a hive: the smoke makes its own, never touches existing ones) through the public API.
body=$(jq -n --arg slug "$SLUG" '{slug: $slug, spec: {replicas: 2}}')
resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$body" "${BASE}/api/v1/clusters") ||
  fail "cluster create failed"
CLUSTER_ID=$(echo "$resp" | jq -er '.id') || fail "create response carries no id"
log "created cluster ${SLUG} (${CLUSTER_ID})"

# 3. Wait for the agent pod to exist and be Ready.
label="agents.szymonrichert.pl/cluster=${SLUG}"
deadline=$(($(date +%s) + SMOKE_TIMEOUT_S))
pod=""
while [ -z "$pod" ]; do
  pod=$(kubectl -n "$NAMESPACE" get pods -l "$label" -o name 2> /dev/null | head -n1 || true)
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
  *0.2.0*) log "any-mcp version 0.2.0" ;;
  *) fail "any-mcp --version is not 0.2.0: ${ver}" ;;
esac

# 5. The hive has a leader (the dispatcher elects the lowest-ordinal ready agent).
leader=""
while [ -z "$leader" ]; do
  leader=$(curl -fsS --max-time 20 "${BASE}/api/v1/clusters/${CLUSTER_ID}" | jq -r '.leader_agent_id // empty') || leader=""
  [ -n "$leader" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    fail "cluster ${SLUG} has no leader within ${SMOKE_TIMEOUT_S}s"
  fi
  sleep 3
done
log "cluster ${SLUG} leader ${leader}"

# 6. From the agent pod, with its own env, GET /agent/v1/me on the agents Service: 200 only if
# the token, the cluster and the pod-IP binding all hold (the CNI kept the source IP).
me_py='import os, urllib.request, urllib.error
req = urllib.request.Request(os.environ["ORCH_AGENT_URL"].rstrip("/") + "/agent/v1/me", headers={"Authorization": "Bearer " + os.environ["ORCH_AGENT_TOKEN"], "X-Orch-Cluster": os.environ["ORCH_CLUSTER_ID"]})
try:
    print(urllib.request.urlopen(req, timeout=15).status)
except urllib.error.HTTPError as e:
    print(e.code)'
me_status=$(kubectl -n "$NAMESPACE" exec "$pod" -- python3 -c "$me_py" 2>&1 | tail -n1) || true
[ "$me_status" = "200" ] || fail "GET /agent/v1/me from ${pod} (via ORCH_AGENT_URL, ORCH_AGENT_TOKEN, ORCH_CLUSTER_ID) answered '${me_status}', expected 200"
log "GET /agent/v1/me from ${pod} through the agents Service: 200"
log "OK"

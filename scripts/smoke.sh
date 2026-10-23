#!/usr/bin/env bash
# End-to-end smoke of a deployed agent-orchestrator: UI answers, a 1-replica
# cluster yields a Ready pod, and that pod's `claude mcp list` shows any-mcp.
# Parameters by env: HOST, NAMESPACE, SMOKE_TIMEOUT_S (SCHEME for tests only).
# Prints no secrets: only status lines, pod events and the mcp list.
set -euo pipefail

HOST="${HOST:-agents.szymonrichert.pl}"
NAMESPACE="${NAMESPACE:-agents}"
SMOKE_TIMEOUT_S="${SMOKE_TIMEOUT_S:-600}"
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
  exit "$rc"
}
trap cleanup EXIT

# 1. UI and API contract document answer.
page=$(curl -fsS --max-time 20 "${BASE}/") || fail "UI ${BASE}/ not reachable"
case "$page" in
  *"<"*">"*) log "UI answers with HTML" ;;
  *) fail "UI body is not HTML" ;;
esac
curl -fsS --max-time 20 -o /dev/null "${BASE}/api/v1/openapi.json" || fail "openapi.json not reachable"
log "openapi.json answers"

# 2. Create a 1-replica cluster through the public API.
body=$(jq -n --arg slug "$SLUG" '{slug: $slug, spec: {replicas: 1}}')
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
  *0.1.0*) log "any-mcp version 0.1.0" ;;
  *) fail "any-mcp --version is not 0.1.0: ${ver}" ;;
esac
log "OK"

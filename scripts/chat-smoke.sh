#!/usr/bin/env bash
# Live chat smoke S1-S4 in the owner DM (design D17). It posts REAL messages to the owner's Messenger chat from the owner's Facebook
# account, so it is its own script and task (`mise run chat-smoke`) and never part of scripts/smoke.sh.
#
# Caller (1:1 room-session binding, D9; gate question Q11): the owner DM's `binding` is read from the connector UI API
# (GET /matrix/api/v1/rooms/{room_id}, port-forward to the api container, bypassing its SSO sidecar).
# - Bound: S1-S4 run from the leader pod of the bound session (hive: the pod of leader_agent_id in GET /api/v1/sessions/{id}/agents;
#   alone: its only agent), with that pod's own ORCH_AGENT_* env (never printed). That session gets no message and no change; nothing
#   is created or deleted.
# - Unbound: a throwaway alone session chat-smoke-<ts> is created from the spec of CHAT_SMOKE_PRESET (preset id or name; default: the
#   default preset) with replicas 1 and max_replicas 1; its S1 initiates in the DM and binds it. It is deleted at the end (and by the
#   trap on failure or interrupt), then the script waits, with its own deadline, until the connector releases the binding.
# A binding without a session (a first run in progress reserving the room) stops the script: no caller may post there.
#
# From the caller pod, through the orchestrator agent routes (/agent/v1/chat/*, the relay to the connector's agent listener):
# the rooms list must name the room, then S1 a message with no inbound message in this run, S2 a reply to S1's s<n>, S3 a thumbs-up
# reaction to S1's s<n>, S4 a second, different reaction to the same target. Each send's status is polled to sent, failed or refused,
# each wait with its own CHAT_SMOKE_TIMEOUT_S deadline (a timeout exits 1 naming the step). S1-S3 must end sent; S4 is printed with its
# status and reason (or the api refusal) and never fails the script. Before S1 it prints the hours since the room's newest inbound
# message (Q8: S1 runs regardless; the over-24h case is confirmed at the first natural opportunity).
#
# Output lines: `binding before: <slug|none>`, `hours since last inbound: <n|none>`, `caller: bound <slug>` or
# `caller: throwaway <slug>`, `S1 s<n> sent`, `S2 s<n> sent`, `S3 s<n> sent`, `S4 s<n> <status> [<reason>]` or
# `S4 - refused <http status> <error code>`, `binding during: <slug>` (throwaway only), `binding after: <slug|none>`.
#
# Env: NAMESPACE (agents), RELEASE (agents-orchestrator), MATRIX_RELEASE (agents-matrix-connector), CHAT_SMOKE_ROOM
# (!iLWrAwaTMYnINJsyNv:szymonrichert.pl), CHAT_SMOKE_PRESET, CHAT_SMOKE_TIMEOUT_S (600, per wait), SMOKE_LOCAL_PORT (18000, the
# orchestrator api port-forward), SMOKE_MATRIX_API_LOCAL_PORT (18004, the connector api port-forward); CHAT_SMOKE_PROC_DIR for tests only.
# It owns its port-forwards: a stale kubectl port-forward on either local port is killed first, and the trap kills its own on every exit.
set -euo pipefail

NAMESPACE="${NAMESPACE:-agents}"
RELEASE="${RELEASE:-agents-orchestrator}"
MATRIX_RELEASE="${MATRIX_RELEASE:-agents-matrix-connector}"
ROOM="${CHAT_SMOKE_ROOM:-!iLWrAwaTMYnINJsyNv:szymonrichert.pl}"
PRESET="${CHAT_SMOKE_PRESET:-}"
TIMEOUT_S="${CHAT_SMOKE_TIMEOUT_S:-600}"
ORCH_PORT="${SMOKE_LOCAL_PORT:-18000}"
MX_PORT="${SMOKE_MATRIX_API_LOCAL_PORT:-18004}"
PROC_DIR="${CHAT_SMOKE_PROC_DIR:-/proc}"
POLL_S=2
BASE="http://127.0.0.1:${ORCH_PORT}"
MX="http://127.0.0.1:${MX_PORT}/matrix/api/v1"
ROOM_ENC=$(jq -rn --arg r "$ROOM" '$r | @uri')
TS=$(date +%s)
PF_PIDS=""
THROWAWAY_ID=""
THROWAWAY_SLUG=""
CALLER_POD=""

log() { echo "chat-smoke: $*"; }
fail() {
  echo "chat-smoke: FAIL: $*" >&2
  exit 1
}

# delete_throwaway: deletes the session this script created, if any (never another one).
delete_throwaway() {
  [ -n "$THROWAWAY_ID" ] || return 0
  if curl -fsS --max-time 20 -X DELETE -o /dev/null "${BASE}/api/v1/sessions/${THROWAWAY_ID}"; then
    log "deleted throwaway session ${THROWAWAY_SLUG} (${THROWAWAY_ID})"
    THROWAWAY_ID=""
    return 0
  fi
  echo "chat-smoke: delete failed; delete throwaway session '${THROWAWAY_SLUG}' (id ${THROWAWAY_ID}) manually" >&2
  return 1
}

cleanup() {
  local rc=$? pid
  trap - EXIT INT TERM
  delete_throwaway || rc=1
  for pid in $PF_PIDS; do
    kill "$pid" 2> /dev/null || true
    wait "$pid" 2> /dev/null || true
  done
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

now() { date +%s; }

# stale_pids <port>: pids of kubectl port-forward processes holding the local port (from /proc, else `ps`).
stale_pids() {
  local port=$1 re d pid args
  re="(^|[[:space:]/])kubectl[[:space:]](.*[[:space:]])?port-forward[[:space:]](.*[[:space:]])?${port}:[0-9]"
  if [ -d "$PROC_DIR" ]; then
    for d in "$PROC_DIR"/[0-9]*; do
      pid=${d##*/}
      [ "$pid" = "$$" ] && continue
      args=$(tr '\0' ' ' < "$d/cmdline" 2> /dev/null) || continue
      if [[ $args =~ $re ]]; then echo "$pid"; fi
    done
  else
    ps -eo pid=,args= | while read -r pid args; do
      if [[ $args =~ $re ]]; then echo "$pid"; fi
    done
  fi
}

kill_stale_port_forward() {
  local port=$1 pid deadline
  for pid in $(stale_pids "$port"); do
    kill "$pid" 2> /dev/null || true
    log "killed stale port-forward ${pid} on port ${port}"
  done
  deadline=$(($(now) + 5))
  while [ -n "$(stale_pids "$port")" ]; do
    [ "$(now)" -ge "$deadline" ] && fail "a stale port-forward still holds port ${port}"
    sleep 0.2
  done
}

wait_up() { # <url> <what>
  local _
  for _ in $(seq 1 20); do
    curl -s --max-time 2 -o /dev/null "$1" && return 0
    sleep 1
  done
  fail "port-forward to $2 not answering on $1"
}

room_json() { curl -fsS --max-time 20 "${MX}/rooms/${ROOM_ENC}"; }
binding_name() { echo "$1" | jq -r 'if .binding == null then "none" else (.binding.slug // .binding.session_id // "reserved") end'; }

# last_inbound_at: the `at` of the room's newest inbound message (pages through the merged feed), empty when none.
last_inbound_at() {
  local before="" page at
  while :; do
    page=$(curl -fsS --max-time 20 "${MX}/rooms/${ROOM_ENC}/messages?limit=200${before:+&before_id=${before}}") || return 1
    at=$(echo "$page" | jq -r '[.[] | select(.direction == "in")][0].at // empty')
    if [ -n "$at" ]; then
      echo "$at"
      return 0
    fi
    [ "$(echo "$page" | jq 'length')" -lt 200 ] && return 0
    before=$(echo "$page" | jq -r 'last.id')
  done
}

# hours_since <iso timestamp>: whole hours from then to now (Z or a +hh:mm/-hh:mm offset, fraction ignored).
hours_since() {
  jq -rn --arg at "$1" --argjson now "$(now)" '
    ($at | capture("^(?<d>[0-9-]+T[0-9:]+)(\\.[0-9]+)?(?<z>Z|[+-][0-9]{2}:[0-9]{2})$")) as $p
    | (if $p.z == "Z" then 0 else (($p.z[1:3] | tonumber) * 3600 + ($p.z[4:6] | tonumber) * 60)
        * (if $p.z[0:1] == "-" then -1 else 1 end) end) as $off
    | (($p.d + "Z") | fromdateiso8601) - $off
    | (($now - .) / 3600) | floor'
}

# resolve_leader_pod <session id> <slug>: waits (own deadline) for the session's leader agent pod (hive: leader_agent_id; alone: its
# only live agent), present under the session label and Ready; sets CALLER_POD.
resolve_leader_pod() {
  local sid=$1 slug=$2 deadline view agents pod_name pods remaining
  deadline=$(($(now) + TIMEOUT_S))
  CALLER_POD=""
  while :; do
    view=$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${sid}") || fail "caller: session ${slug} (${sid}) is not readable (gone?)"
    [ "$(echo "$view" | jq -r '.desired')" = "deleting" ] && fail "caller: session ${slug} (${sid}) is being deleted"
    agents=$(curl -fsS --max-time 20 "${BASE}/api/v1/sessions/${sid}/agents") || agents="[]"
    pod_name=$(jq -rn --argjson v "$view" --argjson a "$agents" '
      [$a[] | select((.gone | not) and .ready == true)] as $live
      | if $v.hive then ([$live[] | select(.id == $v.leader_agent_id)][0].pod_name // empty)
        elif ($live | length) == 1 then $live[0].pod_name
        else empty end')
    if [ -n "$pod_name" ]; then
      pods=$(kubectl -n "$NAMESPACE" get pods -l "agents.szymonrichert.pl/session=${slug}" -o name 2> /dev/null || true)
      if grep -Fxq "pod/${pod_name}" <<< "$pods"; then
        CALLER_POD="pod/${pod_name}"
        break
      fi
    fi
    [ "$(now)" -ge "$deadline" ] && fail "caller: no ready leader pod of session ${slug} within ${TIMEOUT_S}s"
    sleep "$POLL_S"
  done
  remaining=$((deadline - $(now)))
  [ "$remaining" -lt 1 ] && remaining=1
  kubectl -n "$NAMESPACE" wait --for=condition=Ready "$CALLER_POD" --timeout="${remaining}s" > /dev/null ||
    fail "caller: ${CALLER_POD} not Ready within ${TIMEOUT_S}s"
}

# agent_call <method> <path> [json body]: from inside the caller pod with its own ORCH_AGENT_URL, ORCH_AGENT_TOKEN and ORCH_SESSION_ID
# (never echoed; the pod's stderr is dropped); prints "<status> <body>".
agent_py='import os, sys, urllib.request, urllib.error
method, path = sys.argv[1], sys.argv[2]
data = sys.argv[3].encode() if len(sys.argv) > 3 else None
headers = {"Authorization": "Bearer " + os.environ["ORCH_AGENT_TOKEN"], "X-Orch-Session": os.environ["ORCH_SESSION_ID"]}
if data:
    headers["Content-Type"] = "application/json"
req = urllib.request.Request(os.environ["ORCH_AGENT_URL"].rstrip("/") + path, data=data, method=method, headers=headers)
try:
    resp = urllib.request.urlopen(req, timeout=15)
    status, body = resp.status, resp.read()
except urllib.error.HTTPError as e:
    status, body = e.code, e.read()
print(status, " ".join(body.decode().split()))'
agent_call() {
  kubectl -n "$NAMESPACE" exec "$CALLER_POD" -c ccw -- python3 -c "$agent_py" "$@" 2> /dev/null | tail -n1 || true
}

# send <step> <path> <json body>: 202 sets SEND_ID; any other answer sets SEND_REFUSAL ("<http status> <error code>") and returns 1.
send() {
  local out code
  out=$(agent_call POST "$2" "$3")
  code=${out%% *}
  if [ "$code" = "202" ]; then
    SEND_ID=$(echo "${out#* }" | jq -er '.id') || fail "$1: the 202 answer carries no id: ${out#* }"
    log "$1 ${SEND_ID} accepted ($(echo "${out#* }" | jq -r '.status'))"
    return 0
  fi
  SEND_ID=""
  SEND_REFUSAL="${code:-none} $(echo "${out#* }" | jq -r '.error.code // "unknown"' 2> /dev/null || echo unknown)"
  return 1
}

# wait_settled <step> <send id>: polls the send's status to sent, failed or refused with its own deadline; sets SETTLED_STATUS and
# SETTLED_REASON.
wait_settled() {
  local deadline out status=""
  deadline=$(($(now) + TIMEOUT_S))
  while :; do
    out=$(agent_call GET "/agent/v1/chat/messages/$2")
    if [ "${out%% *}" = "200" ]; then
      status=$(echo "${out#* }" | jq -r '.status // empty')
      case "$status" in
        sent | failed | refused)
          SETTLED_STATUS=$status
          SETTLED_REASON=$(echo "${out#* }" | jq -r '.reason // empty')
          return 0
          ;;
      esac
    else
      status="http ${out%% *}"
    fi
    [ "$(now)" -ge "$deadline" ] && fail "$1 $2 not settled within ${TIMEOUT_S}s (status '${status}')"
    sleep "$POLL_S"
  done
}

# step_sent <step> <path> <json body>: a send that must end sent (S1-S3); sets SEND_ID.
step_sent() {
  send "$1" "$2" "$3" || fail "$1 refused at the api: ${SEND_REFUSAL}"
  wait_settled "$1" "$SEND_ID"
  [ "$SETTLED_STATUS" = "sent" ] || fail "$1 ${SEND_ID} ${SETTLED_STATUS} ${SETTLED_REASON}"
  echo "$1 ${SEND_ID} sent"
}

# 0. Own port-forwards: kill stale ones on both local ports first, then the orchestrator api and the connector api containers.
for port in "$ORCH_PORT" "$MX_PORT"; do kill_stale_port_forward "$port"; done
kubectl -n "$NAMESPACE" port-forward "deploy/${RELEASE}-api" "${ORCH_PORT}:8000" > /dev/null 2>&1 &
PF_PIDS="$PF_PIDS $!"
kubectl -n "$NAMESPACE" port-forward "deploy/${MATRIX_RELEASE}-api" "${MX_PORT}:8000" > /dev/null 2>&1 &
PF_PIDS="$PF_PIDS $!"
wait_up "${BASE}/healthz" "deploy/${RELEASE}-api"
wait_up "http://127.0.0.1:${MX_PORT}/healthz" "deploy/${MATRIX_RELEASE}-api"

# 1. The room's binding before the run, and the hours since its newest inbound message.
room=$(room_json) || fail "binding: GET ${MX}/rooms/${ROOM_ENC} failed (room ${ROOM} unknown to the connector?)"
echo "binding before: $(binding_name "$room")"
if echo "$room" | jq -e '.binding != null and .binding.session_id == null' > /dev/null; then
  fail "binding: room ${ROOM} is reserved by a first run in progress (binding without a session); no caller may post there now"
fi
BOUND_ID=$(echo "$room" | jq -r '.binding.session_id // empty')
BOUND_SLUG=$(echo "$room" | jq -r '.binding.slug // .binding.session_id // empty')

inbound_at=$(last_inbound_at) || fail "inbound: GET ${MX}/rooms/${ROOM_ENC}/messages failed"
if [ -n "$inbound_at" ]; then
  echo "hours since last inbound: $(hours_since "$inbound_at")"
else
  echo "hours since last inbound: none"
fi

# 2. The caller: the bound session's leader pod, or a throwaway alone session.
if [ -n "$BOUND_ID" ]; then
  echo "caller: bound ${BOUND_SLUG}"
  resolve_leader_pod "$BOUND_ID" "$BOUND_SLUG"
else
  presets=$(curl -fsS --max-time 20 "${BASE}/api/v1/presets") || fail "caller: GET /api/v1/presets failed"
  if [ -n "$PRESET" ]; then
    spec=$(echo "$presets" | jq -ce --arg p "$PRESET" '[.[] | select(.id == $p or .name == $p)][0].spec') ||
      fail "caller: no preset ${PRESET} in GET /api/v1/presets"
  else
    spec=$(echo "$presets" | jq -ce '[.[] | select(.is_default)][0].spec') || fail "caller: no default preset in GET /api/v1/presets"
  fi
  THROWAWAY_SLUG="chat-smoke-${TS}"
  body=$(jq -cn --arg slug "$THROWAWAY_SLUG" --argjson spec "$spec" '{slug: $slug, spec: ($spec + {replicas: 1, max_replicas: 1})}')
  resp=$(curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' -d "$body" "${BASE}/api/v1/sessions") ||
    fail "caller: create of throwaway session ${THROWAWAY_SLUG} failed"
  THROWAWAY_ID=$(echo "$resp" | jq -er '.id') || fail "caller: the create answer of ${THROWAWAY_SLUG} carries no id"
  log "created throwaway session ${THROWAWAY_SLUG} (${THROWAWAY_ID})"
  echo "caller: throwaway ${THROWAWAY_SLUG}"
  resolve_leader_pod "$THROWAWAY_ID" "$THROWAWAY_SLUG"
fi
log "caller pod ${CALLER_POD}"

# 3. The room must be listed for the caller; nothing is sent otherwise.
rooms=$(agent_call GET /agent/v1/chat/rooms)
[ "${rooms%% *}" = "200" ] || fail "rooms: GET /agent/v1/chat/rooms from ${CALLER_POD} answered '${rooms%% *}': ${rooms#* }"
echo "${rooms#* }" | jq -e --arg r "$ROOM" 'any(.rooms[]?; .room_id == $r)' > /dev/null ||
  fail "rooms: room ${ROOM} is not listed for ${CALLER_POD} (listed: $(echo "${rooms#* }" | jq -c '[.rooms[]?.room_id]'))"
log "rooms: ${ROOM} listed for ${CALLER_POD}"

# 4. S1-S4.
step_sent S1 /agent/v1/chat/messages \
  "$(jq -acn --arg room "$ROOM" --arg text "chat-smoke ${TS} S1: a message sent with no inbound message in this run" '{text: $text, room: $room}')"
S1_ID=$SEND_ID
if [ -n "$THROWAWAY_ID" ]; then
  room=$(room_json) || fail "binding: GET ${MX}/rooms/${ROOM_ENC} failed after S1"
  [ "$(echo "$room" | jq -r '.binding.session_id // empty')" = "$THROWAWAY_ID" ] ||
    fail "binding: S1 did not bind ${ROOM} to ${THROWAWAY_SLUG} (binding: $(binding_name "$room"))"
  echo "binding during: $(binding_name "$room")"
fi
step_sent S2 /agent/v1/chat/messages \
  "$(jq -acn --arg room "$ROOM" --arg to "$S1_ID" --arg text "chat-smoke ${TS} S2: a quoted reply to S1" '{text: $text, room: $room, reply_to: $to}')"
step_sent S3 /agent/v1/chat/reactions "$(jq -acn --arg to "$S1_ID" '{to: $to, key: "\ud83d\udc4d"}')"
if send S4 /agent/v1/chat/reactions "$(jq -acn --arg to "$S1_ID" '{to: $to, key: "\ud83c\udf89"}')"; then
  wait_settled S4 "$SEND_ID"
  echo "S4 ${SEND_ID} ${SETTLED_STATUS}${SETTLED_REASON:+ ${SETTLED_REASON}}"
else
  echo "S4 - refused ${SEND_REFUSAL}"
fi

# 5. The throwaway session goes, and its binding with it (own deadline); the binding after the run.
if [ -n "$THROWAWAY_ID" ]; then
  released_id=$THROWAWAY_ID
  delete_throwaway || fail "release: could not delete throwaway session ${THROWAWAY_SLUG}"
  deadline=$(($(now) + TIMEOUT_S))
  while :; do
    room=$(room_json) || room=""
    if [ -n "$room" ] && [ "$(echo "$room" | jq -r '.binding.session_id // empty')" != "$released_id" ]; then break; fi
    [ "$(now)" -ge "$deadline" ] &&
      fail "release: ${ROOM} still bound to ${THROWAWAY_SLUG} ${TIMEOUT_S}s after its delete"
    sleep "$POLL_S"
  done
  log "release: ${ROOM} no longer bound to ${THROWAWAY_SLUG}"
else
  room=$(room_json) || fail "binding: GET ${MX}/rooms/${ROOM_ENC} failed after S4"
fi
echo "binding after: $(binding_name "$room")"
log "OK"

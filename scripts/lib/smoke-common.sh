# shellcheck shell=bash
# Helpers shared by scripts/smoke.sh and scripts/chat-smoke.sh; sourced, never run. The sourcing script sets SMOKE_LOG_PREFIX (the
# word printed before each log and failure line), NAMESPACE, and for kill_stale_port_forward optionally PROC_DIR (default /proc; tests
# point it at a fake tree).

log() { echo "${SMOKE_LOG_PREFIX}: $*"; }
fail() {
  echo "${SMOKE_LOG_PREFIX}: FAIL: $*" >&2
  exit 1
}

now() { date +%s; }

# agent_call <pod> <method> <path> [json body]: from inside the pod with its own ORCH_AGENT_URL, ORCH_AGENT_TOKEN and ORCH_SESSION_ID
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
  local pod_name=$1
  shift
  kubectl -n "$NAMESPACE" exec "$pod_name" -c ccw -- python3 -c "$agent_py" "$@" 2> /dev/null | tail -n1 || true
}

# stale_pids <port>: pids of kubectl port-forward processes holding the local port (from /proc, else `ps`). A process that exits during
# the scan has no readable cmdline and is skipped silently (the stderr redirect comes first so the failed open is silenced too).
stale_pids() {
  local port=$1 re d pid args proc_dir=${PROC_DIR:-/proc}
  re="(^|[[:space:]/])kubectl[[:space:]](.*[[:space:]])?port-forward[[:space:]](.*[[:space:]])?${port}:[0-9]"
  if [ -d "$proc_dir" ]; then
    for d in "$proc_dir"/[0-9]*; do
      pid=${d##*/}
      [ "$pid" = "$$" ] && continue
      args=$(tr '\0' ' ' 2> /dev/null < "$d/cmdline") || continue
      if [[ $args =~ $re ]]; then echo "$pid"; fi
    done
  else
    ps -eo pid=,args= | while read -r pid args; do
      if [[ $args =~ $re ]]; then echo "$pid"; fi
    done
  fi
}

# kill_stale_port_forward <port>: kills stale kubectl port-forwards on the local port and waits (up to 5s) until none is left.
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

"""scripts/smoke.sh offline: fake HTTP API and a fake kubectl on PATH."""

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "smoke.sh"
ORCH_VALUES = ROOT / "values" / "agents-orchestrator" / "common.yaml"
# The any-mcp the sessions install: the orchestrator release's pinned tag without its leading v.
ANY_MCP_VERSION = re.search(
    r"^\s*ORCH_ANY_MCP_REF:\s*v(\S+)", ORCH_VALUES.read_text(), re.MULTILINE
)[1]
PLUGINS_REF = re.search(
    r"^\s*ORCH_ANY_MCP_PLUGINS_REF:\s*(\S+)", ORCH_VALUES.read_text(), re.MULTILINE
)[1]
PLUGINS_SOURCE = f"any-mcp-tatara-plugins//orchestration/orchestrator?ref={PLUGINS_REF}"
POD_CONFIG = (
    "plugins:\n  orchestrator:\n    source: "
    f"git::https://github.com/szymonrychu/{PLUGINS_SOURCE}\n    config:\n      token: ${{ORCH_AGENT_TOKEN}}\n"
)


def tools_json(*names, description="a tool"):
    """The shape of `any-mcp --list-tools`: a JSON array of {name, description, inputSchema}."""
    return json.dumps(
        [{"name": n, "description": description, "inputSchema": {"type": "object"}} for n in names],
        indent=2,
    )


# The body of `GET /agent/v1/activity` as the orchestrator README (Agent API) documents it: rendered status lines, oldest first.
ACTIVITY_BODY = json.dumps(
    {
        "lines": [
            {
                "id": 1,
                "agent": "agent-x-0",
                "text": "agent-x-0 ran: true (ok)",
                "ts": "2026-10-27T00:00:00+00:00",
            }
        ]
    }
)

# Stands in for the in-pod python of smoke step 6a: reads the body on stdin and prints what the real script prints after the status.
FAKE_HAS_LINES = """\
import json
import sys

try:
    body = json.load(sys.stdin)
except ValueError:
    body = None
print(isinstance(body, dict) and isinstance(body.get("lines"), list))
"""

WH_USER = "wh-user-smoke"
WH_PASSWORD = "wh-pass-SECRET-value-123"
WH_TOKEN = "orch-token-SECRET-value-456"
WH_ENDPOINT = "/webhooks/api/v1/endpoints/7"
SCHED = "/schedules/api/v1"
MATRIX = "/matrix/api/v1"
MATRIX_USER = "@hermes:szymonrichert.pl"
MESSAGE_ID = 42
AGENT_TOKEN = "agent-token-SECRET-value-789"
CONT_WRAPPER = "wrap-1"
CONT_AGENT = "agent-cont-0"
CONT_MESSAGE_IDS = (51, 52)
CONT_MESSAGES = "/api/v1/sessions/sid-cont/messages"
DEFAULT_ROUTES = (
    "lab.szymonrichert.pl / agents-web\nlab.szymonrichert.pl /api agents-orchestrator\n"
)


# Stands in for `kubectl exec <pod> -c ccw -- python3 -c <script> <method> <path> [body]` against the
# agent API: argv carries the pod, the method, the path and the body; the pod's token is never seen.
FAKE_AGENT_HELPER = r"""
import json, os, re, sys

args = sys.argv[1:]
pod = next(a for a in args if a.startswith("pod/"))
who = re.search(r"smoke-msg-([ab])-(\d+)-0", pod)
side, ts = who[1], who[2]
method, path = args[-2:] if args[-2] in ("GET", "POST") else args[-3:-1]
env = os.environ
hive = f"smoke-{ts}"
a, b = f"smoke-msg-a-{ts}", f"smoke-msg-b-{ts}"
if method == "GET":
    slugs = {
        "a": [] if env.get("FAKE_A_HIDES_B") else [b],
        "b": [a] + ([hive] if env.get("FAKE_B_LISTS_HIVE") else []),
    }[side]
    if side == "a":
        slugs.append(hive)
    print(200, json.dumps({"sessions": [{"slug": s} for s in slugs]}))
elif side == "a":
    assert path == f"/agent/v1/sessions/{b}/messages", path
    print(env.get("FAKE_SEND_STATUS", "202"), json.dumps({"message_id": MESSAGE_ID}))
else:
    assert path == f"/agent/v1/sessions/{hive}/messages", path
    code = env.get("FAKE_B_SEND_CODE", "target_not_superleader")
    print(env.get("FAKE_B_SEND_STATUS", "403"), json.dumps({"error": {"code": code}}))
""".replace("MESSAGE_ID", str(42))


class _Api:
    def __init__(self):
        self.requests: list[tuple[str, str, bytes]] = []
        self.leader: str | None = "agent-uuid-0"
        self.leader_reason: str = "start"
        self.slug: str | None = None
        # After the scale to 1 the fake moves the leader to pod-0 with this reason (None: it never moves).
        self.scale_down_leader_reason: str | None = "scale_down"
        self.leader_post_status = 202
        # pod-1's agent is listed ready false for this many /agents GETs; a /leader POST before that is counted as early.
        self.pod1_not_ready_polls = 0
        self.early_leader_posts = 0
        # The first N /leader POSTs answer 202 but leave the leader unchanged (the orchestrator rejected target_not_ready).
        self.leader_posts_ignored = 0
        self.init_status: dict = {
            "state": "succeeded",
            "exit_code": 0,
            "reason": None,
            "log_tail": "smoke-init\n",
        }
        self.init_scripts: list[str] = ["echo smoke-init"]
        self.spec: dict = {}
        self.scale_status = 202
        self.closes_at_offset: int | None = 3600
        self.restart_pod0 = False
        self.web_root = '<!doctype html><html><body><div id="root"></div></body></html>'
        self.web_config: dict = {
            "backends": [
                {"id": "orchestrator", "basePath": "/api/v1"},
                {"id": "matrix", "basePath": MATRIX},
            ],
        }
        self.state_dir: Path | None = None
        # webhook service fakes (api, ingest and events share this one server)
        self.endpoint_create_status = 201
        self.accept_any_password = False
        self.ingest_ok_reason = "action_ignored"
        self.unknown_suffix_body = None
        self.healthz_status = 200
        self.events: list[dict] | None = None
        self.public_auth_header = 'Basic realm="agents-webhook"'
        self.public_status = 401
        self.suffix: str | None = None
        self.store_filters = True
        self.preview_passes = False
        self.put_clears = True
        # schedule service fakes (same server)
        self.run_status = "in-progress"
        self.run_reason: str | None = None
        self.run_session: str | None = "sid-sched"
        self.schedule_delete_status = 204
        self.readyz_status = 200
        # workspace cache fakes: the store maps a cache key to the marker the last deleted session wrote
        self.cache_available = True
        self.cache_carries = True
        self.cache_init_fails_a = False
        self.cache_store: dict[str, str] = {}
        self.cache_sessions: dict[str, dict] = {}
        # matrix connector fakes (same server)
        self.matrix_user = MATRIX_USER
        self.matrix_sync_healthy = True
        self.matrix_rooms_status = 200
        self.matrix_rules: dict[int, dict] = {}
        self.matrix_list_hides_rule = False
        # session option fakes (kube access and cross-session messaging, same server)
        self.kube_available = True
        self.option_specs: dict[str, dict] = {}
        self.option_deleted: set[str] = set()
        self.message_status = "sent"
        self.frame_in_events = True
        # session continuity fakes (smoke-cont-<ts>, same server): the fake kubectl marks the restart in the state dir
        self.cont_state_after_restart = "idle"
        self.cont_wrapper_after_restart = CONT_WRAPPER
        self.cont_second_status = "sent"
        self.cont_events_after_restart = True
        self.cont_message_ids: list[int] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                n = int(self.headers.get("Content-Length") or 0)
                api.requests.append((self.command, self.path, self.rfile.read(n)))

            def _basic_ok(self):
                want = base64.b64encode(f"{WH_USER}:{WH_PASSWORD}".encode()).decode()
                got = self.headers.get("Authorization", "")
                return got == f"Basic {want}" or (
                    api.accept_any_password and got.startswith("Basic ")
                )

            def _unauthorized(self, www=True):
                body = json.dumps({"error": {"code": "unauthorized", "message": "unauthorized"}})
                data = body.encode()
                self.send_response(401)
                if www:
                    self.send_header("WWW-Authenticate", 'Basic realm="agents-webhook"')
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._record()
                path = self.path.split("?")[0]
                if path == "/healthz":
                    self._send(api.healthz_status, json.dumps({"status": "ok"}))
                elif path == "/readyz":
                    self._send(api.readyz_status, json.dumps({"status": "ready"}))
                elif path == f"{SCHED}/runs/run-1":
                    run = {
                        "id": "run-1",
                        "schedule_id": "sch-1",
                        "trigger": "manual",
                        "status": api.run_status,
                        "reason": api.run_reason,
                        "session_id": api.run_session if api.run_status != "failed" else None,
                        "session_slug": "smoke-1-abcd1234",
                        "message_id": "msg-1",
                    }
                    self._send(200, json.dumps(run))
                elif path.startswith(MATRIX + "/"):
                    self._matrix_get(path)
                elif path == "/api/v1/presets":
                    presets = [
                        {"id": "preset-other", "name": "other", "is_default": False},
                        {"id": "preset-default", "name": "default", "is_default": True},
                    ]
                    self._send(200, json.dumps(presets))
                elif path == "/webhooks/api/v1/events":
                    events = api.events
                    if events is None:
                        events = [
                            {
                                "id": 101,
                                "endpoint_id": 7,
                                "status": "ignored",
                                "reason": "action_ignored",
                            }
                        ]
                    self._send(200, json.dumps(events))
                elif self.path == "/":
                    self._send(200, api.web_root, "text/html")
                elif self.path == "/config.json":
                    self._send(200, json.dumps(api.web_config))
                elif self.path == "/api/v1/openapi.json":
                    self._send(200, "{}")
                elif self.path == "/api/v1/sessions/sid-1":
                    # Hive rule (agents-orchestrator db/leadership.py): only coalesce(max_replicas, replicas) > 1 elects a leader.
                    size = api.spec.get("max_replicas") or api.spec.get("replicas") or 0
                    leader = api.leader if size > 1 else None
                    view = {
                        "id": "sid-1",
                        "spec": api.spec,
                        "created_at": "2026-10-25T10:00:00.123456Z",
                        "leader_agent_id": leader,
                        "leader_since": "2026-10-25T10:00:30Z" if leader else None,
                        "leader_reason": api.leader_reason if leader else None,
                    }
                    if api.closes_at_offset is not None:
                        view["closes_at"] = (
                            f"2026-10-25T{10 + api.closes_at_offset // 3600}:00:00.123456Z"
                        )
                    self._send(200, json.dumps(view))
                elif self.path == "/api/v1/sessions/sid-1/agents":
                    not_ready = api.pod1_not_ready_polls
                    if (api.state_dir / "pod1").exists() and not_ready > 0:
                        api.pod1_not_ready_polls -= 1
                    agents = [
                        {
                            "id": f"agent-uuid-{i}",
                            "ordinal": i,
                            "pod_name": f"agent-{api.slug}-{i}",
                            "ready": not (i == 1 and not_ready > 0),
                            "leader": api.leader == f"agent-uuid-{i}",
                            "gone": False,
                        }
                        for i in range(2 if (api.state_dir / "pod1").exists() else 1)
                    ]
                    self._send(200, json.dumps(agents))
                elif self.path == "/api/v1/sessions/sid-1/init-scripts":
                    body = {"scripts": api.init_scripts, "status": api.init_status}
                    self._send(200, json.dumps(body))
                elif self.path == "/api/v1/meta":
                    meta = {
                        "cache": {"available": api.cache_available, "max_bytes": 5368709120},
                        "kube_access": {"available": api.kube_available},
                    }
                    self._send(200, json.dumps(meta))
                elif path == f"/api/v1/messages/{MESSAGE_ID}":
                    self._send(200, json.dumps({"id": MESSAGE_ID, "status": api.message_status}))
                elif path == "/api/v1/sessions/sid-msg-b/events":
                    self._send(200, json.dumps(api.b_events()))
                elif self._cache_get() or self._cont_get():
                    pass
                else:
                    self._send(404, "{}")

            def _cont_get(self):
                path, _, query = self.path.partition("?")
                restarted = (api.state_dir / "cont_restarted").exists()
                if path == "/api/v1/sessions/sid-cont/agents":
                    state = api.cont_state_after_restart if restarted else "idle"
                    agent = {
                        "id": CONT_AGENT,
                        "ordinal": 0,
                        "pod_name": f"agent-{api.option_specs['cont']['slug']}-0",
                        "ready": True,
                        "state": state,
                        "leader": False,
                        "gone": False,
                    }
                    self._send(200, json.dumps([agent]))
                elif path == "/api/v1/sessions/sid-cont/events":
                    params = parse_qs(query)
                    events = api.cont_events(restarted)
                    if "agent_id" in params:
                        events = [e for e in events if e["agent_id"] == params["agent_id"][0]]
                    if "types" in params:
                        events = [e for e in events if e["type"] in params["types"][0].split(",")]
                    if "after_id" in params:
                        events = [e for e in events if e["id"] > int(params["after_id"][0])]
                    page = {"events": events, "has_more_before": False, "next_after_id": None}
                    self._send(200, json.dumps(page))
                elif path in (f"/api/v1/messages/{i}" for i in CONT_MESSAGE_IDS):
                    mid = int(path.rsplit("/", 1)[1])
                    status = "sent" if mid == CONT_MESSAGE_IDS[0] else api.cont_second_status
                    self._send(200, json.dumps({"id": mid, "status": status}))
                else:
                    return False
                return True

            def _matrix_get(self, path):
                if path == f"{MATRIX}/meta":
                    meta = {
                        "user_id": api.matrix_user,
                        "sync": {"healthy": api.matrix_sync_healthy, "last_sync_at": None},
                    }
                    self._send(200, json.dumps(meta))
                elif path == f"{MATRIX}/rules":
                    rules = [] if api.matrix_list_hides_rule else list(api.matrix_rules.values())
                    body = {"allow": [], "deny": [r for r in rules if r["list"] == "deny"]}
                    self._send(200, json.dumps(body))
                elif path == f"{MATRIX}/rooms":
                    self._send(api.matrix_rooms_status, "[]")
                else:
                    self._send(404, "{}")

            def _matrix_rule_create(self):
                sent = json.loads(api.requests[-1][2])
                rule = {"id": 5, "note": None, "created_at": "2026-10-25T10:00:00Z", **sent}
                api.matrix_rules[5] = rule
                self._send(201, json.dumps(rule))

            def _cache_get(self):
                m = re.fullmatch(r"/api/v1/sessions/(sid-cache-[ab])(/init-scripts)?", self.path)
                sess = api.cache_sessions.get(m[1]) if m else None
                if sess is None or sess["deleted"]:
                    return False
                key = sess["spec"]["cache"]["key"]
                script = sess["spec"]["init_scripts"][0]
                restore = "restored" if key in api.cache_store else "miss"
                if m[2]:
                    init = {"state": "succeeded", "exit_code": 0, "log_tail": ""}
                    want = re.search(r'grep -qx "(\w+)" cache-marker', script)
                    if want and api.cache_store.get(key) != want[1]:
                        init = {"state": "failed", "exit_code": 1, "log_tail": "grep: no match"}
                    if m[1] == "sid-cache-a" and api.cache_init_fails_a:
                        init = {"state": "failed", "exit_code": 2, "log_tail": "boom"}
                    self._send(200, json.dumps({"scripts": [script], "status": init}))
                else:
                    status = {"cache": {"key": key, "restore": {"state": restore}, "save": None}}
                    self._send(
                        200, json.dumps({"id": m[1], "spec": sess["spec"], "status": status})
                    )
                return True

            def _cache_create(self, created):
                sid = "sid-cache-" + created["slug"][-1]
                api.cache_sessions[sid] = {"spec": created["spec"], "deleted": False}
                self._send(201, json.dumps({"id": sid, "slug": created["slug"]}))

            def _cache_delete(self):
                sess = api.cache_sessions.get(self.path.rsplit("/", 1)[1])
                if sess is None or sess["deleted"]:
                    self._send(404, "{}")
                    return
                sess["deleted"] = True
                write = re.search(
                    r"printf '%s\\n' \"(\w+)\" > cache-marker", sess["spec"]["init_scripts"][0]
                )
                if write and api.cache_carries:
                    api.cache_store[sess["spec"]["cache"]["key"]] = write[1]
                self._send(202, json.dumps({"desired": "deleting"}))

            def _scale(self):
                n = json.loads(api.requests[-1][2])["replicas"]
                if api.scale_status != 202:
                    self._send(api.scale_status, json.dumps({"detail": "refused"}))
                    return
                pod1 = api.state_dir / "pod1"
                if n >= 2:
                    pod1.touch()
                else:
                    pod1.unlink(missing_ok=True)
                    if api.scale_down_leader_reason is not None and api.leader == "agent-uuid-1":
                        api.leader = "agent-uuid-0"
                        api.leader_reason = api.scale_down_leader_reason
                if api.restart_pod0:
                    (api.state_dir / "uid0").write_text("uid-pod-0-restarted")
                self._send(202, json.dumps({"id": "sid-1", "replicas": n}))

            def _leader_post(self):
                if api.leader_post_status != 202:
                    self._send(api.leader_post_status, json.dumps({"detail": "refused"}))
                    return
                if api.pod1_not_ready_polls > 0:
                    api.early_leader_posts += 1
                if api.leader_posts_ignored > 0:
                    api.leader_posts_ignored -= 1
                    self._send(202, json.dumps({"agent_id": None}))
                    return
                api.leader = json.loads(api.requests[-1][2])["agent_id"]
                api.leader_reason = "user"
                self._send(202, json.dumps({"agent_id": api.leader}))

            def _webhook_post(self):
                if self.path == "/webhooks/api/v1/endpoints":
                    if api.endpoint_create_status != 201:
                        self._send(api.endpoint_create_status, "{}")
                        return
                    created = json.loads(api.requests[-1][2])
                    api.suffix = created["suffix"]
                    view = {
                        "id": 7,
                        "suffix": created["suffix"],
                        "type": created["type"],
                        "preset_id": created["preset_id"],
                        "username": WH_USER,
                        "password": WH_PASSWORD,
                        "filters": created.get("filters", {})
                        if api.store_filters
                        else {"allow": [], "deny": []},
                    }
                    self._send(201, json.dumps(view))
                    return
                if self.path == "/webhooks/api/v1/templates/preview":
                    if api.preview_passes:
                        result = {"passed": True, "reason": None}
                        text = None
                    else:
                        result = {"passed": False, "reason": "filter_denied: title"}
                        text = None
                    self._send(200, json.dumps({"prompt": text, "filters": result}))
                    return
                if not self.path.startswith("/webhook/"):
                    self._send(404, "{}")
                    return
                suffix = self.path[len("/webhook/") :]
                if suffix == api.suffix and self._basic_ok():
                    ok = {"id": 101, "status": "ignored", "reason": api.ingest_ok_reason}
                    self._send(202, json.dumps(ok))
                elif suffix != api.suffix and api.unknown_suffix_body is not None:
                    self._send(200, api.unknown_suffix_body)
                elif suffix == "smoke-unknown":
                    if api.public_status == 401:
                        self._unauthorized(www=api.public_auth_header is not None)
                    else:
                        self._send(api.public_status, "{}")
                else:
                    self._unauthorized()

            def _schedule_post(self):
                if self.path == f"{SCHED}/schedules":
                    created = json.loads(api.requests[-1][2])
                    view = {"id": "sch-1", **created, "next_fires": []}
                    self._send(201, json.dumps(view))
                elif self.path == f"{SCHED}/schedules/preview":
                    fires = ["2026-10-26T03:00:00Z", "2026-10-27T03:00:00Z", "2026-10-28T03:00:00Z"]
                    self._send(200, json.dumps({"next_fires": fires}))
                elif self.path == f"{SCHED}/schedules/sch-1/run":
                    run = {"id": "run-1", "schedule_id": "sch-1", "status": "new"}
                    self._send(202, json.dumps(run))
                else:
                    self._send(404, "{}")

            def do_POST(self):
                self._record()
                if self.path.startswith("/schedules/"):
                    self._schedule_post()
                    return
                if self.path == f"{MATRIX}/rules":
                    self._matrix_rule_create()
                    return
                if self.path.startswith(("/webhook", "/webhooks")):
                    self._webhook_post()
                    return
                if self.path == CONT_MESSAGES:
                    api.cont_message_ids.append(CONT_MESSAGE_IDS[len(api.cont_message_ids)])
                    self._send(202, json.dumps({"message_id": api.cont_message_ids[-1]}))
                    return
                if self.path == "/api/v1/sessions/sid-1/scale":
                    self._scale()
                    return
                if self.path == "/api/v1/sessions/sid-1/leader":
                    self._leader_post()
                    return
                if self.path != "/api/v1/sessions":
                    self._send(404, "{}")
                    return
                created = json.loads(api.requests[-1][2])
                option = re.match(r"smoke-(kube|msg-a|msg-b|src|cont)-\d+$", created["slug"])
                if option:
                    api.option_specs[option[1]] = created
                    self._send(201, json.dumps({"id": f"sid-{option[1]}", "slug": created["slug"]}))
                    return
                if "cache" in (created.get("spec") or {}):
                    self._cache_create(created)
                    return
                api.spec = created.get("spec") or {}
                slug = created["slug"]
                api.slug = slug
                self._send(201, json.dumps({"id": "sid-1", "slug": slug}))

            def do_PUT(self):
                self._record()
                if self.path == WH_ENDPOINT + "/filters":
                    sent = json.loads(api.requests[-1][2])
                    view = {
                        "id": 7,
                        "filters": sent
                        if api.put_clears
                        else {"allow": [], "deny": [{"field": "title"}]},
                    }
                    self._send(200, json.dumps(view))
                    return
                self._send(404, "{}")

            def do_DELETE(self):
                self._record()
                if self.path == WH_ENDPOINT:
                    self.send_response(204)
                    self.end_headers()
                    return
                if self.path.startswith(f"{MATRIX}/rules/"):
                    gone = api.matrix_rules.pop(int(self.path.rsplit("/", 1)[1]), None)
                    self.send_response(204 if gone else 404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == f"{SCHED}/schedules/sch-1":
                    self.send_response(api.schedule_delete_status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/api/v1/sessions/sid-sched":
                    self._send(202, json.dumps({"id": "sid-sched", "desired": "deleting"}))
                    return
                if self.path in (
                    f"/api/v1/sessions/sid-{k}" for k in ("kube", "msg-a", "msg-b", "src", "cont")
                ):
                    api.option_deleted.add(self.path.rsplit("/", 1)[1])
                    self._send(202, json.dumps({"desired": "deleting"}))
                    return
                if self.path.startswith("/api/v1/sessions/sid-cache-"):
                    self._cache_delete()
                    return
                if self.path != "/api/v1/sessions/sid-1":
                    self._send(404, "{}")
                    return
                self._send(202, json.dumps({"id": "sid-1", "desired": "deleting"}))

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def methods(self):
        return [(m, p) for m, p, _ in self.requests]

    def cont_events(self, restarted):
        """The stored events of the continuity agent: one finished turn, plus the second turn once the pod restarted."""

        def event(i, type_, wrapper):
            return {
                "id": i,
                "agent_id": CONT_AGENT,
                "wrapper_session_id": wrapper,
                "seq": i,
                "type": type_,
                "data": {},
            }

        events = [
            event(1, "user_message", CONT_WRAPPER),
            event(2, "assistant_text", CONT_WRAPPER),
            event(3, "turn_completed", CONT_WRAPPER),
        ]
        if restarted and len(self.cont_message_ids) > 1 and self.cont_events_after_restart:
            wrapper = self.cont_wrapper_after_restart
            events += [event(4, "user_message", wrapper), event(5, "turn_completed", wrapper)]
        return events

    def b_events(self):
        a_slug = self.option_specs["msg-a"]["slug"]
        texts = ["unrelated"]
        if self.frame_in_events:
            texts.append(
                f'<session-message from="{a_slug}" id="{MESSAGE_ID}">\nsmoke ping\n</session-message>'
            )
        events = [
            {"id": i, "type": "user_message", "data": {"text": t}} for i, t in enumerate(texts, 1)
        ]
        return {"events": events, "has_more_before": False, "next_after_id": len(events)}


@pytest.fixture
def api(tmp_path):
    a = _Api()
    a.state_dir = tmp_path / "state"
    a.state_dir.mkdir()
    (a.state_dir / "uid0").write_text("uid-pod-0")
    (a.state_dir / "uid_cont").write_text("uid-cont-old")
    yield a
    a.server.shutdown()


@pytest.fixture
def fakebin(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    k = b / "kubectl"
    k.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *" wait "*) echo "$*" >> "$FAKE_KUBECTL_LOG";'
        ' case "$*" in *"Ready pod/agent-"*"-1 "*) sleep "${FAKE_POD1_READY_SLEEP_S:-0}" ;; esac;'
        ' case "$*" in *"${FAKE_WAIT_FAIL:-@none@}"*) exit 1 ;; esac; exit 0 ;;\n'
        '  *"get secret"*) printf "%s" "$FAKE_WH_TOKEN_B64"; exit 0 ;;\n'
        '  *"get service"*) [ -n "${FAKE_AGENTS_PORT-8001}" ] || exit 1;'
        ' echo "$*" >> "$FAKE_KUBECTL_LOG"; printf "%s" "${FAKE_AGENTS_PORT-8001}"; exit 0 ;;\n'
        '  *"get ingress"*) [ -z "${FAKE_INGRESS_FAIL-}" ] || exit 1;'
        ' printf "%s" "$FAKE_INGRESS_ROUTES"; exit 0 ;;\n'
        '  *" delete "*) echo "$*" >> "$FAKE_KUBECTL_LOG";'
        ' case "$*" in *"pod/agent-smoke-cont-"*) touch "$FAKE_STATE_DIR/cont_restarted";'
        ' [ -n "${FAKE_CONT_KEEP_UID-}" ] || echo uid-cont-new > "$FAKE_STATE_DIR/uid_cont" ;; esac; exit 0 ;;\n'
        '  *"get pod/agent-smoke-cont-"*) cat "$FAKE_STATE_DIR/uid_cont"; exit 0 ;;\n'
        '  *"get pod/agent-smoke-kube-"*serviceAccountName*) printf "%s" "${FAKE_KUBE_SA-agents-orchestrator-kube-access}"; exit 0 ;;\n'
        '  *"get pod/agent-smoke-kube-"*automountServiceAccountToken*) printf "%s" "${FAKE_KUBE_AUTOMOUNT-true}"; exit 0 ;;\n'
        '  *"get pod/agent-"*automountServiceAccountToken*) printf "%s" "${FAKE_HIVE_AUTOMOUNT-false}"; exit 0 ;;\n'
        '  *"get pod/agent-"*) cat "$FAKE_STATE_DIR/uid0"; exit 0 ;;\n'
        '  *"get pod"*) echo "$*" >> "$FAKE_KUBECTL_LOG";'
        ' slug=$(echo "$*" | sed -n "s|.*agents.szymonrichert.pl/session=\\([^ ]*\\).*|\\1|p");'
        ' [ -n "$slug" ] || exit 0;'
        ' echo "pod/agent-${slug}-init-abcde"; echo "pod/agent-${slug}-0";'
        ' [ ! -e "$FAKE_STATE_DIR/pod1" ] || echo "pod/agent-${slug}-1"; exit 0 ;;\n'
        '  *" exec "*|*port-forward*) echo "$*" >> "$FAKE_KUBECTL_LOG" ;;\n'
        "  *) ;;\n"
        "esac\n"
        'case "$*" in\n'
        '  *"/api/v1/presets"*) cat > /dev/null;'
        ' printf "%s %s\\n" "${FAKE_SVC_WITH-200}" "${FAKE_SVC_WITHOUT-401}"; exit 0 ;;\n'
        '  *"SelfSubjectAccessReview"*) printf "%s\\n" "${FAKE_KUBE_PROBE-true 200}"; exit 0 ;;\n'
        '  *"test ! -e"*) exit "${FAKE_HIVE_TOKEN_PRESENT-0}" ;;\n'
        '  *" exec "*"/agent/v1/sessions"*) exec python3 "$FAKE_AGENT_HELPER" "$@" ;;\n'
        '  *"/agent/v1/activity"*)'
        ' printf "%s %s\\n" "${FAKE_ACTIVITY_STATUS-200}"'
        ' "$(printf "%s" "$FAKE_ACTIVITY_BODY" | python3 "$FAKE_HAS_LINES_HELPER")"; exit 0 ;;\n'
        '  *"/agent/v1/me"*) echo "$*" >> "$FAKE_KUBECTL_LOG"; printf "%s\\n" "${FAKE_ME_STATUS-200}"; exit 0 ;;\n'
        '  *"pip show any-mcp-tatara-orchestrator"*) [ -z "${FAKE_PIP_FAIL-}" ] || exit 1;'
        ' echo "Name: any-mcp-tatara-orchestrator"; exit 0 ;;\n'
        '  *"cat /opt/any-mcp/etc/any-mcp.yaml"*) printf "%s" "$FAKE_POD_CONFIG"; exit 0 ;;\n'
        '  *"pod/agent-smoke-src-"*"--list-tools"*) printf "%s\\n" "$FAKE_SRC_TOOLS"; exit 0 ;;\n'
        '  *"--list-tools"*) printf "%s\\n" "$FAKE_TOOLS"; exit 0 ;;\n'
        '  *"ls -d /sessions"*) printf "%s\\n" "$FAKE_SESSION_CFG"; exit 0 ;;\n'
        '  *"any-mcp --version"*) printf "%s\\n" "$FAKE_VERSION"; exit 0 ;;\n'
        '  *" exec "*) printf "%s\\n" "$FAKE_MCP_LIST"; exit 0 ;;\n'
        "  *port-forward*) exec sleep 60 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    k.chmod(0o755)
    (b / "fake_agent.py").write_text(FAKE_AGENT_HELPER)
    (b / "fake_has_lines.py").write_text(FAKE_HAS_LINES)
    return b


def _run(
    api,
    fakebin,
    mcp_list,
    version=f"any-mcp {ANY_MCP_VERSION}",
    agents_port="8001",
    ingress_routes=DEFAULT_ROUTES,
    ingress_fail="",
    session_cfg="/sessions/abc/config",
    port_forward=False,
    me_status="200",
    smoke_timeout="20",
    extra_env=None,
    script=SCRIPT,
):
    target = (
        {
            "SMOKE_LOCAL_PORT": str(api.port),
            "SMOKE_WEB_LOCAL_PORT": str(api.port),
            "SMOKE_WEBHOOK_API_LOCAL_PORT": str(api.port),
            "SMOKE_WEBHOOK_INGEST_LOCAL_PORT": str(api.port),
            "SMOKE_SCHEDULE_API_LOCAL_PORT": str(api.port),
            "SMOKE_MATRIX_API_LOCAL_PORT": str(api.port),
        }
        if port_forward
        else {"HOST": f"127.0.0.1:{api.port}", "SCHEME": "http"}
    )
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV", "HOST", "SCHEME")},
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        **target,
        "SMOKE_TIMEOUT_S": smoke_timeout,
        "FAKE_ME_STATUS": me_status,
        "FAKE_AGENT_HELPER": str(fakebin / "fake_agent.py"),
        "FAKE_HAS_LINES_HELPER": str(fakebin / "fake_has_lines.py"),
        "FAKE_ACTIVITY_BODY": ACTIVITY_BODY,
        "FAKE_POD_AGENT_TOKEN": AGENT_TOKEN,
        "FAKE_MCP_LIST": mcp_list,
        "FAKE_VERSION": version,
        "FAKE_SESSION_CFG": session_cfg,
        "FAKE_AGENTS_PORT": agents_port,
        "FAKE_INGRESS_ROUTES": ingress_routes,
        "FAKE_INGRESS_FAIL": ingress_fail,
        "FAKE_STATE_DIR": str(api.state_dir),
        "FAKE_KUBECTL_LOG": str(fakebin / "kubectl.log"),
        "FAKE_WH_TOKEN_B64": base64.b64encode(WH_TOKEN.encode()).decode(),
        "SMOKE_PUBLIC_URL": f"http://127.0.0.1:{api.port}",
        "FAKE_POD_CONFIG": POD_CONFIG,
        "FAKE_TOOLS": tools_json(
            "orchestrator_sessions_list", "orchestrator_sessions_get", "orchestrator_activity"
        ),
        "FAKE_SRC_TOOLS": tools_json("petstore_list_pets", "petstore_get_pet"),
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(script)], check=False, env=env, capture_output=True, text=True, timeout=60
    )


def test_happy_path_exits_zero_and_cleans_up(api, fakebin):
    r = _run(api, fakebin, "any-mcp: http://x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    methods = api.methods()
    assert ("POST", "/api/v1/sessions") in methods
    assert ("DELETE", "/api/v1/sessions/sid-1") in methods
    body = json.loads(next(b for m, _, b in api.requests if m == "POST"))
    assert re.fullmatch(r"smoke-\d+", body["slug"])
    assert body["spec"] == {
        "replicas": 1,
        "max_replicas": 2,
        "init_scripts": ["echo smoke-init"],
        "wrapper": {
            "auto_compact": {"idle_seconds": 1800},
            "hooks": {"onTurnComplete": [{"type": "command", "run": "true"}]},
        },
        "leader_lease_seconds": 3600,
        "autoclose_after_seconds": 3600,
    }
    scales = [json.loads(b) for m, p, b in api.requests if p == "/api/v1/sessions/sid-1/scale"]
    assert scales == [{"replicas": 2}, {"replicas": 1}]
    leaders = [json.loads(b) for m, p, b in api.requests if p == "/api/v1/sessions/sid-1/leader"]
    assert leaders == [{"agent_id": "agent-uuid-1"}]
    assert "leader did not move" not in r.stderr
    assert re.search(r"scale down: leader is agent-uuid-0 with reason scale_down", r.stdout), (
        r.stdout
    )
    assert ("GET", "/api/v1/sessions/sid-1/init-scripts") in methods
    assert not [p for _, p in methods if p.startswith("/api/v1/clusters")]
    log = (fakebin / "kubectl.log").read_text()
    assert "get service agents-orchestrator-agents " in log
    assert re.search(r"get pods -l agents\.szymonrichert\.pl/session=smoke-\d+ ", log), log
    assert re.search(r"created session smoke-\d+ \(sid-1\)", r.stdout), r.stdout
    assert re.search(r"discarded session smoke-\d+ \(sid-1\)", r.stdout), r.stdout
    assert re.search(r"session smoke-\d+ leader agent-uuid-0", r.stdout), r.stdout
    assert "/api to agents-orchestrator" in r.stdout
    assert "cluster" not in (r.stdout + r.stderr).lower()
    assert not (api.state_dir / "pod1").exists()
    assert (api.state_dir / "uid0").read_text() == "uid-pod-0"


def test_config_fields_checked(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "config fields echoed" in r.stdout
    assert "closes_at" in r.stdout
    assert "leader_since" in r.stdout
    assert ("GET", "/api/v1/sessions/sid-1") in api.methods()
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_config_view_mismatch_fails(api, fakebin):
    api.closes_at_offset = None
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "closes_at" in r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_scale_refused_fails(api, fakebin):
    api.scale_status = 409
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "scale" in r.stdout + r.stderr
    assert "409" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_scale_down_leader_not_moved_fails(api, fakebin):
    api.scale_down_leader_reason = None
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
    assert r.returncode != 0
    assert "scale down: leader did not move to pod-0 with reason scale_down" in r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_scale_down_wrong_reason_fails(api, fakebin):
    api.scale_down_leader_reason = "leader_lost"
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
    assert r.returncode != 0
    assert "scale down: leader did not move to pod-0 with reason scale_down" in r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def _leader_posts(api):
    return [p for m, p, b in api.requests if m == "POST" and p == "/api/v1/sessions/sid-1/leader"]


def test_make_leader_waits_for_agent_ready(api, fakebin):
    api.pod1_not_ready_polls = 4
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stderr
    assert api.early_leader_posts == 0
    assert len(_leader_posts(api)) == 1


def test_make_leader_retries_rejected_request(api, fakebin):
    api.leader_posts_ignored = 1
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env={"SMOKE_LEADER_RETRY_S": "1"})
    assert r.returncode == 0, r.stderr
    assert len(_leader_posts(api)) == 2


# pod-1 Ready uses 6 of the 8 seconds of SMOKE_TIMEOUT_S; the waits after it must start their own window.
SLOW_POD1 = {"FAKE_POD1_READY_SLEEP_S": "6"}


def test_agent_lookup_gets_own_deadline_after_slow_pod1_ready(api, fakebin):
    api.pod1_not_ready_polls = 3
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="8", extra_env=SLOW_POD1)
    assert r.returncode == 0, r.stderr
    assert "no ready agent for" not in r.stderr
    assert len(_leader_posts(api)) == 1


def test_make_leader_wait_gets_own_deadline_after_slow_pod1_ready(api, fakebin):
    api.leader_posts_ignored = 1
    r = _run(
        api,
        fakebin,
        "any-mcp: x - Connected",
        smoke_timeout="8",
        extra_env={**SLOW_POD1, "SMOKE_LEADER_RETRY_S": "4"},
    )
    assert r.returncode == 0, r.stderr
    assert "leader did not become" not in r.stderr
    assert len(_leader_posts(api)) == 2


def test_make_leader_refused_fails(api, fakebin):
    api.leader_post_status = 409
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "leader" in r.stderr
    assert "409" in r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_pod0_restart_fails(api, fakebin):
    api.restart_pod0 = True
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "uid" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_missing_any_mcp_fails_and_cleans_up(api, fakebin):
    r = _run(api, fakebin, "other-mcp: http://x - Connected")
    assert r.returncode != 0
    assert "other-mcp" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_smoke_checks_session_config_dir(api, fakebin):
    r = _run(api, fakebin, "any-mcp: /opt/any-mcp/bin/any-mcp - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    assert "CLAUDE_CONFIG_DIR=/sessions/abc/config" in log
    assert "any-mcp --version" in log


def test_any_mcp_not_connected_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: /opt/any-mcp/bin/any-mcp - Failed to connect")
    assert r.returncode != 0
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_wrong_any_mcp_version_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", version="any-mcp 0.1.0")
    assert r.returncode != 0
    assert f"any-mcp --version is not {ANY_MCP_VERSION}" in r.stdout + r.stderr


def test_any_mcp_version_is_the_orchestrator_values_ref(api, fakebin):
    assert ANY_MCP_VERSION == "0.5.0"
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"any-mcp version {ANY_MCP_VERSION}" in r.stdout


def _tree_with_any_mcp_ref(tmp_path, line):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, tree / "scripts" / "smoke.sh")
    shutil.copytree(SCRIPT.parent / "lib", tree / "scripts" / "lib")
    values = tree / "values" / "agents-orchestrator"
    values.mkdir(parents=True)
    (values / "common.yaml").write_text(f"env:\n{line}  ORCH_ANY_MCP_PLUGINS_REF: {PLUGINS_REF}\n")
    return tree / "scripts" / "smoke.sh"


def test_any_mcp_version_follows_the_values_file(api, fakebin, tmp_path):
    script = _tree_with_any_mcp_ref(tmp_path, "  ORCH_ANY_MCP_REF: v9.8.7\n")
    r = _run(api, fakebin, "any-mcp: x - Connected", version="any-mcp 9.8.7", script=script)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "any-mcp version 9.8.7" in r.stdout
    r = _run(api, fakebin, "any-mcp: x - Connected", script=script)
    assert r.returncode != 0
    assert "any-mcp --version is not 9.8.7" in r.stderr


def test_missing_any_mcp_ref_fails_before_any_change(api, fakebin, tmp_path):
    script = _tree_with_any_mcp_ref(tmp_path, "  ORCH_ANY_MCP_REPO: https://example\n")
    r = _run(api, fakebin, "any-mcp: x - Connected", script=script)
    assert r.returncode != 0
    assert "ORCH_ANY_MCP_REF" in r.stderr
    assert not [m for m, _ in api.methods() if m == "POST"]


def test_missing_plugins_ref_fails_before_any_change(api, fakebin, tmp_path):
    script = _tree_with_any_mcp_ref(tmp_path, "  ORCH_ANY_MCP_REF: v9.8.7\n")
    values = script.parent.parent / "values" / "agents-orchestrator" / "common.yaml"
    values.write_text("env:\n  ORCH_ANY_MCP_REF: v9.8.7\n")
    r = _run(api, fakebin, "any-mcp: x - Connected", script=script)
    assert r.returncode != 0
    assert "ORCH_ANY_MCP_PLUGINS_REF" in r.stderr
    assert not [m for m, _ in api.methods() if m == "POST"]


def test_missing_agents_service_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", agents_port="")
    assert r.returncode != 0
    assert "agents-orchestrator-agents" in r.stdout + r.stderr


def test_wrong_agents_service_port_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", agents_port="8000")
    assert r.returncode != 0
    assert "8001" in r.stdout + r.stderr


def test_agents_service_as_ingress_backend_fails(api, fakebin):
    r = _run(
        api,
        fakebin,
        "any-mcp: x - Connected",
        ingress_routes=DEFAULT_ROUTES + "lab.szymonrichert.pl /x agents-orchestrator-agents\n",
    )
    assert r.returncode != 0
    assert "ingress" in r.stdout + r.stderr


def test_ingress_query_error_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", ingress_fail="1")
    assert r.returncode != 0
    assert "ingress" in r.stdout + r.stderr


def test_no_ingresses_still_passes(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", ingress_routes="")
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_session_config_dir_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", session_cfg="")
    assert r.returncode != 0
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_script_is_strict_bounded_and_traps():
    src = SCRIPT.read_text()
    assert "set -euo pipefail" in src
    assert re.search(r"^\s*trap\b.*EXIT", src, re.MULTILINE)
    assert "-X DELETE" in src
    assert "claude mcp list" in src and "any-mcp" in src
    curls = [
        line
        for line in src.splitlines()
        if re.search(r"\bcurl\b", line) and not line.lstrip().startswith("#")
    ]
    assert curls
    for line in curls:
        assert "--max-time" in line, line


def test_script_is_shellcheck_clean():
    assert shutil.which("shellcheck"), "shellcheck must be installed"
    subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_default_target_is_a_port_forward_to_the_api(api, fakebin):
    # The public host is behind SSO; the smoke reaches the api through a port-forward,
    # which the api's source allowlist (127.0.0.1/32) admits.
    r = _run(api, fakebin, "any-mcp: http://x - Connected", port_forward=True)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    assert f"port-forward deploy/agents-orchestrator-api {api.port}:8000" in log
    assert f"port-forward deploy/agents-web {api.port}:8080" in log
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_agent_me_check_fails_without_200(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", me_status="403")
    assert r.returncode != 0
    assert "/agent/v1/me" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_agent_me_check_runs_in_pod_with_its_own_env(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    start = re.search(r"exec pod/agent-smoke-\d+-0 -- python3", log)
    assert start, log
    entry = log[start.start() :]
    assert "/agent/v1/me" in entry
    for name in ("ORCH_AGENT_URL", "ORCH_AGENT_TOKEN", "ORCH_SESSION_ID", "X-Orch-Session"):
        assert name in entry


def _activity_entries(fakebin):
    """The kubectl calls whose in-pod script names the activity route (each call is logged as `$*`, a script spans lines)."""
    log = (fakebin / "kubectl.log").read_text()
    return [c for c in re.split(r"\n(?=-n )", log) if "/agent/v1/activity" in c]


def test_agent_activity_check_runs_in_pod_with_its_own_env(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    entries = _activity_entries(fakebin)
    assert len(entries) == 1, entries
    entry = entries[0]
    assert re.search(r"exec pod/agent-smoke-\d+-0 -- python3 -c", entry), entry
    assert "limit=5" in entry
    for name in ("ORCH_AGENT_URL", "ORCH_AGENT_TOKEN", "ORCH_SESSION_ID", "X-Orch-Session"):
        assert name in entry
    assert "GET /agent/v1/activity" in r.stdout
    assert AGENT_TOKEN not in r.stdout + r.stderr


@pytest.mark.parametrize(
    ("name", "env", "needle"),
    [
        (
            "forbidden",
            {"FAKE_ACTIVITY_STATUS": "403", "FAKE_ACTIVITY_BODY": json.dumps({"error": "x"})},
            "'403 False'",
        ),
        ("not-found", {"FAKE_ACTIVITY_STATUS": "404", "FAKE_ACTIVITY_BODY": ""}, "'404 False'"),
        ("no-lines-key", {"FAKE_ACTIVITY_BODY": json.dumps({"items": []})}, "'200 False'"),
        ("lines-not-array", {"FAKE_ACTIVITY_BODY": json.dumps({"lines": {}})}, "'200 False'"),
        ("body-not-json", {"FAKE_ACTIVITY_BODY": "<html>sso</html>"}, "'200 False'"),
    ],
)
def test_agent_activity_check_fails_without_lines(api, fakebin, name, env, needle):
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env=env)
    assert r.returncode != 0, f"{name}: smoke passed\n{r.stdout}"
    assert "smoke: FAIL: GET /agent/v1/activity" in r.stderr and needle in r.stderr, (
        f"{name}: {r.stderr}"
    )
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_empty_activity_lines_array_passes(api, fakebin):
    r = _run(
        api,
        fakebin,
        "any-mcp: x - Connected",
        extra_env={"FAKE_ACTIVITY_BODY": json.dumps({"lines": []})},
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_leader_fails(api, fakebin):
    api.leader = None
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
    assert r.returncode != 0
    assert re.search(r"session smoke-\d+ has no leader", r.stderr), r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_init_scripts_succeed(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    body = json.loads(next(b for m, _, b in api.requests if m == "POST"))
    assert body["spec"]["init_scripts"] == ["echo smoke-init"]
    assert ("GET", "/api/v1/sessions/sid-1/init-scripts") in api.methods()
    # the init Job pod is listed first but the agent pod is chosen by name
    log = (fakebin / "kubectl.log").read_text()
    assert re.search(r"exec pod/agent-smoke-\d+-0 ", log)
    assert "-init-abcde" not in log
    assert "init scripts succeeded" in r.stdout


def test_init_scripts_failed_fails_and_cleans_up(api, fakebin):
    api.init_status = {
        "state": "failed",
        "exit_code": 7,
        "reason": "BackoffLimitExceeded",
        "log_tail": "boom\n",
    }
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "BackoffLimitExceeded" in out
    assert "exit_code=7" in out
    assert "boom" in out
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_init_scripts_timeout_fails_and_cleans_up(api, fakebin):
    api.init_status = {"state": "running", "exit_code": None, "reason": None, "log_tail": ""}
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="8")
    assert r.returncode != 0
    assert "init" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_init_scripts_wrong_log_or_scripts_fails(api, fakebin):
    api.init_status = {"state": "succeeded", "exit_code": 0, "reason": None, "log_tail": "other\n"}
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "smoke-init" in r.stdout + r.stderr
    api.requests.clear()
    api.init_status["log_tail"] = "smoke-init\n"
    api.init_scripts = ["echo other"]
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_ui_checked_on_web_service(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", port_forward=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("GET", "/") in api.methods()
    assert ("GET", "/config.json") in api.methods()
    assert "deploy/agents-web" in (fakebin / "kubectl.log").read_text()
    assert "config.json lists backend orchestrator" in r.stdout


def test_web_root_not_html_fails(api, fakebin):
    api.web_root = "plain text"
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "agents-web index" in r.stdout + r.stderr


def test_web_config_without_orchestrator_fails(api, fakebin):
    api.web_config = {"backends": [{"id": "other", "basePath": "/api/v1"}]}
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "orchestrator" in r.stdout + r.stderr


def test_root_path_routed_to_api_fails(api, fakebin):
    routes = "lab.szymonrichert.pl / agents-orchestrator\nlab.szymonrichert.pl /api agents-orchestrator\n"
    r = _run(api, fakebin, "any-mcp: x - Connected", ingress_routes=routes)
    assert r.returncode != 0
    assert "agents-web" in r.stdout + r.stderr


def test_api_path_missing_fails(api, fakebin):
    r = _run(
        api,
        fakebin,
        "any-mcp: x - Connected",
        ingress_routes="lab.szymonrichert.pl / agents-web\n",
    )
    assert r.returncode != 0
    assert "/api" in r.stdout + r.stderr


def test_sso_page_instead_of_web_index_fails(api, fakebin):
    api.web_root = "<html><body>Sign in to Keycloak</body></html>"
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "agents-web index" in r.stdout + r.stderr


def _wh_delete(api):
    return ("DELETE", WH_ENDPOINT) in api.methods()


def test_webhook_checks_happy_path(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    for name in ("agents-webhook-ingest", "agents-webhook-api", "agents-webhook-worker"):
        assert f"deploy/{name}" in log
    create = next(b for m, p, b in api.requests if (m, p) == ("POST", "/webhooks/api/v1/endpoints"))
    body = json.loads(create)
    assert body["type"] == "github-issue"
    assert body["preset_id"] == "preset-default"
    assert re.fullmatch(r"smoke-[0-9a-f]+", body["suffix"])
    posts = [p for m, p, _ in api.requests if m == "POST" and p.startswith("/webhook/")]
    assert posts.count(f"/webhook/{body['suffix']}") == 2
    assert "/webhook/smoke-unknown" in posts
    assert len(posts) == 4
    assert body["filters"]["deny"][0]["field"] == "title"
    assert body["filters"]["deny"][0]["match"] == "regex"
    assert body["filters"]["deny"][0]["values"] == ["^smoke-deny"]
    preview = next(
        b for m, p, b in api.requests if (m, p) == ("POST", "/webhooks/api/v1/templates/preview")
    )
    pbody = json.loads(preview)
    assert pbody["type"] == "github-issue"
    assert pbody["example"]["issue"]["title"] == "smoke-deny check"
    assert pbody["example"]["action"] == "opened"
    assert pbody["filters"] == body["filters"]
    put = next(b for m, p, b in api.requests if (m, p) == ("PUT", WH_ENDPOINT + "/filters"))
    assert json.loads(put) == {"allow": [], "deny": []}
    for line in (
        "endpoint stored the deny filter",
        "preview denied by the filter",
        "filters replaced",
    ):
        assert line in r.stdout
    assert ("GET", "/healthz") in api.methods()
    assert ("GET", "/webhooks/api/v1/events?endpoint_id=7&limit=5") in api.methods()
    assert _wh_delete(api)
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()
    assert re.search(
        r"exec -i pod/agent-smoke-\d+-0 -- python3 .*agents-orchestrator-services:8002",
        log,
        re.DOTALL,
    )
    assert "agents-orchestrator-services:8002" in r.stdout
    assert "WWW-Authenticate" in r.stdout


def test_webhook_port_forwards_bypass_the_sidecar(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", port_forward=True)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    assert f"port-forward deploy/agents-webhook-api {api.port}:8000" in log
    assert f"port-forward svc/agents-webhook-ingest {api.port}:80" in log
    assert _wh_delete(api)


@pytest.mark.parametrize(
    ("name", "setup", "extra_env", "needle", "endpoint_created"),
    [
        (
            "worker-not-available",
            None,
            {"FAKE_WAIT_FAIL": "agents-webhook-worker"},
            "worker",
            False,
        ),
        ("endpoint-create-fails", {"endpoint_create_status": 500}, None, "endpoint", False),
        ("event-not-ignored", {"ingest_ok_reason": "queued"}, None, "action_ignored", True),
        ("wrong-password-accepted", {"accept_any_password": True}, None, "wrong password", True),
        (
            "unknown-suffix-body-differs",
            {"unknown_suffix_body": '{"x": 1}'},
            None,
            "unknown suffix",
            True,
        ),
        ("healthz-down", {"healthz_status": 500}, None, "healthz", True),
        ("event-missing-from-api", {"events": []}, None, "events", True),
        ("service-port-rejects-token", None, {"FAKE_SVC_WITH": "401"}, "8002", True),
        ("service-port-open-without-token", None, {"FAKE_SVC_WITHOUT": "200"}, "8002", True),
        ("public-401-lacks-realm", {"public_auth_header": None}, None, "WWW-Authenticate", True),
        ("public-not-401", {"public_status": 302}, None, "public", True),
    ],
)
def test_webhook_check_failures(api, fakebin, name, setup, extra_env, needle, endpoint_created):
    for key, value in (setup or {}).items():
        setattr(api, key, value)
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env=extra_env)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"{name}: smoke passed\n{out}"
    assert needle in out, f"{name}: {out}"
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()
    assert _wh_delete(api) is endpoint_created


@pytest.mark.parametrize(
    ("name", "setup", "needle"),
    [
        (
            "filters-not-stored",
            {"store_filters": False},
            "endpoint create did not store the filters",
        ),
        ("preview-passed", {"preview_passes": True}, "preview"),
        ("put-not-applied", {"put_clears": False}, "PUT"),
    ],
)
def test_webhook_filter_check_failures(api, fakebin, name, setup, needle):
    for key, value in setup.items():
        setattr(api, key, value)
    r = _run(api, fakebin, "any-mcp: x - Connected")
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"{name}: smoke passed\n{out}"
    assert needle in out, f"{name}: {out}"
    assert _wh_delete(api)
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_webhook_smoke_ingests_only_the_closed_issue(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    delivered = [
        b
        for m, p, b in api.requests
        if m == "POST" and p.startswith("/webhook/") and p != "/webhook/smoke-unknown"
    ]
    assert len(delivered) == 3
    for raw in delivered:
        assert json.loads(raw)["action"] == "closed"
        assert "smoke-deny" not in raw.decode()


def test_webhook_smoke_prints_no_secret(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    for secret in (WH_PASSWORD, WH_USER, WH_TOKEN, base64.b64encode(WH_TOKEN.encode()).decode()):
        assert secret not in r.stdout + r.stderr
    auth = base64.b64encode(f"{WH_USER}:{WH_PASSWORD}".encode()).decode()
    assert auth not in r.stdout + r.stderr
    # also on a failing run: the failure path never echoes the create response or the token
    api.requests.clear()
    api.accept_any_password = True
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    for secret in (WH_PASSWORD, WH_TOKEN, auth):
        assert secret not in r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    assert WH_TOKEN not in log and WH_PASSWORD not in log


def _sched_requests(api):
    return [(m, p, b) for m, p, b in api.requests if p.startswith(SCHED)]


def test_schedule_step_passes(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    methods = api.methods()
    assert ("GET", "/readyz") in methods
    create = next(b for m, p, b in api.requests if (m, p) == ("POST", f"{SCHED}/schedules"))
    body = json.loads(create)
    assert re.fullmatch(r"smoke-\d+", body["name"])
    assert body["cron"] == "0 3 * * *"
    assert body["enabled"] is False
    assert body["preset_id"] == "preset-default"
    assert body["prompt"]
    preview = next(
        b for m, p, b in api.requests if (m, p) == ("POST", f"{SCHED}/schedules/preview")
    )
    assert json.loads(preview)["cron"] == "0 3 * * *"
    assert ("POST", f"{SCHED}/schedules/sch-1/run") in methods
    assert ("GET", f"{SCHED}/runs/run-1") in methods
    assert "schedule preview lists 3 fire times" in r.stdout
    assert "run run-1 is in-progress with session sid-sched" in r.stdout


def test_schedule_step_failed_run(api, fakebin):
    api.run_status = "failed"
    api.run_reason = "preset_not_found"
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "schedule: run run-1 failed: preset_not_found" in r.stderr
    assert ("DELETE", f"{SCHED}/schedules/sch-1") in api.methods()
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()
    assert ("DELETE", "/api/v1/sessions/sid-sched") not in api.methods()


def test_schedule_step_cleans_up(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    methods = api.methods()
    assert ("DELETE", "/api/v1/sessions/sid-sched") in methods
    assert ("DELETE", f"{SCHED}/schedules/sch-1") in methods
    assert "discarded schedule session sid-sched" in r.stdout
    assert "deleted schedule sch-1" in r.stdout


def test_schedule_cleanup_failure_prints_manual_line(api, fakebin):
    api.schedule_delete_status = 500
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert "delete schedule id sch-1 manually" in r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-sched") in api.methods()


def test_schedule_never_in_progress_times_out_and_cleans_up(api, fakebin):
    api.run_status = "new"
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
    assert r.returncode != 0
    assert "schedule: run run-1 not in-progress" in r.stderr
    assert ("DELETE", f"{SCHED}/schedules/sch-1") in api.methods()


def test_schedule_port_forward_to_api_container(api, fakebin):
    r = _run(
        api,
        fakebin,
        "any-mcp: x - Connected",
        port_forward=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        f"port-forward deploy/agents-schedule-api {api.port}:8000"
        in (fakebin / "kubectl.log").read_text()
    )


def _cache_posts(api):
    posts = [json.loads(b) for m, p, b in api.requests if m == "POST" and p == "/api/v1/sessions"]
    return [c for c in posts if "cache" in c["spec"]]


def test_workspace_cache_round_trip_passes(api, fakebin):
    r = _run(api, fakebin, "any-mcp: http://x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("GET", "/api/v1/meta") in api.methods()
    a, b = _cache_posts(api)
    assert a["spec"]["cache"] == {"key": "smoke-workspace-cache"} == b["spec"]["cache"]
    assert a["spec"]["replicas"] == 1 == b["spec"]["replicas"]
    assert re.fullmatch(
        r"printf '%s\\n' \"[0-9a-f]+\" > cache-marker", a["spec"]["init_scripts"][0]
    )
    assert re.fullmatch(r'grep -qx "[0-9a-f]+" cache-marker', b["spec"]["init_scripts"][0])
    assert ("DELETE", "/api/v1/sessions/sid-cache-a") in api.methods()
    assert ("DELETE", "/api/v1/sessions/sid-cache-b") in api.methods()
    assert "cache" in r.stdout and "OK" in r.stdout
    assert "cleanup failed" not in r.stderr


def test_workspace_cache_unavailable_fails(api, fakebin):
    api.cache_available = False
    r = _run(api, fakebin, "any-mcp: http://x - Connected")
    assert r.returncode != 0
    assert "workspace cache is not configured" in r.stderr
    assert _cache_posts(api) == []


def test_workspace_cache_marker_missing_fails(api, fakebin):
    api.cache_carries = False
    r = _run(api, fakebin, "any-mcp: http://x - Connected")
    assert r.returncode != 0
    assert "workspace cache did not carry the marker" in r.stderr
    assert "grep: no match" in r.stderr


def test_workspace_cache_sessions_cleaned_up(api, fakebin):
    api.cache_carries = False
    r = _run(api, fakebin, "any-mcp: http://x - Connected")
    assert r.returncode != 0
    assert ("DELETE", "/api/v1/sessions/sid-cache-a") in api.methods()
    assert ("DELETE", "/api/v1/sessions/sid-cache-b") in api.methods()
    assert all(s["deleted"] for s in api.cache_sessions.values())


def test_workspace_cache_first_session_failing_is_deleted(api, fakebin):
    api.cache_init_fails_a = True
    r = _run(api, fakebin, "any-mcp: http://x - Connected")
    assert r.returncode != 0
    assert ("DELETE", "/api/v1/sessions/sid-cache-a") in api.methods()
    assert len(_cache_posts(api)) == 1


def _matrix_requests(api):
    return [(m, p) for m, p in api.methods() if p.startswith(MATRIX)]


def test_matrix_connector_checks(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    for name in ("agents-matrix-connector-api", "agents-matrix-connector-bot"):
        assert f"wait --for=condition=Available deploy/{name}" in log
    assert _matrix_requests(api) == [
        ("GET", f"{MATRIX}/meta"),
        ("POST", f"{MATRIX}/rules"),
        ("GET", f"{MATRIX}/rules"),
        ("DELETE", f"{MATRIX}/rules/5"),
        ("GET", f"{MATRIX}/rooms"),
    ]
    sent = json.loads(next(b for m, p, b in api.requests if (m, p) == ("POST", f"{MATRIX}/rules")))
    assert sent["list"] == "deny"
    assert sent["kind"] == "user"
    assert re.fullmatch(r"@smoke-[0-9a-f]+:invalid\.example", sent["value"])
    assert api.matrix_rules == {}
    assert "config.json lists backend matrix" in r.stdout


def test_matrix_port_forward_bypasses_the_sidecar(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", port_forward=True)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    assert f"port-forward deploy/agents-matrix-connector-api {api.port}:8000" in log


@pytest.mark.parametrize(
    ("name", "setup", "needle"),
    [
        ("wrong user", lambda a: setattr(a, "matrix_user", "@someone:else"), "user_id"),
        ("sync unhealthy", lambda a: setattr(a, "matrix_sync_healthy", False), "sync"),
        ("rooms 500", lambda a: setattr(a, "matrix_rooms_status", 500), "rooms"),
        ("rule not listed", lambda a: setattr(a, "matrix_list_hides_rule", True), "rules"),
        (
            "no matrix backend",
            lambda a: setattr(
                a, "web_config", {"backends": [{"id": "orchestrator", "basePath": "/api/v1"}]}
            ),
            "matrix",
        ),
    ],
)
def test_matrix_check_failures(api, fakebin, name, setup, needle):
    setup(api)
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0, name
    assert needle in r.stderr
    assert api.matrix_rules == {}, "the trap must delete the rule"


def test_matrix_smoke_sends_nothing(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    for _, path in api.methods():
        assert "/_matrix" not in path
        assert "/send" not in path
    assert {p for m, p in _matrix_requests(api) if m == "POST"} == {f"{MATRIX}/rules"}
    text = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "/_matrix" not in text
    assert "homeserver" not in text.lower()
    assert "matrix-synapse" not in text
    assert "/send" not in text


def _option_log(fakebin):
    return (fakebin / "kubectl.log").read_text()


def test_kube_access_step(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    kube = api.option_specs["kube"]
    assert re.fullmatch(r"smoke-kube-\d+", kube["slug"])
    assert kube["spec"] == {"replicas": 1, "kube_access": True}
    log = _option_log(fakebin)
    assert re.search(r"exec pod/agent-smoke-kube-\d+-0 -c ccw -- sh -c", log), log
    for needle in (
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
        "--cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        "https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}",
        "SelfSubjectAccessReview",
        '\\"verb\\":\\"*\\",\\"group\\":\\"*\\",\\"resource\\":\\"*\\"',
        "/api/v1/namespaces",
        "test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token",
    ):
        assert needle in log, needle
    assert "kubectl" not in log.split("SelfSubjectAccessReview")[0].split("-c ccw")[-1]
    assert "meta reports kube_access.available" in r.stdout
    assert "full Kubernetes access" in r.stdout


@pytest.mark.parametrize(
    ("name", "setup", "extra_env", "needle"),
    [
        ("meta-unavailable", {"kube_available": False}, None, "kube_access.available"),
        (
            "wrong-service-account",
            None,
            {"FAKE_KUBE_SA": "agents-orchestrator-agent"},
            "-kube-access",
        ),
        (
            "token-not-mounted",
            None,
            {"FAKE_KUBE_AUTOMOUNT": "false"},
            "automountServiceAccountToken",
        ),
        ("not-allowed", None, {"FAKE_KUBE_PROBE": "false 200"}, "SelfSubjectAccessReview"),
        ("namespaces-forbidden", None, {"FAKE_KUBE_PROBE": "true 403"}, "/api/v1/namespaces"),
        ("hive-token-mounted", None, {"FAKE_HIVE_AUTOMOUNT": "true"}, "hive pod"),
        ("hive-token-file-present", None, {"FAKE_HIVE_TOKEN_PRESENT": "1"}, "hive pod"),
    ],
)
def test_kube_access_step_failures(api, fakebin, name, setup, extra_env, needle):
    for key, value in (setup or {}).items():
        setattr(api, key, value)
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env=extra_env)
    assert r.returncode != 0, f"{name}: smoke passed\n{r.stdout}"
    assert "kube access:" in r.stderr and needle in r.stderr, f"{name}: {r.stderr}"
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()
    assert ("kube" in api.option_specs) is ("sid-kube" in api.option_deleted)


def test_session_messaging_step(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    a, b = api.option_specs["msg-a"], api.option_specs["msg-b"]
    assert re.fullmatch(r"smoke-msg-a-\d+", a["slug"])
    assert re.fullmatch(r"smoke-msg-b-\d+", b["slug"])
    assert a["spec"] == {"replicas": 1, "session_messaging": True}
    assert b["spec"] == {"replicas": 1}
    ts = a["slug"].rsplit("-", 1)[1]
    log = _option_log(fakebin)
    assert re.search(
        r"exec pod/agent-smoke-msg-a-\d+-0 -c ccw -- python3 -c .* GET /agent/v1/sessions",
        log,
        re.DOTALL,
    )
    assert f"POST /agent/v1/sessions/smoke-msg-b-{ts}/messages " in log
    assert f'{{"text":"smoke ping {ts}"}}' in log
    assert f"POST /agent/v1/sessions/smoke-{ts}/messages " in log
    assert ("GET", f"/api/v1/messages/{MESSAGE_ID}") in api.methods()
    assert (
        "GET",
        "/api/v1/sessions/sid-msg-b/events?types=user_message&limit=200",
    ) in api.methods()
    assert f"message {MESSAGE_ID} reached sent" in r.stdout
    assert "403 target_not_superleader" in r.stdout


@pytest.mark.parametrize(
    ("name", "setup", "extra_env", "needle"),
    [
        ("a-does-not-list-b", None, {"FAKE_A_HIDES_B": "1"}, "lists"),
        ("send-not-accepted", None, {"FAKE_SEND_STATUS": "429"}, "202"),
        ("never-sent", {"message_status": "queued"}, None, "sent"),
        ("failed-message", {"message_status": "failed"}, None, "failed"),
        ("no-frame-in-events", {"frame_in_events": False}, None, "user_message"),
        ("b-lists-hive", None, {"FAKE_B_LISTS_HIVE": "1"}, "hive"),
        ("b-send-allowed", None, {"FAKE_B_SEND_STATUS": "202"}, "target_not_superleader"),
        ("b-send-wrong-code", None, {"FAKE_B_SEND_CODE": "not_leader"}, "target_not_superleader"),
    ],
)
def test_session_messaging_step_failures(api, fakebin, name, setup, extra_env, needle):
    for key, value in (setup or {}).items():
        setattr(api, key, value)
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env=extra_env, smoke_timeout="6")
    assert r.returncode != 0, f"{name}: smoke passed\n{r.stdout}"
    assert "messaging:" in r.stderr and needle in r.stderr, f"{name}: {r.stderr}"
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


@pytest.mark.parametrize("fail", [False, True])
def test_session_option_sessions_cleaned_up(api, fakebin, fail):
    api.message_status = "queued" if fail else "sent"
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6" if fail else "20")
    assert (r.returncode != 0) is fail, r.stdout + r.stderr
    expected = {"sid-kube", "sid-msg-a", "sid-msg-b", "sid-src"}
    assert api.option_deleted == (expected if fail else expected | {"sid-cont"})
    assert not r.stderr.count("cleanup failed")
    for kind in ("kube", "msg-a", "msg-b"):
        assert ("DELETE", f"/api/v1/sessions/sid-{kind}") in api.methods()


def test_session_option_steps_print_no_token(api, fakebin):
    for message_status in ("sent", "queued"):
        api.message_status = message_status
        api.requests.clear()
        r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
        out = r.stdout + r.stderr + _option_log(fakebin)
        assert AGENT_TOKEN not in out
        assert "ORCH_AGENT_TOKEN" not in r.stdout + r.stderr
    src = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in src if re.search(r"echo.*TOKEN|set -x|printf.*\$tok", line)]


PLUGIN_SOURCE_URL = (
    f"git::https://github.com/szymonrychu/any-mcp//examples/petstore?ref=v{ANY_MCP_VERSION}"
)


def test_orchestrator_plugin_from_plugins_repo(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    log = _option_log(fakebin)
    assert re.search(
        r"exec pod/agent-smoke-\d+-0 -c ccw -- /opt/any-mcp/bin/pip show any-mcp-tatara-orchestrator",
        log,
    ), log
    assert re.search(
        r"exec pod/agent-smoke-\d+-0 -c ccw -- cat /opt/any-mcp/etc/any-mcp.yaml", log
    ), log
    assert re.search(
        r"exec pod/agent-smoke-\d+-0 -c ccw -- /opt/any-mcp/bin/any-mcp --config /opt/any-mcp/etc/any-mcp.yaml --list-tools",
        log,
    ), log
    assert "orchestrator_sessions_list" in r.stdout


@pytest.mark.parametrize(
    "tools",
    [
        tools_json("orchestrator_sessions_list", "orchestrator_sessions_get"),
        tools_json("orchestrator_sessions_list", description="like orchestrator_activity"),
        tools_json("orchestrator_sessions_list", "orchestrator_activity_log"),
    ],
    ids=["absent", "name-only-in-a-description", "name-is-a-prefix-match"],
)
def test_plugin_tools_require_activity(api, fakebin, tools):
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env={"FAKE_TOOLS": tools})
    assert r.returncode != 0, r.stdout
    assert "smoke: FAIL: plugin tools:" in r.stderr and "orchestrator_activity" in r.stderr, (
        r.stderr
    )
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_plugin_tools_list_reports_activity(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--list-tools lists orchestrator_sessions_list and orchestrator_activity" in r.stdout


@pytest.mark.parametrize(
    ("name", "env", "needle"),
    [
        ("pip-show", {"FAKE_PIP_FAIL": "1"}, "any-mcp-tatara-orchestrator is not installed"),
        (
            "wrong-ref",
            {"FAKE_POD_CONFIG": POD_CONFIG.replace(PLUGINS_REF, "v9.9.9")},
            f"names no {PLUGINS_SOURCE}",
        ),
        (
            "old-source",
            {"FAKE_POD_CONFIG": "plugins:\n  orchestrator:\n    source: x\n"},
            "names no",
        ),
        (
            "no-tool",
            {"FAKE_TOOLS": tools_json("orchestrator_sessions_get")},
            "orchestrator_sessions_list",
        ),
        (
            "name-only-in-description",
            {
                "FAKE_TOOLS": tools_json(
                    "orchestrator_sessions_get", description="like orchestrator_sessions_list"
                )
            },
            "orchestrator_sessions_list",
        ),
        (
            "name-is-a-prefix-match",
            {"FAKE_TOOLS": tools_json("orchestrator_sessions_list_all")},
            "orchestrator_sessions_list",
        ),
    ],
)
def test_orchestrator_plugin_check_failures(api, fakebin, name, env, needle):
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env=env)
    assert r.returncode != 0, f"{name}: smoke passed\n{r.stdout}"
    assert "smoke: FAIL: plugin tools:" in r.stderr and needle in r.stderr, f"{name}: {r.stderr}"
    assert ("DELETE", "/api/v1/sessions/sid-1") in api.methods()


def test_extra_plugin_source_step(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    src = api.option_specs["src"]
    assert re.fullmatch(r"smoke-src-\d+", src["slug"])
    assert src["spec"] == {"replicas": 1, "any_mcp_sources": [PLUGIN_SOURCE_URL]}
    assert re.search(
        r"exec pod/agent-smoke-src-\d+-0 -c ccw -- /opt/any-mcp/bin/any-mcp --config /opt/any-mcp/etc/any-mcp.yaml --list-tools",
        _option_log(fakebin),
    )
    assert "petstore_ tools" in r.stdout


def test_extra_plugin_source_without_petstore_tool_fails(api, fakebin):
    r = _run(
        api,
        fakebin,
        "any-mcp: x - Connected",
        extra_env={"FAKE_SRC_TOOLS": tools_json("other_tool")},
    )
    assert r.returncode != 0, r.stdout
    assert "smoke: FAIL: extra source:" in r.stderr and "petstore_" in r.stderr, r.stderr


@pytest.mark.parametrize("fail", [False, True])
def test_plugin_source_session_cleaned_up(api, fakebin, fail):
    env = {"FAKE_SRC_TOOLS": tools_json("other_tool")} if fail else None
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env=env)
    assert (r.returncode != 0) is fail, r.stdout + r.stderr
    assert "sid-src" in api.option_deleted
    assert not r.stderr.count("cleanup failed")


@pytest.mark.parametrize("fail", [False, True])
def test_plugin_steps_print_no_token(api, fakebin, fail):
    env = {
        "FAKE_POD_AGENT_TOKEN": AGENT_TOKEN,
        "FAKE_TOOLS": tools_json(
            "orchestrator_sessions_list", "orchestrator_activity", description=AGENT_TOKEN
        ),
        "FAKE_SRC_TOOLS": tools_json(
            "other_tool" if fail else "petstore_x", description=AGENT_TOKEN
        ),
        "FAKE_POD_CONFIG": POD_CONFIG + f"leak: {AGENT_TOKEN}\n",
    }
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env=env)
    assert (r.returncode != 0) is fail, r.stdout + r.stderr
    assert AGENT_TOKEN not in r.stdout + r.stderr
    assert "ORCH_AGENT_TOKEN" not in r.stdout + r.stderr


def test_petstore_name_only_in_a_description_does_not_count(api, fakebin):
    src = tools_json("other_tool", description="petstore_list_pets lives elsewhere")
    r = _run(api, fakebin, "any-mcp: x - Connected", extra_env={"FAKE_SRC_TOOLS": src})
    assert r.returncode != 0, r.stdout
    assert "smoke: FAIL: extra source:" in r.stderr, r.stderr


def _cont_posts(api):
    return [json.loads(b) for m, p, b in api.requests if (m, p) == ("POST", CONT_MESSAGES)]


def _deletes(fakebin):
    log = (fakebin / "kubectl.log").read_text()
    return [line for line in log.splitlines() if " delete " in line]


def test_session_continuity_after_pod_restart(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    cont = api.option_specs["cont"]
    assert re.fullmatch(r"smoke-cont-\d+", cont["slug"])
    assert cont["spec"] == {"replicas": 1}
    # a real turn first, then the restart of that session's pod, then a second message
    posts = _cont_posts(api)
    assert len(posts) == 2 and all(p["text"] for p in posts)
    methods = api.methods()
    first_wait = methods.index(("GET", f"/api/v1/messages/{CONT_MESSAGE_IDS[0]}"))
    second_wait = methods.index(("GET", f"/api/v1/messages/{CONT_MESSAGE_IDS[1]}"))
    assert first_wait < second_wait
    events = f"/api/v1/sessions/sid-cont/events?agent_id={CONT_AGENT}"
    assert ("GET", f"{events}&limit=200") in methods
    assert ("GET", f"{events}&after_id=3&limit=200") in methods
    deletes = _deletes(fakebin)
    assert len(deletes) == 1
    assert re.search(rf"delete pod/agent-{cont['slug']}-0 --wait=false$", deletes[0]), deletes
    log = (fakebin / "kubectl.log").read_text()
    ready = f"wait --for=condition=Ready pod/agent-{cont['slug']}-0 "
    assert ready in log.split(deletes[0])[1]
    assert ("DELETE", "/api/v1/sessions/sid-cont") in methods
    out = r.stdout
    assert re.search(
        r"continuity: pod agent-smoke-cont-\d+-0 returned Ready with a new uid", out
    ), out
    assert f"continuity: agent {CONT_AGENT} is not dead after the restart" in out
    assert "continuity: message 52 reached sent after the restart" in out
    assert f"continuity: wrapper session id {CONT_WRAPPER} unchanged after the restart" in out
    assert "uid-cont-old" not in out and "uid-cont-new" not in out


def test_continuity_new_wrapper_session_fails(api, fakebin):
    api.cont_wrapper_after_restart = "wrap-2"
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0, r.stdout
    assert "smoke: FAIL: continuity: wrapper session id changed" in r.stderr, r.stderr
    assert "wrap-2" in r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-cont") in api.methods()


def test_continuity_no_events_after_restart_fails(api, fakebin):
    api.cont_events_after_restart = False
    started = time.monotonic()
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
    assert r.returncode != 0, r.stdout
    assert "smoke: FAIL: continuity: no event from the agent after the restart" in r.stderr, (
        r.stderr
    )
    assert time.monotonic() - started < 50
    assert ("DELETE", "/api/v1/sessions/sid-cont") in api.methods()


def test_continuity_dead_agent_fails(api, fakebin):
    api.cont_state_after_restart = "dead"
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
    assert r.returncode != 0, r.stdout
    assert re.search(
        r"smoke: FAIL: continuity: agent .* still dead 6s after the pod restart", r.stderr
    ), r.stderr
    assert len(_cont_posts(api)) == 1, "no second message goes to a dead agent"
    assert ("DELETE", "/api/v1/sessions/sid-cont") in api.methods()


def test_continuity_message_not_delivered_fails(api, fakebin):
    api.cont_second_status = "queued"
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
    assert r.returncode != 0, r.stdout
    assert re.search(
        r"smoke: FAIL: continuity: message 52 not sent within 6s after the restart \(status 'queued'\)",
        r.stderr,
    ), r.stderr
    assert ("DELETE", "/api/v1/sessions/sid-cont") in api.methods()


def test_continuity_pod_not_recreated_fails(api, fakebin):
    env = {"FAKE_CONT_KEEP_UID": "1"}
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6", extra_env=env)
    assert r.returncode != 0, r.stdout
    assert re.search(r"smoke: FAIL: continuity: .* has no new uid within 6s", r.stderr), r.stderr
    assert len(_cont_posts(api)) == 1
    assert ("DELETE", "/api/v1/sessions/sid-cont") in api.methods()


def test_continuity_deletes_only_smoke_pod(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    slug = api.option_specs["cont"]["slug"]
    (line,) = _deletes(fakebin)
    assert line.split()[-3:] == ["delete", f"pod/agent-{slug}-0", "--wait=false"], line
    # the hive session's pod and every other smoke session's pod are never deleted
    assert not re.search(r"pod/agent-smoke-(\d+|kube|msg|src)", line)
    # the only kubectl delete in the script targets the continuity pod, guarded by the smoke-cont name
    src = [x for x in SCRIPT.read_text().splitlines() if not x.lstrip().startswith("#")]
    deletes = [x for x in src if re.search(r"\bkubectl\b.*\bdelete\b", x)]
    assert len(deletes) == 1 and "$cont_pod" in deletes[0], deletes
    assert any(re.search(r'"pod/agent-smoke-cont-"\*"-0"\)', x) for x in src)


def test_smoke_scripts_share_helpers():
    # smoke.sh and chat-smoke.sh source one helper file and define no copy of agent_call or agent_py.
    scripts = ROOT / "scripts"
    lib = scripts / "lib" / "smoke-common.sh"
    assert lib.is_file()
    lib_src = lib.read_text()
    assert re.search(r"^agent_call\(\) \{", lib_src, re.MULTILINE)
    assert re.search(r"^agent_py=", lib_src, re.MULTILINE)
    for name in ("smoke.sh", "chat-smoke.sh"):
        src = [
            line
            for line in (scripts / name).read_text().splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert any(re.match(r"^source .*lib/smoke-common\.sh", line) for line in src), name
        assert not [line for line in src if re.match(r"^(agent_call\(\)|agent_py=)", line)], name

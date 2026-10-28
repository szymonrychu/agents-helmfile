"""scripts/chat-smoke.sh offline: one fake HTTP server plays the orchestrator public API, the
orchestrator agent chat routes and the connector UI API; a fake kubectl on PATH forwards
`kubectl exec <pod> -c ccw -- python3 -c <script> <method> <path> [body]` to the agent routes.

The fake bodies follow the committed contracts on agents-matrix-connector main (edfb876):
`openapi-agent.json` (ChatRooms, ChatRoom, SendAccepted, SendStatus, ErrorBody) and `openapi.json`
(RoomDetail, BindingView, SessionView, AccessView, MessageView), relayed unchanged by the
orchestrator agent routes `/agent/v1/chat/*` (agents-orchestrator README, "Chat").
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "chat-smoke.sh"
SMOKE = ROOT / "scripts" / "smoke.sh"
ROOM = "!iLWrAwaTMYnINJsyNv:szymonrichert.pl"
OTHER_ROOM = "!otherRoomXyz:szymonrichert.pl"
MATRIX = "/matrix/api/v1"
BOUND_ID = "5b7e0000-0000-4000-8000-000000000005"
BOUND_SLUG = "mx-5"
THROWAWAY_ID = "7a1e0000-0000-4000-8000-000000000007"
AGENT_TOKEN = "agent-token-SECRET-value-789"
CHAT_TOKEN = "chat-token-SECRET-value-321"
THUMBS_UP = "\U0001f44d"

# Required keys of the committed schemas (agents-matrix-connector main edfb876).
CONTRACT = {
    "ChatRoom": {"room_id", "name", "kind", "bound", "agents_may_initiate"},
    "SendAccepted": {"id", "status"},
    "SendStatus": {"id", "kind", "status", "reason", "room_id", "created_at", "sent_at"},
    "ErrorDetail": {"code", "message"},
    "RoomDetail": {
        "room_id",
        "name",
        "alias",
        "kind",
        "encrypted",
        "message_request",
        "membership",
        "member_count",
        "access",
        "preset_id",
        "trigger",
        "effective_trigger",
        "agents_may_initiate",
        "session",
        "binding",
        "last_event_at",
        "queued",
        "members",
    },
    "BindingView": {"session_id", "slug", "origin", "since"},
    "MessageView": {
        "direction",
        "id",
        "at",
        "sender",
        "sender_name",
        "body",
        "status",
        "reason",
        "run_id",
    },
}


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


# Stands in for the in-pod python3 helper: forwards the method, path and body to the fake agent
# routes with the pod name, prints "<status> <body>" like the real helper, and writes the pod's
# own secret to stderr the way a noisy pod might; the script must never show it.
FAKE_EXEC = r"""
import os, sys, urllib.request, urllib.error
args = sys.argv[1:]
pod = next(a for a in args if a.startswith("pod/"))
container = args[args.index("-c") + 1] if "-c" in args[: args.index("--")] else ""
rest = args[args.index("--") + 1 :]
assert rest[:2] == ["python3", "-c"], rest[:2]
script = rest[2]
method, path = rest[3], rest[4]
data = rest[5].encode() if len(rest) > 5 else None
sys.stderr.write("ORCH_AGENT_TOKEN=" + os.environ["FAKE_POD_AGENT_TOKEN"] + "\n")
headers = {"X-Fake-Pod": pod, "X-Fake-Container": container}
headers["X-Fake-Env"] = ",".join(sorted(set(__import__("re").findall(r"ORCH_[A-Z_]+", script))))
if data:
    headers["Content-Type"] = "application/json"
req = urllib.request.Request(
    "http://127.0.0.1:" + os.environ["FAKE_API_PORT"] + path, data=data, method=method, headers=headers
)
try:
    resp = urllib.request.urlopen(req, timeout=15)
    status, body = resp.status, resp.read()
except urllib.error.HTTPError as e:
    status, body = e.code, e.read()
print(status, " ".join(body.decode().split()))
"""

FAKE_KUBECTL = r"""#!/bin/sh
echo "$*" >> "$FAKE_KUBECTL_LOG"
case "$*" in
  *" port-forward "*)
    echo "$$" >> "$FAKE_STATE_DIR/pf-started"
    trap 'kill "$child" 2> /dev/null; echo "$$" >> "$FAKE_STATE_DIR/pf-killed"; exit 0' TERM INT
    sleep 60 &
    child=$!
    wait "$child"
    exit 0 ;;
  *" exec "*"/agent/v1/chat/"*) exec python3 "$FAKE_EXEC" "$@" ;;
  *" exec "*) exit 1 ;;
  *" wait "*) exit 0 ;;
  *"get pods"*)
    slug=$(echo "$*" | sed -n "s|.*agents.szymonrichert.pl/session=\([^ ]*\).*|\1|p")
    [ -n "$slug" ] || exit 0
    echo "pod/agent-${slug}-init-abcde"; echo "pod/agent-${slug}-0"
    [ "$slug" != "${FAKE_TWO_PODS_SLUG:-@none@}" ] || echo "pod/agent-${slug}-1"
    exit 0 ;;
esac
exit 0
"""


class _Fake:
    def __init__(self, bound: bool):
        self.requests: list[tuple[str, str, bytes, dict]] = []
        self.listed = True
        self.binding: dict | None = (
            {
                "session_id": BOUND_ID,
                "slug": BOUND_SLUG,
                "origin": "inbound",
                "since": "2026-10-27T09:00:00.123456Z",
            }
            if bound
            else None
        )
        self.messages: list[dict] = []
        # Per step (1 = S1 ... 4 = S4): seconds until the send settles, final status, reason;
        # or ("refuse", http status, error code) for a refusal at the api.
        self.plan: dict[int, tuple] = {i: (0, "sent", None) for i in (1, 2, 3, 4)}
        self.sends: dict[int, dict] = {}
        self.deleted: set[str] = set()
        # GETs of the room after the throwaway session's delete until its binding is released
        # (None: never released).
        self.release_after_polls: int | None = 1
        self.polls_after_delete = 0
        self.presets = [
            {
                "id": "preset-other",
                "name": "other",
                "is_default": False,
                "spec": {"replicas": 3},
                "created_at": "2026-10-20T10:00:00Z",
                "updated_at": "2026-10-20T10:00:00Z",
            },
            {
                "id": "preset-default",
                "name": "Default",
                "is_default": True,
                "spec": {"replicas": 1, "max_replicas": 2, "model": "opus"},
                "created_at": "2026-10-20T10:00:00Z",
                "updated_at": "2026-10-20T10:00:00Z",
            },
        ]
        self.throwaway_slug: str | None = None
        self.throwaway_spec: dict | None = None
        self.post_seen = threading.Event()
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                data = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                with fake.lock:
                    fake.requests.append((self.command, self.path, body, dict(self.headers)))
                return body

            def do_GET(self):
                self._record()
                with fake.lock:
                    code, body = fake.get(self.path)
                self._send(code, body)

            def do_POST(self):
                body = self._record()
                with fake.lock:
                    code, out = fake.post(self.path, body, self.headers.get("X-Fake-Pod", ""))
                self._send(code, out)

            def do_DELETE(self):
                self._record()
                with fake.lock:
                    code, out = fake.delete(self.path)
                self._send(code, out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # --- orchestrator public API -------------------------------------------------------------

    def sessions(self) -> dict[str, dict]:
        out = {
            BOUND_ID: {
                "slug": BOUND_SLUG,
                "hive": True,
                "max_replicas": 2,
                "leader_agent_id": "agent-b1",
                "agents": [("agent-b0", 0, False), ("agent-b1", 1, True)],
            }
        }
        if self.throwaway_slug:
            out[THROWAWAY_ID] = {
                "slug": self.throwaway_slug,
                "hive": False,
                "max_replicas": 1,
                "leader_agent_id": None,
                "agents": [("agent-t0", 0, False)],
            }
        return {k: v for k, v in out.items() if k not in self.deleted}

    def session_view(self, sid: str, s: dict) -> dict:
        return {
            "id": sid,
            "slug": s["slug"],
            "spec": {"replicas": 1, "max_replicas": s["max_replicas"]},
            "desired": "active",
            "status": {},
            "generation": 1,
            "observed_generation": 1,
            "created_at": "2026-10-27T09:00:00.123456Z",
            "preset_id": None,
            "preset_name": None,
            "hive": s["hive"],
            "max_replicas": s["max_replicas"],
            "static": False,
            "leader_agent_id": s["leader_agent_id"],
            "leader_epoch": 3 if s["leader_agent_id"] else None,
            "leader_since": "2026-10-27T09:01:00Z" if s["leader_agent_id"] else None,
            "leader_reason": "initial" if s["leader_agent_id"] else None,
            "closes_at": None,
        }

    def agents_view(self, s: dict) -> list[dict]:
        return [
            {
                "id": aid,
                "ordinal": ordinal,
                "pod_name": f"agent-{s['slug']}-{ordinal}",
                "ready": True,
                "state": "idle",
                "free_since": "2026-10-27T09:02:00Z",
                "current_turn_id": None,
                "context": None,
                "context_pct": None,
                "leader": leader,
                "peers": [],
                "gone": False,
            }
            for aid, ordinal, leader in s["agents"]
        ]

    # --- connector UI API --------------------------------------------------------------------

    def room_detail(self) -> dict:
        return {
            "room_id": ROOM,
            "name": "Szymon Richert",
            "alias": None,
            "kind": "dm",
            "encrypted": False,
            "message_request": False,
            "membership": "join",
            "member_count": 2,
            "access": {"state": "allowed", "reason": "rule", "rule_id": 1},
            "preset_id": None,
            "trigger": None,
            "effective_trigger": "all",
            "agents_may_initiate": True,
            "session": None,
            "binding": self.binding,
            "last_event_at": _iso(datetime.now(UTC)),
            "queued": 0,
            "members": [],
        }

    def room_messages(self, query: str) -> list[dict]:
        params = parse_qs(query)
        limit = int(params.get("limit", ["50"])[0])
        feed = self.messages
        if "before_id" in params:
            ids = [m["id"] for m in feed]
            feed = feed[ids.index(params["before_id"][0]) + 1 :]
        return feed[:limit]

    # --- agent chat routes (through the fake exec) -------------------------------------------

    def send_status(self, n: int) -> dict:
        send = self.sends[n]
        delay, status, reason = send["plan"]
        if time.monotonic() - send["created"] < delay:
            status, reason = "sending", None
        return {
            "id": f"s{n}",
            "kind": send["kind"],
            "status": status,
            "reason": reason,
            "room_id": ROOM,
            "created_at": "2026-10-28T10:00:00.123456Z",
            "sent_at": "2026-10-28T10:00:01.123456Z" if status == "sent" else None,
        }

    def get(self, raw: str):
        path, _, query = raw.partition("?")
        path = unquote(path)
        if path == "/healthz":
            return 200, {"status": "ok"}
        if path == "/api/v1/presets":
            return 200, self.presets
        m = re.fullmatch(r"/api/v1/sessions/([^/]+)(/agents)?", path)
        if m:
            s = self.sessions().get(m[1])
            if s is None:
                return 404, {"error": {"code": "not_found", "message": "no such session"}}
            return 200, self.agents_view(s) if m[2] else self.session_view(m[1], s)
        if path == f"{MATRIX}/rooms/{ROOM}":
            if THROWAWAY_ID in self.deleted and self.binding is not None:
                self.polls_after_delete += 1
                done = self.release_after_polls
                if done is not None and self.polls_after_delete > done:
                    self.binding = None
            return 200, self.room_detail()
        if path == f"{MATRIX}/rooms/{ROOM}/messages":
            return 200, self.room_messages(query)
        if path == "/agent/v1/chat/rooms":
            rooms = [{"room_id": OTHER_ROOM, "name": "other", "kind": "group"}]
            if self.listed:
                rooms.insert(0, {"room_id": ROOM, "name": "Szymon Richert", "kind": "dm"})
            for r in rooms:
                r["bound"] = self.binding is not None and r["room_id"] == ROOM
                r["agents_may_initiate"] = True
            return 200, {"rooms": rooms}
        m = re.fullmatch(r"/agent/v1/chat/messages/s(\d+)", path)
        if m and int(m[1]) in self.sends:
            return 200, self.send_status(int(m[1]))
        return 404, {"error": {"code": "not_found", "message": "not found"}}

    def post(self, path: str, body: bytes, pod: str):
        if path == "/api/v1/sessions":
            created = json.loads(body)
            self.throwaway_slug = created["slug"]
            self.throwaway_spec = created.get("spec")
            return 201, self.session_view(THROWAWAY_ID, self.sessions()[THROWAWAY_ID])
        if path in ("/agent/v1/chat/messages", "/agent/v1/chat/reactions"):
            self.post_seen.set()
            step = len(self.sends) + 1
            plan = self.plan[step]
            if plan[0] == "refuse":
                self.sends[1000 + step] = {"refused": True}
                return plan[1], {"error": {"code": plan[2], "message": "refused"}}
            n = len([k for k in self.sends if k < 1000]) + 1
            kind = "message" if path.endswith("messages") else "reaction"
            self.sends[n] = {
                "kind": kind,
                "body": json.loads(body),
                "created": time.monotonic(),
                "plan": plan,
                "pod": pod,
            }
            if (
                self.binding is None
                and self.throwaway_slug
                and pod.endswith(f"agent-{self.throwaway_slug}-0")
            ):
                self.binding = {
                    "session_id": THROWAWAY_ID,
                    "slug": self.throwaway_slug,
                    "origin": "agent",
                    "since": _iso(datetime.now(UTC)),
                }
            return 202, {"id": f"s{n}", "status": "queued"}
        return 404, {"error": {"code": "not_found", "message": "not found"}}

    def delete(self, path: str):
        sid = path.rsplit("/", 1)[1]
        if path.startswith("/api/v1/sessions/") and sid in self.sessions():
            self.deleted.add(sid)
            return 202, {"id": sid, "desired": "deleting"}
        return 404, {"error": {"code": "not_found", "message": "not found"}}

    # --- assertions helpers ------------------------------------------------------------------

    def calls(self, method: str | None = None) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _, _ in self.requests if method is None or m == method]

    def agent_calls(self) -> list[tuple[str, str, str, str, str]]:
        return [
            (m, p, h.get("X-Fake-Pod", ""), h.get("X-Fake-Container", ""), h.get("X-Fake-Env", ""))
            for m, p, _, h in self.requests
            if p.startswith("/agent/v1/")
        ]

    def send_bodies(self) -> list[tuple[str, dict]]:
        return [
            (p, json.loads(b))
            for m, p, b, _ in self.requests
            if m == "POST" and p.startswith("/agent/v1/chat/")
        ]


@pytest.fixture
def make(tmp_path):
    made: list[_Fake] = []

    def factory(bound: bool) -> _Fake:
        fake = _Fake(bound)
        made.append(fake)
        return fake

    yield factory
    for fake in made:
        fake.server.shutdown()


@pytest.fixture
def fakebin(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    k = b / "kubectl"
    k.write_text(FAKE_KUBECTL)
    k.chmod(0o755)
    (b / "fake_exec.py").write_text(FAKE_EXEC)
    (tmp_path / "state").mkdir()
    return b


def _env(fake: _Fake, fakebin: Path, timeout: str = "8", extra: dict | None = None) -> dict:
    state = fakebin.parent / "state"
    return {
        **{k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")},
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "SMOKE_LOCAL_PORT": str(fake.port),
        "SMOKE_MATRIX_API_LOCAL_PORT": str(fake.port),
        "CHAT_SMOKE_TIMEOUT_S": timeout,
        "FAKE_API_PORT": str(fake.port),
        "FAKE_EXEC": str(fakebin / "fake_exec.py"),
        "FAKE_KUBECTL_LOG": str(fakebin / "kubectl.log"),
        "FAKE_STATE_DIR": str(state),
        "FAKE_TWO_PODS_SLUG": BOUND_SLUG,
        "FAKE_POD_AGENT_TOKEN": AGENT_TOKEN,
        # An operator running the script from inside an agent pod has its own agent env.
        "ORCH_AGENT_TOKEN": AGENT_TOKEN,
        "ORCH_AGENT_URL": "http://agents-orchestrator-agents:8001",
        "ORCH_SESSION_ID": "0c1d0000-0000-4000-8000-0000000000aa",
        "ORCH_CHAT_TOKEN": CHAT_TOKEN,
        **(extra or {}),
    }


def _run(fake: _Fake, fakebin: Path, timeout: str = "8", extra: dict | None = None):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        env=_env(fake, fakebin, timeout, extra),
        capture_output=True,
        text=True,
        timeout=90,
    )


def _lines(r) -> list[str]:
    return r.stdout.splitlines()


def _index(lines: list[str], pattern: str) -> int:
    for i, line in enumerate(lines):
        if re.fullmatch(pattern, line):
            return i
    raise AssertionError(f"no line matching {pattern!r} in:\n" + "\n".join(lines))


def _pf_pids(fakebin: Path, name: str) -> set[str]:
    path = fakebin.parent / "state" / name
    return set(path.read_text().split()) if path.exists() else set()


def _assert_port_forwards_gone(fakebin: Path) -> None:
    started = _pf_pids(fakebin, "pf-started")
    assert started, "the script started no port-forward"
    # The trap's kill reaches the fake before the script exits; allow its marker to land.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not started <= _pf_pids(fakebin, "pf-killed"):
        time.sleep(0.1)
    assert started <= _pf_pids(fakebin, "pf-killed"), (started, _pf_pids(fakebin, "pf-killed"))


def test_s1_to_s4_happy_path(make, fakebin):
    fake = make(bound=True)
    r = _run(fake, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _lines(r)
    rooms = [i for i, (m, p, *_) in enumerate(fake.agent_calls()) if p == "/agent/v1/chat/rooms"]
    posts = [(p, b) for p, b in fake.send_bodies()]
    assert rooms and rooms[0] == 0, fake.agent_calls()
    assert [p for p, _ in posts] == [
        "/agent/v1/chat/messages",
        "/agent/v1/chat/messages",
        "/agent/v1/chat/reactions",
        "/agent/v1/chat/reactions",
    ]
    s1, s2, s3, s4 = (b for _, b in posts)
    assert s1["room"] == ROOM and "reply_to" not in s1 and s1["text"]
    assert s2["reply_to"] == "s1" and s2["text"]
    assert s3 == {"to": "s1", "key": THUMBS_UP}
    assert s4["to"] == "s1" and s4["key"] != THUMBS_UP and s4["key"]
    # Each send's status is polled after that send and before the next one.
    paths = [p for _, p, *_ in fake.agent_calls()]
    post_at = [i for i, p in enumerate(paths) if p in {x for x, _ in posts}]
    for n in (1, 2, 3, 4):
        first_poll = paths.index(f"/agent/v1/chat/messages/s{n}")
        assert first_poll > post_at[n - 1], paths
        assert n == 4 or first_poll < post_at[n], paths
    order = [
        _index(lines, r"S1 s1 sent"),
        _index(lines, r"S2 s2 sent"),
        _index(lines, r"S3 s3 sent"),
        _index(lines, r"S4 s4 sent"),
    ]
    assert order == sorted(order), lines
    assert "chat-smoke: OK" in lines[-1], lines


def test_bound_dm_uses_bound_session(make, fakebin):
    fake = make(bound=True)
    r = _run(fake, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _lines(r)
    assert _index(lines, rf"binding before: {BOUND_SLUG}") < _index(lines, r"S1 s1 sent")
    assert _index(lines, rf"caller: bound {BOUND_SLUG}") < _index(lines, r"S1 s1 sent")
    assert _index(lines, rf"binding after: {BOUND_SLUG}") > _index(lines, r"S4 s4 .*")
    # The hive's leader is agent-b1 on pod-1, not pod-0: the caller is the leader pod.
    calls = fake.agent_calls()
    assert calls
    assert {pod for _, _, pod, _, _ in calls} == {f"pod/agent-{BOUND_SLUG}-1"}
    assert {c for _, _, _, c, _ in calls} == {"ccw"}
    # The in-pod helper reads the pod's own agent env.
    assert {env for *_, env in calls} == {"ORCH_AGENT_TOKEN,ORCH_AGENT_URL,ORCH_SESSION_ID"}
    # Nothing created or deleted, no message to and no change of the bound session.
    assert ("POST", "/api/v1/sessions") not in fake.calls()
    assert fake.calls("DELETE") == []
    public_posts = [p for m, p in fake.calls("POST") if p.startswith("/api/")]
    assert public_posts == [], public_posts
    assert [m for m, p in fake.calls() if m not in ("GET", "POST")] == []


def test_unbound_dm_uses_throwaway_and_waits_release(make, fakebin):
    fake = make(bound=False)
    fake.release_after_polls = 2
    r = _run(fake, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _lines(r)
    slug = fake.throwaway_slug
    assert slug and re.fullmatch(r"chat-smoke-\d+", slug), slug
    # An alone session from the default preset's spec.
    assert fake.throwaway_spec == {"replicas": 1, "max_replicas": 1, "model": "opus"}
    assert _index(lines, r"binding before: none") < _index(lines, rf"caller: throwaway {slug}")
    assert _index(lines, rf"caller: throwaway {slug}") < _index(lines, r"S1 s1 sent")
    assert _index(lines, rf"binding during: {slug}") > _index(lines, r"S1 s1 sent")
    assert _index(lines, r"binding after: none") > _index(lines, r"S4 s4 .*")
    assert {pod for _, _, pod, _, _ in fake.agent_calls()} == {f"pod/agent-{slug}-0"}
    deletes = fake.calls("DELETE")
    assert deletes == [("DELETE", f"/api/v1/sessions/{THROWAWAY_ID}")]
    assert fake.polls_after_delete == 3
    assert fake.binding is None

    # A binding still held at the release deadline exits 1 naming the step.
    fake2 = make(bound=False)
    fake2.release_after_polls = None
    r2 = _run(fake2, fakebin, timeout="4")
    assert r2.returncode == 1, r2.stdout + r2.stderr
    assert "release" in r2.stderr and fake2.throwaway_slug in r2.stderr, r2.stderr
    assert fake2.calls("DELETE") == [("DELETE", f"/api/v1/sessions/{THROWAWAY_ID}")]


def test_room_not_listed_stops_before_sending(make, fakebin):
    for bound in (True, False):
        fake = make(bound=bound)
        fake.listed = False
        r = _run(fake, fakebin)
        assert r.returncode == 1, r.stdout + r.stderr
        assert ROOM in r.stderr, r.stderr
        assert fake.send_bodies() == []
        assert not re.search(r"^S\d", r.stdout, re.MULTILINE), r.stdout
        if bound:
            assert fake.calls("DELETE") == []
        else:
            assert fake.calls("DELETE") == [("DELETE", f"/api/v1/sessions/{THROWAWAY_ID}")]


def test_each_wait_has_its_own_deadline(make, fakebin):
    # S1 and S3 each settle after 4 s of a 6 s window: together they exceed one shared window.
    fake = make(bound=True)
    fake.plan[1] = (4, "sent", None)
    fake.plan[3] = (4, "sent", None)
    r = _run(fake, fakebin, timeout="6")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "S3 s3 sent" in _lines(r)

    # A wait that passes its own deadline exits 1 naming the step.
    slow = make(bound=True)
    slow.plan[3] = (60, "sent", None)
    r = _run(slow, fakebin, timeout="3")
    assert r.returncode == 1, r.stdout + r.stderr
    assert re.search(r"S3 s3 .*3s", r.stderr), r.stderr
    assert [p for p, _ in slow.send_bodies()][-1] == "/agent/v1/chat/reactions"
    assert len(slow.send_bodies()) == 3


def test_s4_failure_is_reported_not_fatal(make, fakebin):
    failed = make(bound=True)
    failed.plan[4] = (0, "failed", "M_FORBIDDEN: reaction not allowed")
    r = _run(failed, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "S4 s4 failed M_FORBIDDEN: reaction not allowed" in _lines(r)

    refused = make(bound=True)
    refused.plan[4] = (0, "refused", "room_bound")
    r = _run(refused, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "S4 s4 refused room_bound" in _lines(r)

    at_api = make(bound=True)
    at_api.plan[4] = ("refuse", 422, "invalid_key")
    r = _run(at_api, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "S4 - refused 422 invalid_key" in _lines(r)

    for step, plan in ((1, (0, "refused", "room_bound")), (2, (0, "failed", "M_UNKNOWN: x"))):
        bad = make(bound=True)
        bad.plan[step] = plan
        r = _run(bad, fakebin)
        assert r.returncode == 1, r.stdout + r.stderr
        assert f"S{step} s{step} {plan[1]} {plan[2]}" in r.stderr, r.stderr
        assert len(bad.send_bodies()) == step

    bad = make(bound=True)
    bad.plan[3] = ("refuse", 429, "rate_limited")
    r = _run(bad, fakebin)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "S3" in r.stderr and "429 rate_limited" in r.stderr, r.stderr
    assert len(bad.send_bodies()) == 3


def _msg(i: int, direction: str, at: datetime | str) -> dict:
    return {
        "direction": direction,
        "id": f"{direction}-{i}",
        "at": at if isinstance(at, str) else _iso(at),
        "sender": "@owner:szymonrichert.pl" if direction == "in" else "@hermes:szymonrichert.pl",
        "sender_name": None,
        "body": "hi",
        "status": "handled" if direction == "in" else "sent",
        "reason": None,
        "run_id": None,
    }


def test_prints_hours_since_last_inbound(make, fakebin):
    now = datetime.now(UTC)
    fake = make(bound=True)
    # 250 newer outbound rows push the newest inbound message onto the second page.
    fake.messages = [_msg(i, "out", now - timedelta(minutes=i)) for i in range(250, 0, -1)]
    fake.messages.sort(key=lambda m: m["at"], reverse=True)
    fake.messages += [_msg(7, "in", now - timedelta(hours=30, minutes=10))]
    fake.messages += [_msg(6, "in", now - timedelta(hours=50))]
    r = _run(fake, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _lines(r)
    assert _index(lines, r"hours since last inbound: 30") < _index(lines, r"S1 s1 sent")
    room_path = f"{MATRIX}/rooms/{quote(ROOM, safe='')}/messages"
    pages = [p for m, p in fake.calls("GET") if urlsplit(p).path == room_path]
    assert len(pages) == 2, pages

    offset = make(bound=True)
    at = (now - timedelta(hours=5, minutes=5)).astimezone(timezone(timedelta(hours=2)))
    offset.messages = [_msg(1, "in", at.isoformat())]
    r = _run(offset, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "hours since last inbound: 5" in _lines(r)

    none = make(bound=True)
    none.messages = [_msg(1, "out", now)]
    r = _run(none, fakebin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _index(_lines(r), r"hours since last inbound: none") < _index(_lines(r), r"S1 s1 sent")


def test_cleanup_and_no_secrets(make, fakebin):
    # A stale port-forward on the script's port is killed first.
    fake = make(bound=False)
    env = _env(fake, fakebin)
    stale = subprocess.Popen(
        [
            str(fakebin / "kubectl"),
            "-n",
            "agents",
            "port-forward",
            "deploy/agents-orchestrator-api",
            f"{fake.port}:8000",
        ],
        env=env,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _pf_pids(fakebin, "pf-started"):
        time.sleep(0.05)
    try:
        r = _run(fake, fakebin)
        assert stale.wait(timeout=5) == 0
    finally:
        if stale.poll() is None:
            stale.kill()
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    for secret in (AGENT_TOKEN, CHAT_TOKEN, "ORCH_AGENT_TOKEN=", "ORCH_SESSION_ID="):
        assert secret not in out, secret
    assert env["ORCH_SESSION_ID"] not in out
    assert fake.calls("DELETE") == [("DELETE", f"/api/v1/sessions/{THROWAWAY_ID}")]
    _assert_port_forwards_gone(fakebin)
    log = (fakebin / "kubectl.log").read_text()
    assert f"port-forward deploy/agents-orchestrator-api {fake.port}:8000" in log
    assert f"port-forward deploy/agents-matrix-connector-api {fake.port}:8000" in log

    # On failure (S2 failed) the trap deletes the throwaway session and kills the port-forwards.
    (fakebin.parent / "state" / "pf-started").unlink()
    failing = make(bound=False)
    failing.plan[2] = (0, "failed", "M_UNKNOWN: x")
    r = _run(failing, fakebin)
    assert r.returncode == 1, r.stdout + r.stderr
    assert failing.calls("DELETE") == [("DELETE", f"/api/v1/sessions/{THROWAWAY_ID}")]
    _assert_port_forwards_gone(fakebin)
    assert AGENT_TOKEN not in r.stdout + r.stderr

    # On interrupt (SIGINT, then SIGTERM, during S1's wait) the trap does the same.
    for sig in (signal.SIGINT, signal.SIGTERM):
        (fakebin.parent / "state" / "pf-started").unlink()
        slow = make(bound=False)
        slow.plan[1] = (60, "sent", None)
        proc = subprocess.Popen(
            ["bash", str(SCRIPT)],
            env=_env(slow, fakebin, timeout="60"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert slow.post_seen.wait(timeout=30), "S1 was never posted"
        time.sleep(0.5)
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=30)
        assert proc.returncode != 0, out + err
        assert slow.calls("DELETE") == [("DELETE", f"/api/v1/sessions/{THROWAWAY_ID}")], sig
        _assert_port_forwards_gone(fakebin)

    # The bound case deletes nothing (the trap only deletes a session the script created).
    (fakebin.parent / "state" / "pf-started").unlink()
    bound = make(bound=True)
    bound.plan[2] = (0, "failed", "M_UNKNOWN: x")
    r = _run(bound, fakebin)
    assert r.returncode == 1
    assert bound.calls("DELETE") == []
    _assert_port_forwards_gone(fakebin)


def test_stale_port_forward_found_through_ps(make, fakebin):
    # Without /proc (macOS) the stale lookup reads `ps -eo pid=,args=`.
    fake = make(bound=True)
    env = _env(fake, fakebin, extra={"CHAT_SMOKE_PROC_DIR": str(fakebin.parent / "no-proc")})
    stale = subprocess.Popen(
        [str(fakebin / "kubectl"), "-n", "agents", "port-forward", "deploy/x", f"{fake.port}:8000"],
        env=env,
    )
    other = subprocess.Popen(["sleep", "30"])
    ps = fakebin / "ps"
    ps.write_text(
        "#!/bin/sh\n"
        '[ "$*" = "-eo pid=,args=" ] || exit 2\n'
        # A killed stale process leaves the listing (its zombie has an empty cmdline until reaped).
        f"tr -d '\\000' < /proc/{stale.pid}/cmdline | grep -q . &&"
        f' echo "  {stale.pid} /bin/sh {fakebin}/kubectl'
        f' -n agents port-forward deploy/x {fake.port}:8000"\n'
        f'echo "  {other.pid} kubectl -n agents port-forward deploy/x 1{fake.port}:8000"\n'
    )
    ps.chmod(0o755)
    try:
        r = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=90, check=False
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert stale.wait(timeout=5) == 0
        assert other.poll() is None
        assert f"killed stale port-forward {stale.pid} on port {fake.port}" in r.stdout
    finally:
        for proc in (stale, other):
            if proc.poll() is None:
                proc.kill()


def test_fake_bodies_match_connector_contract(make):
    fake = make(bound=True)
    assert set(fake.room_detail()) == CONTRACT["RoomDetail"]
    assert set(fake.binding) == CONTRACT["BindingView"]
    assert set(_msg(1, "in", datetime.now(UTC))) == CONTRACT["MessageView"]
    code, rooms = fake.get("/agent/v1/chat/rooms")
    assert code == 200 and set(rooms) == {"rooms"}
    assert all(set(r) == CONTRACT["ChatRoom"] for r in rooms["rooms"])
    code, accepted = fake.post("/agent/v1/chat/messages", b'{"text": "x", "room": "r"}', "p")
    assert code == 202 and set(accepted) == CONTRACT["SendAccepted"]
    assert set(fake.send_status(1)) == CONTRACT["SendStatus"]
    code, err = fake.get("/agent/v1/chat/messages/s99")
    assert code == 404 and set(err) == {"error"} and set(err["error"]) == CONTRACT["ErrorDetail"]


def test_smoke_never_sends_chat():
    body = SMOKE.read_text()
    assert "/agent/v1/chat" not in body
    assert "chat-smoke" not in body

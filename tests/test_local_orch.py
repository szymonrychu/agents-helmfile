"""local/orch.py against an in-process fake orchestrator (http.server on a thread)."""

import importlib.util
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SPEC = importlib.util.spec_from_file_location(
    "local_orch", Path(__file__).resolve().parent.parent / "local" / "orch.py"
)
orch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(orch)


def sess(slug, preset_id, desired="active"):
    return {"id": str(uuid.uuid4()), "slug": slug, "preset_id": preset_id, "desired": desired}


@pytest.mark.parametrize(
    ("sessions", "n", "expected"),
    [
        ([], 2, (2, [])),
        ([("a1",), ("a2",), ("a3",)], 1, (0, ["a3", "a2"])),
        ([("a1",), ("a2",)], 2, (0, [])),
        ([("a1",), ("a2", "deleting")], 2, (1, [])),
        ([("a1",), ("x1", "active", "other")], 1, (0, [])),
        ([("a1",), ("x1", "active", "other")], 2, (1, [])),
    ],
)
def test_plan_hives(sessions, n, expected):
    rows = [
        {
            "id": f"id-{slug}",
            "slug": slug,
            "desired": (rest + ["active"])[0],
            "preset_id": (rest + ["active", "p"])[1],
        }
        for slug, *rest in sessions
    ]
    count, discard = orch.plan_hives(rows, "p", n)
    assert (count, [s.removeprefix("id-") for s in discard]) == expected


def test_plan_rejects_negative():
    with pytest.raises(ValueError):
        orch.plan_hives([], "p", -1)


class Fake:
    def __init__(self):
        self.projects = []
        self.presets = []
        self.sessions = []
        self.agents_ready = True
        self.counter = 0
        self.seen_hosts = set()
        self.fail_paths = {}
        self.raw_paths = {}
        self.ghost = None
        self.list_calls = 0


def make_server(state):  # noqa: C901
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body=None):
            data = json.dumps(body if body is not None else {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def handle_any(self, method):  # noqa: C901, PLR0911, PLR0912
            state.seen_hosts.add(self.headers.get("Host"))
            url = urlparse(self.path)
            parts = url.path.removeprefix("/api/v1").strip("/").split("/")
            if (method, url.path) in state.fail_paths:
                return self.send(
                    state.fail_paths[method, url.path],
                    {"error": {"code": "boom", "message": "SECRETBODY"}},
                )
            if (method, url.path) in state.raw_paths:
                data = state.raw_paths[method, url.path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            # Like the orchestrator: the list omits archived projects, a create of an existing
            # slug conflicts, GET and PATCH by slug see archived ones too.
            if parts == ["projects"] and method == "GET":
                return self.send(200, [p for p in state.projects if not p["archived"]])
            if parts == ["projects"] and method == "POST":
                body = self.body()
                if any(p["slug"] == body["slug"] for p in state.projects):
                    return self.send(409, {"error": {"code": "project_exists"}})
                state.projects.append({**body, "archived": False})
                return self.send(201, state.projects[-1])
            if len(parts) == 2 and parts[0] == "projects" and method in ("GET", "PATCH"):
                for p in state.projects:
                    if p["slug"] == parts[1]:
                        p.update(self.body() if method == "PATCH" else {})
                        return self.send(200, p)
                return self.send(404, {"error": {"code": "not_found"}})
            if parts == ["presets"] and method == "GET":
                return self.send(200, state.presets)
            if parts == ["presets"] and method == "POST":
                preset = {"id": str(uuid.uuid4()), **self.body()}
                state.presets.append(preset)
                return self.send(201, preset)
            if len(parts) == 2 and parts[0] == "presets" and method == "PUT":
                for p in state.presets:
                    if p["id"] == parts[1]:
                        p.update(self.body())
                        return self.send(200, p)
                return self.send(404)
            if parts == ["sessions"] and method == "GET":
                project = parse_qs(url.query).get("project", [None])[0]
                out = [s for s in state.sessions if s["project"] == project]
                state.sessions = [s for s in state.sessions if s["desired"] != "deleting"]
                state.list_calls += 1
                if state.ghost and state.list_calls == 2:
                    out = [*out, state.ghost]
                return self.send(200, out)
            if parts == ["sessions"] and method == "POST":
                preset_id = self.body()["preset_id"]
                state.counter += 1
                s = {
                    "id": str(uuid.uuid4()),
                    "slug": f"h{state.counter}",
                    "preset_id": preset_id,
                    "desired": "active",
                    "project": "local",
                }
                state.sessions.append(s)
                return self.send(201, s)
            if len(parts) == 2 and parts[0] == "sessions" and method == "DELETE":
                for s in state.sessions:
                    if s["id"] == parts[1]:
                        s["desired"] = "deleting"
                        return self.send(202, s)
                return self.send(404)
            if len(parts) == 3 and parts[2] == "agents":
                if state.ghost and parts[1] == state.ghost["id"]:
                    return self.send(404)
                return self.send(200, [{"ready": state.agents_ready, "gone": False}])
            return self.send(404)

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_PATCH(self):
            self.handle_any("PATCH")

        def do_DELETE(self):
            self.handle_any("DELETE")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def fake():
    state = Fake()
    server = make_server(state)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


def run(fake, *args, host=None):
    pre = ["--url", fake.url] + (["--host-header", host] if host else [])
    return orch.main([*pre, *args])


def test_seed_idempotent(fake):
    assert run(fake, "seed", "--docker", "true", host="localhost") == 0
    assert run(fake, "seed", "--docker", "true") == 0
    assert [p["slug"] for p in fake.projects] == ["local"]
    assert len(fake.presets) == 1
    preset = fake.presets[0]
    assert preset["name"] == "local-hive"
    assert preset["project"] == "local"
    assert preset["spec"] == {"replicas": 1, "max_replicas": 1, "docker": True}
    assert "localhost" in fake.seen_hosts


def test_seed_restores_an_archived_project(fake):
    fake.projects.append({"slug": "local", "name": "Local", "description": "", "archived": True})
    assert run(fake, "seed", "--docker", "true") == 0
    assert [(p["slug"], p["archived"]) for p in fake.projects] == [("local", False)]


def test_seed_updates_docker_flag(fake):
    run(fake, "seed", "--docker", "true")
    run(fake, "seed", "--docker", "false")
    assert len(fake.presets) == 1
    assert fake.presets[0]["spec"]["docker"] is False


def active(fake, preset_id):
    return [s for s in fake.sessions if s["preset_id"] == preset_id and s["desired"] == "active"]


def test_hives_converge_up_and_down(fake):
    run(fake, "seed", "--docker", "false")
    preset_id = fake.presets[0]["id"]
    foreign = {
        "id": str(uuid.uuid4()),
        "slug": "zz9",
        "preset_id": "other",
        "desired": "active",
        "project": "local",
    }
    fake.sessions.append(foreign)
    assert run(fake, "hives", "2", "--wait") == 0
    assert len(active(fake, preset_id)) == 2
    assert run(fake, "hives", "2", "--wait") == 0
    assert len(active(fake, preset_id)) == 2
    assert run(fake, "hives", "1", "--wait") == 0
    assert len(active(fake, preset_id)) == 1
    assert run(fake, "hives", "0", "--wait") == 0
    assert active(fake, preset_id) == []
    assert foreign in fake.sessions and foreign["desired"] == "active"


def test_hives_requires_seed(fake, capsys):
    assert run(fake, "hives", "1") == 1
    assert "run seed first" in capsys.readouterr().err


def test_wait_timeout_exits_3(fake, capsys):
    run(fake, "seed", "--docker", "false")
    fake.agents_ready = False
    assert run(fake, "hives", "1", "--wait", "--timeout", "1", "--poll", "0.2") == 3
    assert "h1" in capsys.readouterr().err


def test_api_error_prints_no_body(fake, capsys):
    fake.fail_paths["GET", "/api/v1/projects/local"] = 500
    assert run(fake, "seed", "--docker", "true") == 1
    err = capsys.readouterr().err
    assert "GET" in err and "/api/v1/projects/local" in err and "500" in err and "boom" in err
    assert "SECRETBODY" not in err


def test_wait_tolerates_session_vanishing(fake):
    run(fake, "seed", "--docker", "false")
    preset_id = fake.presets[0]["id"]
    fake.ghost = sess("ghost1", preset_id)
    fake.ghost["project"] = "local"
    assert run(fake, "hives", "1", "--wait", "--timeout", "10", "--poll", "0.1") == 0
    assert len(active(fake, preset_id)) == 1


def test_non_json_response_exits_1(fake, capsys):
    fake.raw_paths["GET", "/api/v1/projects/local"] = b"<html>SECRETBODY</html>"
    assert run(fake, "seed", "--docker", "true") == 1
    err = capsys.readouterr().err
    assert (
        "GET" in err
        and "/api/v1/projects/local" in err
        and "200" in err
        and "invalid_response" in err
    )
    assert "SECRETBODY" not in err

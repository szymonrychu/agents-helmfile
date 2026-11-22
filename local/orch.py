"""Seed the local project and preset and scale local-hive sessions through the orchestrator API.

Stdlib only. Calls the orchestrator main port (prefix /api/v1) without an auth header.
Exit codes: 0 success, 1 API error, 2 usage error, 3 wait timeout.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

PREFIX = "/api/v1"
PROJECT = "local"
PRESET = "local-hive"


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, url, host_header=None):
        self.url = url.rstrip("/")
        self.host_header = host_header

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + PREFIX + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.host_header:
            req.add_header("Host", self.host_header)
        shown = PREFIX + path
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            code = "-"
            try:
                code = json.loads(exc.read())["error"]["code"]
            except (ValueError, KeyError, TypeError):
                pass
            raise ApiError(f"{method} {shown} -> {exc.code} {code}", exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(f"{method} {shown} -> unreachable {type(exc).__name__}") from None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise ApiError(f"{method} {shown} -> {status} invalid_response", status) from None


def _natural(slug):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", slug)]


def plan_hives(sessions, preset_id, n):
    """Return (create_count, session ids to discard) to reach n active sessions of the preset."""
    if n < 0:
        raise ValueError("n must be >= 0")
    mine = [s for s in sessions if s.get("preset_id") == preset_id and s.get("desired") == "active"]
    if len(mine) <= n:
        return n - len(mine), []
    mine.sort(key=lambda s: _natural(s["slug"]), reverse=True)
    return 0, [s["id"] for s in mine[: len(mine) - n]]


def find_preset(client):
    for preset in client.call("GET", f"/presets?project={PROJECT}"):
        if preset["name"] == PRESET and preset.get("project", PROJECT) == PROJECT:
            return preset
    return None


def seed(client, docker):
    if not any(p["slug"] == PROJECT for p in client.call("GET", "/projects")):
        client.call("POST", "/projects", {"slug": PROJECT, "name": "Local", "description": ""})
    spec = {"replicas": 1, "max_replicas": 1, "docker": docker}
    preset = find_preset(client)
    if preset is None:
        client.call("POST", "/presets", {"name": PRESET, "project": PROJECT, "spec": spec})
    elif any(preset["spec"].get(k) != v for k, v in spec.items()):
        client.call(
            "PUT",
            f"/presets/{preset['id']}",
            {"name": PRESET, "project": PROJECT, "spec": {**preset["spec"], **spec}},
        )


def hive_sessions(client, preset_id):
    rows = client.call("GET", f"/sessions?project={PROJECT}")
    return [s for s in rows if s.get("preset_id") == preset_id]


def not_ready(client, preset_id, n):
    """Slugs standing in the way of exactly n ready active hives with none deleting."""
    sessions = hive_sessions(client, preset_id)
    waiting = [s["slug"] for s in sessions if s["desired"] != "active"]
    active = []
    for s in (s for s in sessions if s["desired"] == "active"):
        try:
            rows = client.call("GET", f"/sessions/{s['id']}/agents")
        except ApiError as exc:
            if exc.status == 404:
                continue
            raise
        active.append(s)
        agents = [a for a in rows if not a.get("gone")]
        if not agents or not all(a["ready"] for a in agents):
            waiting.append(s["slug"])
    if len(active) != n and not waiting:
        waiting.append(f"(want {n} active, have {len(active)})")
    return waiting


def hives(client, n, wait, timeout, poll):
    preset = find_preset(client)
    if preset is None:
        raise ApiError(f"preset {PRESET} not found: run seed first")
    create, discard = plan_hives(hive_sessions(client, preset["id"]), preset["id"], n)
    for _ in range(create):
        client.call("POST", "/sessions", {"preset_id": preset["id"]})
    for session_id in discard:
        client.call("DELETE", f"/sessions/{session_id}")
    if not wait:
        return 0
    deadline = time.monotonic() + timeout
    while True:
        waiting = not_ready(client, preset["id"], n)
        if not waiting:
            return 0
        if time.monotonic() >= deadline:
            print("timeout, not ready: " + ", ".join(waiting), file=sys.stderr)
            return 3
        time.sleep(poll)


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="orch.py")
    parser.add_argument("--url", required=True)
    parser.add_argument("--host-header")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_seed = sub.add_parser("seed")
    p_seed.add_argument("--docker", required=True, choices=["true", "false"])
    p_hives = sub.add_parser("hives")
    p_hives.add_argument("n", type=int)
    p_hives.add_argument("--wait", action="store_true")
    p_hives.add_argument("--timeout", type=float, default=900)
    p_hives.add_argument("--poll", type=float, default=5)
    return parser.parse_args(argv)


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.cmd == "hives" and args.n < 0:
        print("n must be >= 0", file=sys.stderr)
        return 2
    client = Client(args.url, args.host_header)
    try:
        if args.cmd == "seed":
            seed(client, args.docker == "true")
            return 0
        return hives(client, args.n, args.wait, args.timeout, args.poll)
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

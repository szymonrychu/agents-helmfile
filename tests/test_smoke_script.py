"""scripts/smoke.sh offline: fake HTTP API and a fake kubectl on PATH."""

import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "smoke.sh"


class _Api:
    def __init__(self):
        self.requests: list[tuple[str, str, bytes]] = []
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

            def do_GET(self):
                self._record()
                if self.path == "/":
                    self._send(200, "<!doctype html><html></html>", "text/html")
                elif self.path == "/api/v1/openapi.json":
                    self._send(200, "{}")
                else:
                    self._send(404, "{}")

            def do_POST(self):
                self._record()
                slug = json.loads(api.requests[-1][2])["slug"]
                self._send(201, json.dumps({"id": "cid-1", "slug": slug}))

            def do_DELETE(self):
                self._record()
                self._send(202, json.dumps({"id": "cid-1", "desired": "deleting"}))

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def methods(self):
        return [(m, p) for m, p, _ in self.requests]


@pytest.fixture
def api():
    a = _Api()
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
        '  *" wait "*) exit 0 ;;\n'
        '  *"get pod"*) echo "pod/smoke-agent-0"; exit 0 ;;\n'
        '  *" exec "*) printf "%s\\n" "$FAKE_MCP_LIST"; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    k.chmod(0o755)
    return b


def _run(api, fakebin, mcp_list):
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")},
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "HOST": f"127.0.0.1:{api.port}",
        "SCHEME": "http",
        "SMOKE_TIMEOUT_S": "20",
        "FAKE_MCP_LIST": mcp_list,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], check=False, env=env, capture_output=True, text=True, timeout=60
    )


def test_happy_path_exits_zero_and_cleans_up(api, fakebin):
    r = _run(api, fakebin, "any-mcp: http://x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    methods = api.methods()
    assert ("POST", "/api/v1/clusters") in methods
    assert ("DELETE", "/api/v1/clusters/cid-1") in methods
    body = json.loads(next(b for m, _, b in api.requests if m == "POST"))
    assert re.fullmatch(r"smoke-\d+", body["slug"])
    assert body["spec"]["replicas"] == 1


def test_missing_any_mcp_fails_and_cleans_up(api, fakebin):
    r = _run(api, fakebin, "other-mcp: http://x - Connected")
    assert r.returncode != 0
    assert "other-mcp" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


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


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck missing")
def test_script_is_shellcheck_clean():
    subprocess.run(["shellcheck", "--severity=error", str(SCRIPT)], check=True)

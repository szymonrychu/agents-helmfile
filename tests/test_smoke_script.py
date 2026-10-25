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
        '  *"get service"*) [ -n "${FAKE_AGENTS_PORT-8001}" ] || exit 1;'
        ' echo "$*" >> "$FAKE_KUBECTL_LOG"; printf "%s" "${FAKE_AGENTS_PORT-8001}"; exit 0 ;;\n'
        '  *"get ingress"*) [ -z "${FAKE_INGRESS_FAIL-}" ] || exit 1;'
        ' printf "%s" "$FAKE_INGRESS_BACKENDS"; exit 0 ;;\n'
        '  *"get pod"*) echo "pod/smoke-agent-0"; exit 0 ;;\n'
        '  *" exec "*|*port-forward*) echo "$*" >> "$FAKE_KUBECTL_LOG" ;;\n'
        "  *) ;;\n"
        "esac\n"
        'case "$*" in\n'
        '  *"ls -d /sessions"*) printf "%s\\n" "$FAKE_SESSION_CFG"; exit 0 ;;\n'
        '  *"any-mcp --version"*) printf "%s\\n" "$FAKE_VERSION"; exit 0 ;;\n'
        '  *" exec "*) printf "%s\\n" "$FAKE_MCP_LIST"; exit 0 ;;\n'
        "  *port-forward*) exec sleep 60 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    k.chmod(0o755)
    return b


def _run(
    api,
    fakebin,
    mcp_list,
    version="any-mcp 0.2.0",
    agents_port="8001",
    ingress_backends="agent-orchestrator",
    ingress_fail="",
    session_cfg="/sessions/abc/config",
    port_forward=False,
):
    target = (
        {"SMOKE_LOCAL_PORT": str(api.port)}
        if port_forward
        else {"HOST": f"127.0.0.1:{api.port}", "SCHEME": "http"}
    )
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV", "HOST", "SCHEME")},
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        **target,
        "SMOKE_TIMEOUT_S": "20",
        "FAKE_MCP_LIST": mcp_list,
        "FAKE_VERSION": version,
        "FAKE_SESSION_CFG": session_cfg,
        "FAKE_AGENTS_PORT": agents_port,
        "FAKE_INGRESS_BACKENDS": ingress_backends,
        "FAKE_INGRESS_FAIL": ingress_fail,
        "FAKE_KUBECTL_LOG": str(fakebin / "kubectl.log"),
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


def test_smoke_checks_session_config_dir(api, fakebin):
    r = _run(api, fakebin, "any-mcp: /opt/any-mcp/bin/any-mcp - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    assert "CLAUDE_CONFIG_DIR=/sessions/abc/config" in log
    assert "any-mcp --version" in log


def test_any_mcp_not_connected_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: /opt/any-mcp/bin/any-mcp - Failed to connect")
    assert r.returncode != 0
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


def test_wrong_any_mcp_version_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", version="any-mcp 0.1.0")
    assert r.returncode != 0
    assert "0.2.0" in r.stdout + r.stderr


def test_missing_agents_service_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", agents_port="")
    assert r.returncode != 0
    assert "agent-orchestrator-agents" in r.stdout + r.stderr


def test_wrong_agents_service_port_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", agents_port="8000")
    assert r.returncode != 0
    assert "8001" in r.stdout + r.stderr


def test_agents_service_as_ingress_backend_fails(api, fakebin):
    r = _run(
        api,
        fakebin,
        "any-mcp: x - Connected",
        ingress_backends="agent-orchestrator agent-orchestrator-agents",
    )
    assert r.returncode != 0
    assert "ingress" in r.stdout + r.stderr


def test_ingress_query_error_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", ingress_fail="1")
    assert r.returncode != 0
    assert "ingress" in r.stdout + r.stderr


def test_no_ingresses_still_passes(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", ingress_backends="")
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_session_config_dir_fails(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", session_cfg="")
    assert r.returncode != 0
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


def test_script_is_shellcheck_clean():
    assert shutil.which("shellcheck"), "shellcheck must be installed"
    subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_default_target_is_a_port_forward_to_the_api(api, fakebin):
    # The public host is behind SSO; the smoke reaches the api through a port-forward,
    # which the api's source allowlist (127.0.0.1/32) admits.
    r = _run(api, fakebin, "any-mcp: http://x - Connected", port_forward=True)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    assert f"port-forward deploy/agent-orchestrator-api {api.port}:8000" in log
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()

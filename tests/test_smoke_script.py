"""scripts/smoke.sh offline: fake HTTP API and a fake kubectl on PATH."""

import base64
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
WH_USER = "wh-user-smoke"
WH_PASSWORD = "wh-pass-SECRET-value-123"
WH_TOKEN = "orch-token-SECRET-value-456"
WH_ENDPOINT = "/webhooks/api/v1/endpoints/7"
DEFAULT_ROUTES = "lab.szymonrichert.pl / agents-web\nlab.szymonrichert.pl /api agent-orchestrator\n"


class _Api:
    def __init__(self):
        self.requests: list[tuple[str, str, bytes]] = []
        self.leader: str | None = "agent-uuid-0"
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
            "backends": [{"id": "orchestrator", "basePath": "/api/v1"}],
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
                elif self.path == "/api/v1/clusters/cid-1":
                    # Hive rule (agent-orchestrator db/leadership.py): only coalesce(max_replicas, replicas) > 1 elects a leader.
                    size = api.spec.get("max_replicas") or api.spec.get("replicas") or 0
                    leader = api.leader if size > 1 else None
                    view = {
                        "id": "cid-1",
                        "spec": api.spec,
                        "created_at": "2026-10-25T10:00:00.123456Z",
                        "leader_agent_id": leader,
                        "leader_since": "2026-10-25T10:00:30Z" if leader else None,
                        "leader_reason": "start" if leader else None,
                    }
                    if api.closes_at_offset is not None:
                        view["closes_at"] = (
                            f"2026-10-25T{10 + api.closes_at_offset // 3600}:00:00.123456Z"
                        )
                    self._send(200, json.dumps(view))
                elif self.path == "/api/v1/clusters/cid-1/init-scripts":
                    body = {"scripts": api.init_scripts, "status": api.init_status}
                    self._send(200, json.dumps(body))
                else:
                    self._send(404, "{}")

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
                if api.restart_pod0:
                    (api.state_dir / "uid0").write_text("uid-pod-0-restarted")
                self._send(202, json.dumps({"id": "cid-1", "replicas": n}))

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

            def do_POST(self):
                self._record()
                if self.path.startswith(("/webhook", "/webhooks")):
                    self._webhook_post()
                    return
                if self.path == "/api/v1/clusters/cid-1/scale":
                    self._scale()
                    return
                created = json.loads(api.requests[-1][2])
                api.spec = created.get("spec") or {}
                slug = created["slug"]
                self._send(201, json.dumps({"id": "cid-1", "slug": slug}))

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
                self._send(202, json.dumps({"id": "cid-1", "desired": "deleting"}))

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def methods(self):
        return [(m, p) for m, p, _ in self.requests]


@pytest.fixture
def api(tmp_path):
    a = _Api()
    a.state_dir = tmp_path / "state"
    a.state_dir.mkdir()
    (a.state_dir / "uid0").write_text("uid-pod-0")
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
        '  *" wait "*) echo "$*" >> "$FAKE_KUBECTL_LOG"; case "$*" in *"${FAKE_WAIT_FAIL:-@none@}"*) exit 1 ;; esac; exit 0 ;;\n'
        '  *"get secret"*) printf "%s" "$FAKE_WH_TOKEN_B64"; exit 0 ;;\n'
        '  *"get service"*) [ -n "${FAKE_AGENTS_PORT-8001}" ] || exit 1;'
        ' echo "$*" >> "$FAKE_KUBECTL_LOG"; printf "%s" "${FAKE_AGENTS_PORT-8001}"; exit 0 ;;\n'
        '  *"get ingress"*) [ -z "${FAKE_INGRESS_FAIL-}" ] || exit 1;'
        ' printf "%s" "$FAKE_INGRESS_ROUTES"; exit 0 ;;\n'
        '  *"get pod/agent-"*) cat "$FAKE_STATE_DIR/uid0"; exit 0 ;;\n'
        '  *"get pod"*) slug=$(echo "$*" | sed -n "s|.*cluster=\\([^ ]*\\).*|\\1|p");'
        ' echo "pod/agent-${slug}-init-abcde"; echo "pod/agent-${slug}-0";'
        ' [ ! -e "$FAKE_STATE_DIR/pod1" ] || echo "pod/agent-${slug}-1"; exit 0 ;;\n'
        '  *" exec "*|*port-forward*) echo "$*" >> "$FAKE_KUBECTL_LOG" ;;\n'
        "  *) ;;\n"
        "esac\n"
        'case "$*" in\n'
        '  *"/api/v1/presets"*) cat > /dev/null;'
        ' printf "%s %s\\n" "${FAKE_SVC_WITH-200}" "${FAKE_SVC_WITHOUT-401}"; exit 0 ;;\n'
        '  *"/agent/v1/me"*) echo "$*" >> "$FAKE_KUBECTL_LOG"; printf "%s\\n" "${FAKE_ME_STATUS-200}"; exit 0 ;;\n'
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
    ingress_routes=DEFAULT_ROUTES,
    ingress_fail="",
    session_cfg="/sessions/abc/config",
    port_forward=False,
    me_status="200",
    smoke_timeout="20",
    extra_env=None,
):
    target = (
        {
            "SMOKE_LOCAL_PORT": str(api.port),
            "SMOKE_WEB_LOCAL_PORT": str(api.port),
            "SMOKE_WEBHOOK_API_LOCAL_PORT": str(api.port),
            "SMOKE_WEBHOOK_INGEST_LOCAL_PORT": str(api.port),
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
        **(extra_env or {}),
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
    scales = [json.loads(b) for m, p, b in api.requests if p.endswith("/scale")]
    assert scales == [{"replicas": 2}, {"replicas": 1}]
    assert not (api.state_dir / "pod1").exists()
    assert (api.state_dir / "uid0").read_text() == "uid-pod-0"


def test_config_fields_checked(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "config fields echoed" in r.stdout
    assert "closes_at" in r.stdout
    assert "leader_since" in r.stdout
    assert ("GET", "/api/v1/clusters/cid-1") in api.methods()
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


def test_config_view_mismatch_fails(api, fakebin):
    api.closes_at_offset = None
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "closes_at" in r.stderr
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


def test_scale_refused_fails(api, fakebin):
    api.scale_status = 409
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "scale" in r.stdout + r.stderr
    assert "409" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


def test_pod0_restart_fails(api, fakebin):
    api.restart_pod0 = True
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode != 0
    assert "uid" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


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
        ingress_routes=DEFAULT_ROUTES + "lab.szymonrichert.pl /x agent-orchestrator-agents\n",
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
    assert f"port-forward deploy/agents-web {api.port}:8080" in log
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


def test_agent_me_check_fails_without_200(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected", me_status="403")
    assert r.returncode != 0
    assert "/agent/v1/me" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


def test_agent_me_check_runs_in_pod_with_its_own_env(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    log = (fakebin / "kubectl.log").read_text()
    start = re.search(r"exec pod/agent-smoke-\d+-0 -- python3", log)
    assert start, log
    entry = log[start.start() :]
    assert "/agent/v1/me" in entry
    for name in ("ORCH_AGENT_URL", "ORCH_AGENT_TOKEN", "ORCH_CLUSTER_ID"):
        assert name in entry


def test_no_leader_fails(api, fakebin):
    api.leader = None
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="6")
    assert r.returncode != 0
    assert "leader" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


def test_init_scripts_succeed(api, fakebin):
    r = _run(api, fakebin, "any-mcp: x - Connected")
    assert r.returncode == 0, r.stdout + r.stderr
    body = json.loads(next(b for m, _, b in api.requests if m == "POST"))
    assert body["spec"]["init_scripts"] == ["echo smoke-init"]
    assert ("GET", "/api/v1/clusters/cid-1/init-scripts") in api.methods()
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
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


def test_init_scripts_timeout_fails_and_cleans_up(api, fakebin):
    api.init_status = {"state": "running", "exit_code": None, "reason": None, "log_tail": ""}
    r = _run(api, fakebin, "any-mcp: x - Connected", smoke_timeout="8")
    assert r.returncode != 0
    assert "init" in r.stdout + r.stderr
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


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
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


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
    routes = (
        "lab.szymonrichert.pl / agent-orchestrator\nlab.szymonrichert.pl /api agent-orchestrator\n"
    )
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
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()
    assert re.search(
        r"exec -i pod/agent-smoke-\d+-0 -- python3 .*agent-orchestrator-services:8002",
        log,
        re.DOTALL,
    )
    assert "agent-orchestrator-services:8002" in r.stdout
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
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()
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
    assert ("DELETE", "/api/v1/clusters/cid-1") in api.methods()


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

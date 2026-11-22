"""local/tokens.py: token generation, per-release values and Secret manifests."""

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "local" / "tokens.py"
CANARY_USER = "CANARY-harbor-user"
CANARY_PASS = "CANARY-harbor-pass"
CANARY_CLAUDE = "CANARY-claude-token"
ALL = "webhook,schedule,app-connector"


def run(state, profiles=ALL, extra=(), **env_over):
    env = {
        "PATH": os.environ["PATH"],
        "HARBOR_USERNAME": CANARY_USER,
        "HARBOR_PASSWORD": CANARY_PASS,
        "CLAUDE_CODE_OAUTH_TOKEN": CANARY_CLAUDE,
    }
    env.update(env_over)
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--state", str(state), "--profiles", profiles, *extra],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def tokens(state):
    return json.loads((Path(state) / "tokens.json").read_text())


def values(state, release):
    return yaml.safe_load((Path(state) / "values" / f"{release}.yaml").read_text()) or {}


def secrets(state):
    docs = yaml.safe_load_all((Path(state) / "secrets.yaml").read_text())
    return {d["metadata"]["name"]: d for d in docs if d}


def lines(secret, key):
    return secret["stringData"][key].split("\n")


def test_tokens_stable_across_runs(tmp_path):
    assert run(tmp_path).returncode == 0
    first = tokens(tmp_path)
    assert run(tmp_path).returncode == 0
    assert tokens(tmp_path) == first


@pytest.mark.parametrize("var", ["HARBOR_USERNAME", "HARBOR_PASSWORD", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_missing_env_fails_without_echo(tmp_path, var):
    proc = run(tmp_path, **{var: ""})
    assert proc.returncode == 2
    assert var in proc.stderr
    out = proc.stdout + proc.stderr
    assert not any(c in out for c in (CANARY_USER, CANARY_PASS, CANARY_CLAUDE))


def test_fake_claude_only_when_flagged(tmp_path):
    proc = run(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="", AGENTS_LOCAL_FAKE_CLAUDE="1")
    assert proc.returncode == 0
    assert tokens(tmp_path)["claude"].startswith("fake-")
    other = tmp_path / "real"
    assert run(other).returncode == 0
    assert tokens(other)["claude"] == CANARY_CLAUDE
    assert values(other, "agents-orchestrator")["claude"]["token"] == CANARY_CLAUDE


def test_token_pairs_wire_consistently(tmp_path):
    assert run(tmp_path).returncode == 0
    t = tokens(tmp_path)
    s = secrets(tmp_path)
    orch = values(tmp_path, "agents-orchestrator")
    hashes = orch["serviceApi"]["tokenHashes"]
    for rel in (
        "agents-vault",
        "agents-taskboard",
        "agents-mindmap",
        "agents-usage",
        "agents-webhook",
        "agents-schedule",
        "agents-app-connector",
    ):
        raw = t["svc_" + rel.removeprefix("agents-").replace("-", "_")]
        assert hashes[rel] == hashlib.sha256(raw.encode()).hexdigest()
        if rel != "agents-mindmap":
            assert values(tmp_path, rel)["orchestrator"]["token"] == raw
    assert s["agents-mindmap-orchestrator-token"]["stringData"]["token"] == t["svc_mindmap"]
    # orchestrator upstream tokens are lines of the matching Secret
    assert orch["usage"]["token"] in lines(s["agents-usage-service-tokens"], "tokens")
    assert orch["vault"]["token"] in lines(s["agents-vault-service-tokens"], "tokens")
    assert orch["admin"]["history"]["token"] in lines(s["agents-history-service-tokens"], "tokens")
    assert orch["admin"]["webhook"]["token"] in lines(s["agents-webhook-admin-tokens"], "tokens")
    assert orch["admin"]["schedule"]["token"] in lines(s["agents-schedule-admin-tokens"], "tokens")
    assert orch["mindmap"]["token"] == s["agents-mindmap-relay-token"]["stringData"]["token"]
    # taskboard side
    tb = values(tmp_path, "agents-taskboard")
    assert tb["relayToken"] == orch["taskboard"]["token"]
    assert tb["usage"]["token"] in lines(s["agents-usage-service-tokens"], "tokens")
    assert tb["mindmapTokens"] == s["agents-mindmap-taskboard-token"]["stringData"]["token"]
    assert tb["delegateTokens"] == values(tmp_path, "agents-app-connector")["taskboard"]["token"]
    assert (
        values(tmp_path, "agents-webhook")["endpointKey"] == t["webhook_endpoint_key"]
        and len(base64.b64decode(t["webhook_endpoint_key"])) == 32
    )
    assert t["webhook_schedule"] in lines(s["agents-schedule-admin-tokens"], "tokens")
    assert s["agents-webhook-schedule-token"]["stringData"]["token"] == t["webhook_schedule"]
    # ingest tokens differ from service tokens and from each other
    assert s["agents-usage-ingest"]["stringData"]["token"] == t["ingest_usage"]
    assert s["agents-history-ingest"]["stringData"]["token"] == t["ingest_history"]
    svc_lines = lines(s["agents-usage-service-tokens"], "tokens") + lines(
        s["agents-history-service-tokens"], "tokens"
    )
    assert not {t["ingest_usage"], t["ingest_history"]} & set(svc_lines)
    assert t["ingest_usage"] != t["ingest_history"]
    # regcred decodes to the Harbor login
    cfg = json.loads(base64.b64decode(s["regcred"]["data"][".dockerconfigjson"]))
    auth = cfg["auths"]["harbor.szymonrichert.pl"]
    assert base64.b64decode(auth["auth"]).decode() == f"{CANARY_USER}:{CANARY_PASS}"
    assert s["regcred"]["type"] == "kubernetes.io/dockerconfigjson"


@pytest.mark.parametrize(
    "profiles",
    ["", "webhook", "schedule", "app-connector", "webhook,schedule"],
)
def test_profiles_gate_admin_upstreams(tmp_path, profiles):
    assert run(tmp_path, profiles=profiles).returncode == 0
    chosen = set(filter(None, profiles.split(",")))
    orch = values(tmp_path, "agents-orchestrator")
    admin = orch.get("admin", {})
    assert ("webhook" in admin) == ("webhook" in chosen)
    assert ("schedule" in admin) == ("schedule" in chosen)
    if "webhook" in chosen:
        assert admin["webhook"]["url"] == "http://agents-webhook-api:8001"
    if "schedule" in chosen:
        assert admin["schedule"]["url"] == "http://agents-schedule-api:8001"
    hashes = orch["serviceApi"]["tokenHashes"]
    for name in ("webhook", "schedule", "app-connector"):
        assert (f"agents-{name}" in hashes) == (name in chosen)
    for name in ("webhook", "schedule", "app-connector"):
        exists = (tmp_path / "values" / f"agents-{name}.yaml").exists()
        assert exists == (name in chosen)


def test_state_files_private(tmp_path):
    state = tmp_path / "state"
    assert run(state).returncode == 0
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    files = [state / "tokens.json", state / "secrets.yaml", *(state / "values").glob("*.yaml")]
    assert len(files) > 5
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_no_value_printed(tmp_path):
    proc = run(tmp_path)
    assert proc.returncode == 0
    out = proc.stdout + proc.stderr
    secret_values = [*tokens(tmp_path).values(), CANARY_USER, CANARY_PASS, CANARY_CLAUDE]
    assert not [v for v in secret_values if v in out]
    assert str(tmp_path) in out


def test_cors_origins_follow_port(tmp_path):
    assert run(tmp_path, extra=["--http-port", "9090"]).returncode == 0
    env = values(tmp_path, "agents-orchestrator")["env"]
    assert env["ORCH_ALLOWED_ORIGINS"] == "http://localhost:9090"


@pytest.mark.parametrize(
    ("release", "key"),
    [
        ("agents-taskboard", "publicUrl"),
        ("agents-mindmap", "publicBaseUrl"),
        ("agents-webhook", "publicBaseUrl"),
    ],
)
def test_public_urls_point_at_localhost(tmp_path, release, key):
    assert run(tmp_path, extra=["--http-port", "9090"]).returncode == 0
    assert values(tmp_path, release)[key] == "http://localhost:9090"


def test_claude_token_follows_env(tmp_path):
    fake = {"CLAUDE_CODE_OAUTH_TOKEN": "", "AGENTS_LOCAL_FAKE_CLAUDE": "1"}
    assert run(tmp_path, **fake).returncode == 0
    first = tokens(tmp_path)
    assert first["claude"].startswith("fake-")
    assert run(tmp_path, **fake).returncode == 0
    assert tokens(tmp_path) == first
    assert run(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="CANARY-claude-one").returncode == 0
    assert tokens(tmp_path)["claude"] == "CANARY-claude-one"
    assert run(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="CANARY-claude-two").returncode == 0
    after = tokens(tmp_path)
    assert after["claude"] == "CANARY-claude-two"
    assert {k: v for k, v in after.items() if k != "claude"} == {
        k: v for k, v in first.items() if k != "claude"
    }
    assert values(tmp_path, "agents-orchestrator")["claude"]["token"] == "CANARY-claude-two"


def test_corrupt_state_exits_2(tmp_path):
    (tmp_path / "tokens.json").write_text("{CANARY-corrupt-content")
    proc = run(tmp_path)
    assert proc.returncode == 2
    assert "tokens.json" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "CANARY-corrupt-content" not in proc.stdout + proc.stderr


def test_newly_declared_names_added_existing_kept(tmp_path):
    assert run(tmp_path).returncode == 0
    path = tmp_path / "tokens.json"
    full = json.loads(path.read_text())
    del full["svc_schedule"]
    path.write_text(json.dumps(full))
    assert run(tmp_path).returncode == 0
    again = tokens(tmp_path)
    assert {k: v for k, v in again.items() if k != "svc_schedule"} == {
        k: v for k, v in full.items() if k != "svc_schedule"
    }
    assert again["svc_schedule"]

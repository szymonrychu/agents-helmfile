"""The agents-schedule release: pin, needs, wiring, ingress path, SSO and the loopback allowlist."""

import re

REL = "agents-schedule"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
# The v0.2.0 release commit 1e2e2a8: chart and image from one commit.
CHART = "0.1.0-main.1e2e2a8"
TAG = "0.2.0"


def _block(repo_root, name: str) -> str:
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    match = re.search(rf"  - name: {name}\n(.*?)(?=\n  - name: |\Z)", text, re.DOTALL)
    assert match, f"release {name} not declared"
    return match.group(1)


def _field(block: str, key: str) -> str:
    match = re.search(rf"^    {key}: (\S+)", block, re.MULTILINE)
    assert match, f"no {key}"
    return match.group(1)


def test_release_declared(repo_root, layered_values):
    block = _block(repo_root, REL)
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-schedule"
    assert _field(block, "version") == CHART
    assert _field(block, "namespace") == "agents"
    assert "- agents/agents-schedule-db" in block
    assert "application: agents-schedule" in block
    assert "purpose: agents" in block
    assert layered_values(REL)["image"]["tag"] == TAG
    orch = _block(repo_root, "agents-orchestrator")
    assert "values/agents-orchestrator/service-token-agents-schedule.secrets.yaml" in orch


def test_schedule_pin_matches_release(repo_root, layered_values):
    tag = layered_values(REL)["image"]["tag"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag)
    assert _field(_block(repo_root, REL), "version") == CHART
    assert tag == TAG


def test_secrets_wired(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-schedule-db-cluster-app"
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("url")
    assert v["orchestrator"]["url"] == "http://agents-orchestrator-services:8002"
    # the token comes from default.secrets.yaml (SOPS), never plain values
    assert not v["orchestrator"].get("token")
    assert not v["orchestrator"].get("tokenSecret", {}).get("name")


def test_ingress_path(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/schedules", "pathType": "Prefix"}]}]
    assert ing["tls"] == [{"secretName": "agents-orchestrator-tls", "hosts": [HOST]}]
    assert ing["denySourceRanges"] == NODE_IPS
    assert not any("cert-manager" in k or "tls-acme" in k for k in ing.get("annotations", {}))


def test_sso_and_allowlist(layered_values):
    v = layered_values(REL)
    orch = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    assert v["components"]["api"]["podAnnotations"] == orch
    assert orch["oauth2-proxy-admission/secret-name"] == "agents-orchestrator-proxy"
    assert not v["components"].get("worker", {}).get("podAnnotations")
    assert v["env"]["SCHED_ALLOWED_CIDRS"] == "127.0.0.1/32"

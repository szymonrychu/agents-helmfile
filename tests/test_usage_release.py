"""The agents-usage release: pin, needs, wiring, secrets Secrets, ingress path, SSO and loopback allowlist."""

import re

REL = "agents-usage"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
# The chart version the agents-usage main build publishes for the v0.1.0 release commit
# (0.1.0-main.<sha7>); all zeros until that commit exists, so check-pins refuses it until set.
CHART = "0.1.0-main.0000000"
TAG = "0.1.0"


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
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-usage"
    assert _field(block, "version") == CHART
    assert _field(block, "namespace") == "agents"
    assert "- agents/agents-usage-db" in block
    assert "- agents/agents-orchestrator" in block
    assert "application: agents-usage" in block
    assert "purpose: agents" in block
    assert layered_values(REL)["image"]["repository"] == (
        "harbor.szymonrichert.pl/containers/agents-usage"
    )
    assert layered_values(REL)["image"]["tag"] == TAG


def test_usage_pin_matches_release(repo_root, layered_values):
    tag = layered_values(REL)["image"]["tag"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag)
    assert re.fullmatch(
        r"\d+\.\d+\.\d+-main\.[0-9a-f]{7}", _field(_block(repo_root, REL), "version")
    )
    assert tag == TAG


def test_secrets_wired(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-usage-db-cluster-app"
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("url")
    # the prober reads the orchestrator's claude Secret (same namespace); no token in values
    assert v["claude"]["existingSecret"] == "agents-orchestrator-claude"
    assert not v["claude"].get("token")
    # the watcher token comes from default.secrets.yaml (SOPS), never plain values
    assert v["orchestrator"]["url"] == "http://agents-orchestrator-services:8002"
    assert not v["orchestrator"].get("token")
    assert not v["orchestrator"].get("existingSecret")


def test_listeners(layered_values):
    v = layered_values(REL)
    assert v["serviceListener"]["enabled"] is True
    assert v["serviceListener"]["tokensSecret"]["name"] == "agents-usage-service-tokens"
    assert v["ingestListener"]["enabled"] is True
    assert v["ingestListener"]["tokensSecret"] == {"name": "agents-usage-ingest", "key": "token"}


def test_ingress_path(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/usage", "pathType": "Prefix"}]}]
    assert ing["tls"] == [{"secretName": "agents-orchestrator-tls", "hosts": [HOST]}]
    assert ing["denySourceRanges"] == NODE_IPS
    assert not any("cert-manager" in k or "tls-acme" in k for k in ing.get("annotations", {}))


def test_sso_and_allowlist(layered_values):
    v = layered_values(REL)
    orch = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    assert v["components"]["api"]["podAnnotations"] == orch
    assert orch["oauth2-proxy-admission/secret-name"] == "agents-orchestrator-proxy"
    assert not v["components"].get("prober", {}).get("podAnnotations")
    assert not v["components"].get("watcher", {}).get("podAnnotations")
    assert v["env"]["USAGE_ALLOWED_CIDRS"] == "127.0.0.1/32"


def test_no_poll_setting_anywhere(repo_root, layered_values):
    assert not [k for k in layered_values(REL).get("env", {}) if k.startswith("USAGE_POLL")]
    for path in (repo_root / "values" / REL).rglob("*.yaml"):
        assert "USAGE_POLL" not in path.read_text(), path
    assert "USAGE_POLL" not in (repo_root / "helmfile.yaml.gotmpl").read_text()

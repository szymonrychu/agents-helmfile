"""The agents-history release: pin, needs, wiring, token Secrets, ingress path, SSO and loopback allowlist."""

import re

REL = "agents-history"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
# The chart version the agents-history main build published for the v0.1.1 release commit
# (e6a1f6d, release run 41435547204 chart-publish VERSION): 0.1.0-main.<sha7>.
CHART = "0.1.0-main.e6a1f6d"
TAG = "0.1.1"


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
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-history"
    assert _field(block, "version") == CHART
    assert _field(block, "namespace") == "agents"
    assert "- agents/agents-history-db" in block
    assert "application: agents-history" in block
    assert "purpose: agents" in block
    assert layered_values(REL)["image"]["repository"] == (
        "harbor.szymonrichert.pl/containers/agents-history"
    )


def test_release_order(repo_root):
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    names = re.findall(r"^  - name: (\S+)", text, re.MULTILINE)
    assert names.index("agents-history-db") == names.index("agents-usage-db") + 1
    assert names.index("agents-history") == names.index("agents-usage") + 1


def test_history_pin_is_semver(repo_root, layered_values):
    tag = layered_values(REL)["image"]["tag"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag)
    assert re.fullmatch(
        r"\d+\.\d+\.\d+-main\.[0-9a-f]{7}", _field(_block(repo_root, REL), "version")
    )
    assert tag == TAG


def test_secrets_wired(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-history-db-cluster-app"
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("url")


def test_listeners(layered_values):
    v = layered_values(REL)
    assert v["serviceListener"]["enabled"] is True
    assert v["serviceListener"]["tokensSecret"] == {
        "name": "agents-history-service-tokens",
        "key": "tokens",
    }
    assert v["ingestListener"]["enabled"] is True
    assert v["ingestListener"]["tokensSecret"] == {"name": "agents-history-ingest", "key": "token"}


def test_ingress_path(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/history", "pathType": "Prefix"}]}]
    assert ing["tls"] == [{"secretName": "agents-orchestrator-tls", "hosts": [HOST]}]
    assert ing["denySourceRanges"] == NODE_IPS
    assert ing["denySourceRanges"] == layered_values("agents-usage")["ingress"]["denySourceRanges"]
    assert not any("cert-manager" in k or "tls-acme" in k for k in ing.get("annotations", {}))


def test_sso_and_allowlist(layered_values):
    v = layered_values(REL)
    orch = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    assert v["components"]["api"]["podAnnotations"] == orch
    assert orch["oauth2-proxy-admission/secret-name"] == "agents-orchestrator-proxy"
    assert v["env"]["HISTORY_ALLOWED_CIDRS"] == "127.0.0.1/32"


def test_bootstrap_has_history_tokens(repo_root):
    src = (repo_root / "scripts" / "bootstrap-secrets.sh").read_text()
    for func in ("bootstrap_history_token", "bootstrap_history_ingest_token"):
        assert re.search(rf"^{func}\(\) \{{", src, re.MULTILINE), f"{func} not defined"
        assert re.search(rf"^{func}$", src, re.MULTILINE), f"{func} not called"
    assert src.rindex("\nbootstrap_history_ingest_token\n") > src.rindex(
        "\nbootstrap_history_token\n"
    )
    files = dict(re.findall(r'^(HIS_\w+_FILE)="([^"]+)"', src, re.MULTILINE))
    assert files == {
        "HIS_TOKENS_FILE": "values/agents-history/raw/service-tokens.agents-history.pre.secrets.yaml",
        "HIS_ORCH_FILE": "values/agents-orchestrator/history-token.secrets.yaml",
        "HIS_INGEST_FILE": "values/agents-history/raw/ingest-token.agents-history.pre.secrets.yaml",
    }
    assert files["HIS_TOKENS_FILE"] != files["HIS_INGEST_FILE"]

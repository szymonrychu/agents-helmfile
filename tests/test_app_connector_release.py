"""The agents-app-connector release: pin, needs, wiring, the two ingress paths, SSO, orchestrator side."""

import re

REL = "agents-app-connector"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
# The v2.0.0 release commit 559e7e0: chart and image from one commit.
CHART = "0.1.0-main.559e7e0"
TAG = "2.0.0"


def _block(repo_root, name: str) -> str:
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    match = re.search(rf"  - name: {name}\n(.*?)(?=\n  - name: |\Z)", text, re.DOTALL)
    assert match, f"release {name} not declared"
    return match.group(1)


def _field(block: str, key: str) -> str:
    match = re.search(rf"^    {key}: (\S+)", block, re.MULTILINE)
    assert match, f"no {key}"
    return match.group(1)


def test_release_declared_with_db_need(repo_root):
    block = _block(repo_root, REL)
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-app-connector"
    assert _field(block, "version") == CHART
    assert _field(block, "namespace") == "agents"
    assert "- agents/agents-app-connector-db" in block
    assert "- agents/agents-orchestrator" in block
    assert "application: agents-app-connector" in block
    assert "purpose: agents" in block
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    assert text.index("  - name: agents-app-connector-db\n") < text.index(
        "  - name: agents-app-connector\n"
    )
    assert text.index("  - name: agents-taskboard-db\n") < text.index(
        "  - name: agents-app-connector-db\n"
    )
    assert text.index("  - name: agents-taskboard\n") < text.index(
        "  - name: agents-app-connector\n"
    )


def test_image_tag_is_semver(layered_values):
    v = layered_values(REL)
    assert re.fullmatch(r"\d+\.\d+\.\d+", v["image"]["tag"])
    assert v["image"]["tag"] == TAG
    assert v["image"]["repository"] == "harbor.szymonrichert.pl/containers/agents-app-connector"


def test_wiring(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-app-connector-db-cluster-app"
    assert not v["postgres"].get("url")
    assert v["orchestrator"]["url"] == "http://agents-orchestrator-services:8002"
    assert not v["orchestrator"].get("token")
    assert not v["orchestrator"].get("existingSecret")
    assert v["adminAllowedCidrs"] == ["127.0.0.1/32"]
    assert v["env"] == {"APPCONN_HANDOFF_PRESETS": "", "APPCONN_MAX_LONG_EDGE_PX": "1568"}
    assert v["persistence"]["size"] == "5Gi"


def test_token_file_is_sops_and_hash_on_orchestrator(repo_root):
    # the connector's own default.secrets.yaml comes from the default anchor
    assert (repo_root / "values/agents-app-connector/default.secrets.yaml").is_file()
    orch = _block(repo_root, "agents-orchestrator")
    assert "values/agents-orchestrator/service-token-agents-app-connector.secrets.yaml" in orch
    assert "- agents/agents-app-connector" not in orch


def test_only_the_two_paths_are_routed(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["className"] == "nginx"
    assert ing["tls"] == [{"secretName": "agents-orchestrator-tls", "hosts": [HOST]}]
    assert ing["denySourceRanges"] == NODE_IPS
    assert ing["device"]["enabled"] is True and ing["admin"]["enabled"] is True
    assert ing["device"]["host"] == HOST and ing["admin"]["host"] == HOST
    assert ing["device"]["path"] == "/app/v1/sketch"
    assert ing["admin"]["path"] == "/app/admin"
    assert ing["device"]["proxyBodySize"] == "9m"
    assert not any(
        "cert-manager" in k or "tls-acme" in k for k in ing["device"].get("annotations", {})
    )


def test_sso_on_admin_only(layered_values):
    v = layered_values(REL)
    orch = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    assert v["components"]["admin"]["podAnnotations"] == orch
    assert not v["components"].get("app", {}).get("podAnnotations")
    assert not v["ingress"]["device"].get("annotations")


def test_connector_taskboard_url_and_token_file_wired(repo_root, layered_values):
    v = layered_values(REL)
    assert v["taskboard"] == {"url": "http://agents-taskboard-delegate:8003"}
    # the token comes from SOPS, never a plain value or an env entry of the shared ConfigMap
    assert not any("TASKBOARD" in k for k in v["env"])
    block = _block(repo_root, REL)
    assert "values/agents-app-connector/default.secrets.yaml" in block
    assert "values/agents-app-connector/taskboard-token.secrets.yaml" in block
    assert (repo_root / "values/agents-app-connector/taskboard-token.secrets.yaml").is_file()

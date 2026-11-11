"""The agents-taskboard release: pin, needs, wiring, ingress path, SSO, and the orchestrator side."""

import re

REL = "agents-taskboard"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
# The v0.7.0 release commit 319a74d: chart and image from one commit.
CHART = "0.1.0-main.319a74d"
TAG = "0.7.0"


def _block(repo_root, name: str) -> str:
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    match = re.search(rf"  - name: {name}\n(.*?)(?=\n  - name: |\Z)", text, re.DOTALL)
    assert match, f"release {name} not declared"
    return match.group(1)


def _field(block: str, key: str) -> str:
    match = re.search(rf"^    {key}: (\S+)", block, re.MULTILINE)
    assert match, f"no {key}"
    return match.group(1)


def test_taskboard_release_declared_with_db_need(repo_root):
    block = _block(repo_root, REL)
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-taskboard"
    assert _field(block, "version") == CHART
    assert _field(block, "namespace") == "agents"
    assert "- agents/agents-taskboard-db" in block
    assert "- agents/agents-orchestrator" in block
    assert "application: agents-taskboard" in block
    assert "purpose: agents" in block
    assert "values/agents-taskboard/default.secrets.yaml" in block
    assert "values/agents-taskboard/relay-token.secrets.yaml" in block
    # the release is listed after the database it needs
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    assert "  - name: agents-taskboard-db\n" in text


def test_taskboard_image_tag_is_semver(layered_values):
    tag = layered_values(REL)["image"]["tag"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag)
    assert tag == TAG
    assert layered_values(REL)["image"]["repository"] == (
        "harbor.szymonrichert.pl/containers/agents-taskboard"
    )


def test_taskboard_wiring(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-taskboard-db-cluster-app"
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("url")
    assert v["publicUrl"] == "https://lab.szymonrichert.pl"
    # both tokens come from SOPS files, never plain values
    assert not v.get("relayToken")
    assert not v["orchestrator"].get("token")
    assert not v["orchestrator"].get("existingSecret")
    assert not v.get("relayExistingSecret")


def test_taskboard_orchestrator_url_configured_for_label_lookup(layered_values):
    # The notifier posts messages and looks up session labels through the same service listener.
    url = layered_values(REL)["orchestrator"]["url"]
    assert url == "http://agents-orchestrator-services:8002"
    assert url == layered_values("agents-schedule")["orchestrator"]["url"]


def test_taskboard_ingress_path(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/taskboard", "pathType": "Prefix"}]}]
    assert ing["tls"] == [{"secretName": "agents-orchestrator-tls", "hosts": [HOST]}]
    ann = ing["annotations"]
    assert ann["nginx.ingress.kubernetes.io/denylist-source-range"] == ",".join(NODE_IPS)
    assert not any("cert-manager" in k or "tls-acme" in k for k in ann)


def test_taskboard_sso_on_web_only(layered_values):
    v = layered_values(REL)
    orch = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    assert v["components"]["web"]["podAnnotations"] == orch
    for other in ("service", "notifier"):
        assert not v["components"].get(other, {}).get("podAnnotations")
    assert v["allowedCidrs"] == ["127.0.0.1/32"]


def test_orchestrator_values_set_taskboard_pair(repo_root, layered_values, helmfile_build):
    v = layered_values("agents-orchestrator")
    assert v["taskboard"]["url"] == "http://agents-taskboard-service:8002"
    assert not v["taskboard"].get("token")
    assert not v["taskboard"].get("existingSecret")
    block = _block(repo_root, "agents-orchestrator")
    for f in ("service-token-agents-taskboard", "taskboard-token"):
        assert f"values/agents-orchestrator/{f}.secrets.yaml" in block
    assert "- agents/agents-taskboard" not in block


def test_plugins_ref_has_the_taskboard_tools(layered_values):
    ref = layered_values("agents-orchestrator")["env"]["ORCH_ANY_MCP_PLUGINS_REF"]
    # v0.9.0 introduced the taskboard tools; later minors keep them.
    assert tuple(int(p) for p in ref.lstrip("v").split(".")) >= (0, 9, 0)


def test_web_backend_taskboard_listed(layered_values):
    backends = layered_values("agents-web")["backends"]
    assert {"id": "taskboard", "label": "Tasks", "basePath": "/taskboard/api/v1"} in backends


def test_taskboard_has_delegate_component_without_ingress(layered_values):
    v = layered_values(REL)
    # the token file renders the delegate component; no plain token, no existing secret, no ingress
    assert not v.get("delegateTokens")
    assert not v.get("delegateExistingSecret")
    assert not v["components"].get("delegate", {}).get("podAnnotations")
    paths = [p["path"] for h in v["ingress"]["hosts"] for p in h["paths"]]
    assert paths == ["/taskboard"]


def test_taskboard_delegate_token_file_listed(repo_root):
    block = _block(repo_root, REL)
    assert "values/agents-taskboard/delegate-token.secrets.yaml" in block
    assert (repo_root / "values/agents-taskboard/delegate-token.secrets.yaml").is_file()


def test_ingress_paths_unchanged(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/taskboard", "pathType": "Prefix"}]}]
    conn = layered_values("agents-app-connector")["ingress"]
    assert conn["device"]["path"] == "/app/v1/sketch" and conn["admin"]["path"] == "/app/admin"

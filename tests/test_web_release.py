"""The agents-web release: pin, oauth2-proxy sidecar, root ingress, backends config."""

import re

REL = "agents-web"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]


def _chart_version(repo_root) -> str:
    """Read the release's chart version from the helmfile text, so no chart pull is needed."""
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    match = re.search(
        rf"- name: {REL}\n(?:(?!\n  - name:).)*?\n    version: (\S+)", text, re.DOTALL
    )
    assert match, f"no version found for release {REL}"
    return match.group(1)


def test_web_pin_matches_image(repo_root, layered_values):
    tag = layered_values(REL)["image"]["tag"]
    assert re.fullmatch(r"[0-9a-f]{7}", tag)
    assert _chart_version(repo_root) == f"0.1.0-main.{tag}"


def test_web_behind_oauth2_proxy_with_orchestrator_secret(layered_values):
    web = layered_values(REL)["podAnnotations"]
    api = layered_values("agent-orchestrator")["components"]["api"]["podAnnotations"]
    assert web == api
    assert web["oauth2-proxy-admission/enable"] == "true"
    assert web["oauth2-proxy-admission/secret-name"] == "agent-orchestrator-proxy"
    assert web["oauth2-proxy-admission/secret-namespace"] == "agents"
    assert web["oauth2-proxy-admission/redeem-url"]


def test_web_ingress_owns_root_without_cert_annotations(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["denySourceRanges"] == NODE_IPS
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/", "pathType": "Prefix"}]}]
    assert ing["tls"] == [{"secretName": "agent-orchestrator-tls", "hosts": [HOST]}]
    for key in ing.get("annotations") or {}:
        assert key not in ("cert-manager.io/cluster-issuer", "kubernetes.io/tls-acme"), key


def test_web_backends_list_orchestrator_api(layered_values):
    assert layered_values(REL)["backends"] == [
        {"id": "orchestrator", "label": "Orchestrator", "basePath": "/api/v1"}
    ]


def test_web_release_chart(helmfile_build):
    rels = [r for d in helmfile_build() for r in d.get("releases", []) if r["name"] == REL]
    assert rels, f"release {REL} not rendered"
    rel = rels[0]
    assert rel["chart"] == "oci://harbor.szymonrichert.pl/charts/agents-web"
    assert rel["namespace"] == "agents"
    assert rel["timeout"] == 600
    assert rel["labels"]["purpose"] == "agents"
    assert rel["labels"]["application"] == "agents-web"

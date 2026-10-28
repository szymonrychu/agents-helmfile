"""The agents-web release: pin, oauth2-proxy sidecar, root ingress, backends config."""

import os
import re
import shutil
import subprocess

import pytest
import yaml

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
    assert tag == "6ea3457"


def test_web_behind_oauth2_proxy_with_orchestrator_secret(layered_values):
    web = layered_values(REL)["podAnnotations"]
    api = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    assert web == api
    assert web["oauth2-proxy-admission/enable"] == "true"
    assert web["oauth2-proxy-admission/secret-name"] == "agents-orchestrator-proxy"
    assert web["oauth2-proxy-admission/secret-namespace"] == "agents"
    assert web["oauth2-proxy-admission/redeem-url"]


def test_web_ingress_owns_root_without_cert_annotations(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["denySourceRanges"] == NODE_IPS
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/", "pathType": "Prefix"}]}]
    assert ing["tls"] == [{"secretName": "agents-orchestrator-tls", "hosts": [HOST]}]
    for key in ing.get("annotations") or {}:
        assert key not in ("cert-manager.io/cluster-issuer", "kubernetes.io/tls-acme"), key


def test_web_backends_list_orchestrator_api(layered_values):
    assert layered_values(REL)["backends"][0] == {
        "id": "orchestrator",
        "label": "Orchestrator",
        "basePath": "/api/v1",
    }


def test_webhooks_backend_configured(layered_values):
    backends = layered_values(REL)["backends"]
    assert backends[1] == {"id": "webhooks", "label": "Webhooks", "basePath": "/webhooks/api/v1"}


def test_schedules_backend_configured(layered_values):
    backends = layered_values(REL)["backends"]
    assert backends[0]["id"] == "orchestrator"
    assert backends[2] == {
        "id": "schedules",
        "label": "Schedules",
        "basePath": "/schedules/api/v1",
    }


def test_matrix_backend_configured(layered_values):
    backends = layered_values(REL)["backends"]
    assert backends[-1] == {"id": "matrix", "label": "Chats", "basePath": "/matrix/api/v1"}
    assert [b["id"] for b in backends] == ["orchestrator", "webhooks", "schedules", "matrix"]


def test_web_release_needs_matrix_connector(repo_root):
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    block = re.search(rf"- name: {REL}\n(?:(?!\n  - name:).)*", text, re.DOTALL).group(0)
    assert "- agents/agents-matrix-connector" in block


def test_web_release_needs_webhook(repo_root):
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    block = re.search(rf"- name: {REL}\n(?:(?!\n  - name:).)*", text, re.DOTALL).group(0)
    assert "- agents/agents-webhook" in block


def test_web_release_chart(helmfile_build):
    rels = [r for d in helmfile_build() for r in d.get("releases", []) if r["name"] == REL]
    assert rels, f"release {REL} not rendered"
    rel = rels[0]
    assert rel["chart"] == "oci://harbor.szymonrichert.pl/charts/agents-web"
    assert rel["namespace"] == "agents"
    assert rel["timeout"] == 600
    assert rel["labels"]["purpose"] == "agents"
    assert rel["labels"]["application"] == "agents-web"


def test_web_release_chart_renders_unique_pull_secrets(repo_root):
    """Server-side apply rejects duplicate imagePullSecrets names in a pod spec."""
    if shutil.which("helmfile") is None:
        pytest.fail("helmfile is not on PATH (run under `mise exec`/`mise run`)")
    proc = subprocess.run(  # noqa: PLW1510
        ["helmfile", "-f", "helmfile.yaml.gotmpl", "-l", f"application={REL}", "template"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": "/dev/null"},
    )
    assert proc.returncode == 0, proc.stderr
    pods = 0
    for doc in yaml.safe_load_all(proc.stdout):
        spec = ((doc or {}).get("spec") or {}).get("template", {}).get("spec") or (doc or {}).get(
            "spec"
        )
        names = [s["name"] for s in (spec or {}).get("imagePullSecrets", [])]
        if names:
            pods += 1
            assert len(names) == len(set(names)), (
                f"{doc['kind']}/{doc['metadata']['name']}: {names}"
            )
    assert pods

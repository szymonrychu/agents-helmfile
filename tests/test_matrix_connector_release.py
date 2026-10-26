"""The agents-matrix-connector release pair: pins, SSO on the api only, wiring, ingress, no plaintext."""

import os
import re
import shutil
import subprocess

import pytest
import yaml

REL = "agents-matrix-connector"
DB_REL = "agents-matrix-connector-db"
SHA = "038b4f9"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
TOKEN_FILE = "values/agents-orchestrator/service-token-agents-matrix-connector.secrets.yaml"


def _text(repo_root) -> str:
    return (repo_root / "helmfile.yaml.gotmpl").read_text()


def _block(repo_root, name: str) -> str:
    match = re.search(rf"  - name: {name}\n(.*?)(?=\n  - name: |\Z)", _text(repo_root), re.DOTALL)
    assert match, f"release {name} not declared"
    return match.group(1)


def _field(block: str, key: str) -> str:
    match = re.search(rf"^    {key}: (\S+)", block, re.MULTILINE)
    assert match, f"no {key}"
    return match.group(1)


def test_matrix_connector_pin_and_needs(repo_root, layered_values):
    block = _block(repo_root, REL)
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-matrix-connector"
    assert _field(block, "version") == f"0.1.0-main.{SHA}"
    assert layered_values(REL)["image"]["tag"] == SHA
    assert _field(block, "namespace") == "agents"
    assert "- agents/agents-matrix-connector-db" in block
    assert "- agents/agents-orchestrator\n" in block + "\n"
    assert "application: agents-matrix-connector" in block
    db = _block(repo_root, DB_REL)
    assert _field(db, "chart") == "cnpg/cluster"
    assert _field(db, "version") == "0.9.0"
    assert _field(db, "force") == "false"
    assert _field(db, "namespace") == "agents"
    assert "application: agents-matrix-connector" in db
    v = layered_values(DB_REL)
    assert v["cluster"]["initdb"] == {
        "database": "agents_matrix_connector",
        "owner": "agents_matrix_connector",
    }
    assert v["backups"]["s3"]["path"] == "/agents-matrix-connector-db/"
    web = layered_values("agents-webhook-db")
    for key in ("storage", "walStorage"):
        assert v["cluster"][key] == web["cluster"][key]
    assert v["backups"]["s3"]["bucket"] == web["backups"]["s3"]["bucket"]
    assert v["backups"]["scheduledBackups"] == web["backups"]["scheduledBackups"]


def test_wiring_identity_and_policy(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-matrix-connector-db-cluster-app"
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("url")
    assert v["orchestrator"]["url"] == "http://agents-orchestrator-services:8002"
    m = v["matrix"]
    assert m["homeserverUrl"] == "http://matrix-synapse.messaging.svc.cluster.local:8008"
    assert m["userId"] == "@hermes:szymonrichert.pl"
    assert m["deviceId"] == "hermes-agent"
    assert m["selfIdentities"] == ["@facebook_61594633049360:szymonrichert.pl"]
    assert m["bridgeBots"] == ["@facebookbot:szymonrichert.pl"]
    assert v["policy"]["default"] == "deny"


def test_sso_and_ingress(layered_values):
    v = layered_values(REL)
    comps = v["components"]
    web = layered_values("agents-webhook")
    assert comps["api"]["podAnnotations"] == web["components"]["api"]["podAnnotations"]
    assert comps["api"]["podAnnotations"]["oauth2-proxy-admission/secret-name"] == (
        "agents-orchestrator-proxy"
    )
    assert comps["bot"]["podAnnotations"] == {}
    assert not v.get("podAnnotations")
    ing = v["ingress"]
    assert ing["enabled"] is True
    assert ing["host"] == HOST == web["ingress"]["host"]
    assert ing["tls"] == web["ingress"]["tls"] == {"secretName": "agents-orchestrator-tls"}
    assert ing["denySourceRanges"] == web["ingress"]["denySourceRanges"] == NODE_IPS
    assert not any("cert-manager" in k or "tls-acme" in k for k in ing.get("annotations", {}))


def _walk(node, path=""):
    if isinstance(node, dict):
        for k, val in node.items():
            yield from _walk(val, f"{path}.{k}")
    else:
        yield path, node


def test_token_hash_wired_and_no_plaintext(repo_root, layered_values):
    block = _block(repo_root, "agents-orchestrator")
    assert f"- {TOKEN_FILE}" in block
    assert (repo_root / TOKEN_FILE).is_file()
    for rel in (REL, DB_REL):
        for path in (repo_root / "values" / rel).glob("*.yaml"):
            if ".secrets." in path.name:
                continue
            for key, val in _walk(yaml.safe_load(path.read_text()) or {}):
                last = key.rsplit(".", 1)[-1].lower()
                if last in ("token", "accesstoken", "password", "accesskey", "secretkey"):
                    assert not val, f"{path}: {key}"
    v = layered_values(REL)
    assert not v["orchestrator"].get("token")
    assert not v["matrix"].get("accessToken")


def test_db_secret_matches_rendered_cnpg_cluster_helmfile_build(layered_values, repo_root):
    if shutil.which("helmfile") is None:
        pytest.fail("helmfile is not on PATH (run under `mise exec`/`mise run`)")
    proc = subprocess.run(  # noqa: PLW1510
        ["helmfile", "-f", "helmfile.yaml.gotmpl", "-l", f"name={DB_REL}", "template"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": "/dev/null"},
    )
    assert proc.returncode == 0, f"helmfile template failed:\n{proc.stderr}"
    clusters = [
        d
        for d in yaml.safe_load_all(proc.stdout)
        if isinstance(d, dict) and d.get("kind") == "Cluster"
    ]
    assert len(clusters) == 1
    assert layered_values(REL)["postgres"]["existingSecret"] == clusters[0]["metadata"]["name"] + "-app"

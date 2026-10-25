"""The agents-webhook release pair: pins, SSO on the api only, wiring, ingress, no plaintext secrets."""

import ipaddress
import os
import re
import shutil
import subprocess

import pytest
import yaml

REL = "agents-webhook"
DB_REL = "agents-webhook-db"
SHA = "838ba1f"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
# Ingress-nginx runs hostNetwork on .26/.27/.28; the ingest pod sees the node IP or that node's
# flannel.1/cni0 address (podCIDR .0 or .1). Every node is listed so the list survives a move.
TRUSTED = NODE_IPS + [f"10.244.{n}.{h}/32" for n in range(5) for h in (0, 1)]


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


def test_webhook_pin_and_needs(repo_root, layered_values):
    block = _block(repo_root, REL)
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-webhook"
    assert _field(block, "version") == f"0.1.0-main.{SHA}"
    assert layered_values(REL)["image"]["tag"] == SHA
    assert _field(block, "namespace") == "agents"
    assert "- agents/agents-webhook-db" in block
    assert "- agents/agents-orchestrator\n" in block + "\n"
    assert "application: agents-webhook" in block
    db = _block(repo_root, DB_REL)
    assert _field(db, "chart") == "cnpg/cluster"
    assert _field(db, "version") == "0.9.0"
    assert _field(db, "force") == "false"
    assert _field(db, "namespace") == "agents"
    assert "application: agents-webhook" in db


def test_sso_on_api_only(layered_values):
    comps = layered_values(REL)["components"]
    orch = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    assert comps["api"]["podAnnotations"] == orch
    assert comps["api"]["podAnnotations"]["oauth2-proxy-admission/secret-name"] == (
        "agents-orchestrator-proxy"
    )
    assert comps["ingest"]["podAnnotations"] == {}
    assert not comps.get("worker", {}).get("podAnnotations")
    assert not layered_values(REL).get("podAnnotations")


def test_database_and_orchestrator_wiring(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-webhook-db-cluster-app"
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("url")
    assert v["orchestrator"]["url"] == "http://agents-orchestrator-services:8002"
    assert v["publicBaseUrl"] == f"https://{HOST}"
    assert v["trustedProxies"] == TRUSTED
    for cidr in v["trustedProxies"]:
        assert ipaddress.ip_network(cidr).prefixlen == 32
    assert v["env"] == {"WEBHOOK_BOT_LOGINS": ""}


def test_ingress_host_and_tls(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["host"] == HOST
    assert ing["tls"] == {"secretName": "agents-orchestrator-tls"}
    assert ing["denySourceRanges"] == NODE_IPS
    for key in ("annotations", "ingestAnnotations"):
        assert not any("cert-manager" in k or "tls-acme" in k for k in ing.get(key, {}))
    # the public ingest Ingress carries no SSO and no source denylist
    assert not ing.get("ingestAnnotations")
    # >= the 5 MiB body cap (5m is exactly the cap; leave headroom)
    assert ing["proxyBodySize"] == "6m"
    assert int(ing["proxyReadTimeout"]) >= 60
    assert int(ing["proxySendTimeout"]) >= 60


def test_db_values_match_orchestrator_db_shape(layered_values):
    v = layered_values(DB_REL)
    assert v["cluster"]["initdb"] == {"database": "agents_webhook", "owner": "agents_webhook"}
    assert v["backups"]["s3"]["path"] == "/agents-webhook-db/"
    orch = layered_values("agents-orchestrator-db")
    for key in ("storage", "walStorage"):
        assert v["cluster"][key] == orch["cluster"][key]
    assert v["backups"]["s3"]["bucket"] == orch["backups"]["s3"]["bucket"]
    assert v["backups"]["scheduledBackups"] == orch["backups"]["scheduledBackups"]


def _walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    else:
        yield path, node


def test_no_sources_value_and_no_plaintext_secrets(repo_root, layered_values):
    v = layered_values(REL)
    assert "sources" not in v
    for rel in (REL, DB_REL):
        for path in (repo_root / "values" / rel).glob("*.yaml"):
            if ".secrets." in path.name:
                continue
            for key, val in _walk(yaml.safe_load(path.read_text()) or {}):
                assert "sources" not in key.lower().split("."), key
                last = key.rsplit(".", 1)[-1].lower()
                if last in ("token", "password", "accesskey", "secretkey", "url"):
                    assert not val or key.endswith("orchestrator.url"), f"{path}: {key}"
    assert not v["orchestrator"].get("token")
    assert not v["orchestrator"].get("tokenSecret", {}).get("name")


def test_secrets_files_encrypted(repo_root):
    for rel in (REL, DB_REL):
        path = repo_root / "values" / rel / "default.secrets.yaml"
        if not path.is_file():
            continue
        doc = yaml.safe_load(path.read_text())
        assert doc["sops"]["pgp"]
        for key, val in _walk({k: x for k, x in doc.items() if k != "sops"}):
            assert str(val).startswith("ENC["), key


def _rendered_cnpg_app_secret(repo_root) -> str:
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
    return clusters[0]["metadata"]["name"] + "-app"


def test_db_secret_matches_rendered_cnpg_cluster_helmfile_build(layered_values, repo_root):
    assert layered_values(REL)["postgres"]["existingSecret"] == _rendered_cnpg_app_secret(repo_root)


def test_api_allowlist_stays_chart_default(layered_values):
    # The oauth2-proxy sidecar reaches the api over 127.0.0.1:8000, the chart default
    # (WEBHOOK_API_ALLOWED_CIDRS=127.0.0.1/32); an override here would widen or break it.
    assert "allowedCidrs" not in layered_values(REL).get("api", {})


def test_allowed_authors_not_set(layered_values):
    env = layered_values(REL)["env"]
    assert "WEBHOOK_ALLOWED_AUTHORS" not in env
    assert "WEBHOOK_BOT_LOGINS" in env

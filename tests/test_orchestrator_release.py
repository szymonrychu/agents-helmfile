"""The agent-orchestrator release: chart pin, LAN-only ingress, ORCH env, secrets only encrypted."""

import os
import re
import shutil
import subprocess

import pytest
import yaml

REL = "agent-orchestrator"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]


def _release(helmfile_build):
    for doc in helmfile_build():
        for rel in doc.get("releases", []):
            if rel["name"] == REL:
                return rel
    raise AssertionError(f"release {REL} not rendered")


def _ann(layered_values):
    return layered_values(REL)["ingress"]["annotations"]


def test_release_chart_version_and_needs(helmfile_build):
    rel = _release(helmfile_build)
    assert rel["chart"] == "oci://harbor.szymonrichert.pl/charts/agent-orchestrator"
    assert rel["version"]
    assert rel["version"] != "latest"
    assert rel["namespace"] == "agents"
    assert rel["timeout"] == 600
    assert "agents/agent-orchestrator-db" in " ".join(rel["needs"])
    assert rel["labels"]["purpose"] == "agents"
    assert rel["labels"]["application"] == "agent-orchestrator"


HOST = "lab.szymonrichert.pl"


def test_ingress_lan_only_no_ingress_auth(layered_values):
    ing = layered_values(REL)["ingress"]
    ann = ing["annotations"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ann["cert-manager.io/cluster-issuer"] == "letsencrypt-prod"
    assert ann["kubernetes.io/tls-acme"] == "true"
    assert ann["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "3600"
    assert ann["nginx.ingress.kubernetes.io/proxy-send-timeout"] == "3600"
    assert ing["hosts"][0]["host"] == HOST
    assert ing["tls"] == [{"secretName": "agent-orchestrator-tls", "hosts": [HOST]}]
    for key in ann:
        assert not any(bad in key for bad in ("auth-url", "auth-signin", "oauth2")), key


def test_ingress_denies_node_ips_not_lan_only(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["denySourceRanges"] == NODE_IPS
    # SSO (the oauth2-proxy sidecar) gates access; the LAN-only allowlist is gone
    assert not ing.get("allowSourceRanges")
    assert "nginx.ingress.kubernetes.io/whitelist-source-range" not in ing["annotations"]


def test_orch_env_values(layered_values):
    v = layered_values(REL)
    assert v["image"]["repository"] == "harbor.szymonrichert.pl/containers/agent-orchestrator"
    assert v["image"]["tag"] not in ("", "latest")


def test_api_pod_behind_oauth2_proxy_webhook(layered_values):
    v = layered_values(REL)
    assert v["components"]["api"]["podAnnotations"] == {
        "oauth2-proxy-admission/enable": "true",
        "oauth2-proxy-admission/redeem-url": (
            "http://keycloak.authentication.svc.cluster.local"
            "/realms/master/protocol/openid-connect/token"
        ),
        "oauth2-proxy-admission/secret-name": "agent-orchestrator-proxy",
        "oauth2-proxy-admission/secret-namespace": "agents",
    }
    # The webhook rejects annotated pods whose first container has no ports, so the
    # port-less dispatcher/ingester/reconciler must not carry the annotations.
    assert not any(k.startswith("oauth2-proxy-admission/") for k in v.get("podAnnotations", {}))
    for comp in ("dispatcher", "ingester", "reconciler"):
        assert not v["components"].get(comp, {}).get("podAnnotations")


def test_api_probes_target_port_number(layered_values):
    api = layered_values(REL)["components"]["api"]
    assert api["livenessProbe"] == {"httpGet": {"path": "/healthz", "port": 8000}}
    assert api["readinessProbe"] == {"httpGet": {"path": "/readyz", "port": 8000}}


def test_allowed_cidrs_only_the_sidecar(layered_values):
    env = layered_values(REL)["env"]
    assert env["ORCH_ALLOWED_CIDRS"] == "127.0.0.1/32"
    assert env["ORCH_ALLOWED_ORIGINS"] == f"https://{HOST}"


def _rendered_cnpg_app_secret(repo_root) -> str:
    """Render the db release and derive CNPG's app Secret name: Cluster metadata.name + '-app'."""
    if shutil.which("helmfile") is None:
        pytest.fail("helmfile is not on PATH (run under `mise exec`/`mise run`)")
    proc = subprocess.run(  # noqa: PLW1510
        ["helmfile", "-f", "helmfile.yaml.gotmpl", "-l", "name=agent-orchestrator-db", "template"],
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
    assert len(clusters) == 1, f"expected one CNPG Cluster, got {len(clusters)}"
    return clusters[0]["metadata"]["name"] + "-app"


def test_db_secret_matches_rendered_cnpg_cluster(layered_values, repo_root):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == _rendered_cnpg_app_secret(repo_root)


def test_database_from_cnpg_secret(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("password") and not v["postgres"].get("url")
    assert not v.get("cnpg", {}).get("enabled")


def test_secrets_only_via_encrypted_files(layered_values, repo_root):
    v = layered_values(REL)
    assert not v.get("masterKey", {}).get("value")
    assert not v.get("claude", {}).get("token")
    for path in (repo_root / "values").rglob("*.yaml"):
        if ".secrets." in path.name:
            continue
        doc = yaml.safe_load(path.read_text()) or {}
        assert not _has_secret_key(doc), f"plain file {path} carries a token or master key"
    enc = yaml.safe_load((repo_root / "values" / REL / "default.secrets.yaml").read_text())
    assert enc["sops"]["pgp"]
    assert str(enc["masterKey"]["value"]).startswith("ENC[")
    assert str(enc["claude"]["token"]).startswith("ENC[")


def _has_secret_key(node) -> bool:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("token", "claudeOauthToken") and v:
                return True
            if k == "masterKey" and isinstance(v, dict) and v.get("value"):
                return True
            if _has_secret_key(v):
                return True
    return False


def test_any_mcp_configured_for_new_sessions(layered_values):
    env = layered_values(REL)["env"]
    assert env["ORCH_ANY_MCP_REPO"] == "https://github.com/szymonrychu/any-mcp"
    assert env["ORCH_ANY_MCP_REF"] == "v0.2.0"


def _chart_version(repo_root) -> str:
    """Read the release's chart version from the helmfile text, so no chart pull is needed."""
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    match = re.search(
        rf"- name: {REL}\n(?:(?!\n  - name:).)*?\n    version: (\S+)", text, re.DOTALL
    )
    assert match, f"no version found for release {REL}"
    return match.group(1)


def test_orchestrator_pin_matches_image(repo_root, layered_values):
    tag = layered_values(REL)["image"]["tag"]
    assert re.fullmatch(r"[0-9a-f]{7}", tag)
    assert _chart_version(repo_root) == f"0.1.0-main.{tag}"
    assert tag == "a5b5a75"


def test_agent_image_pins_agree(layered_values):
    v = layered_values(REL)
    assert v["agentImage"] == v["env"]["ORCH_AGENT_IMAGES"]
    repo, _, tag = v["agentImage"].rpartition(":")
    assert repo == "harbor.szymonrichert.pl/containers/claude-code-wrapper"
    # Release tag, not a sha7: Harbor prunes the sha7 tags.
    assert tag == "0.2.0"


def test_allowed_origins_is_ingress_host(layered_values):
    v = layered_values(REL)
    host = v["ingress"]["hosts"][0]["host"]
    assert v["env"]["ORCH_ALLOWED_ORIGINS"].split(",") == [f"https://{host}"]

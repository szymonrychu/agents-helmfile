"""The agents-vault release: pin, needs, wiring, token Secrets, ingress path, SSO and the orchestrator side."""

import re

import yaml

REL = "agents-vault"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
# The chart version the agents-vault main build publishes for the v0.5.0 release commit:
# 0.1.0-main.<sha7>.
CHART = "0.1.0-main.4c5cbcc"
TAG = "0.5.0"


def _block(repo_root, name: str) -> str:
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    match = re.search(rf"  - name: {name}\n(.*?)(?=\n  - name: |\Z)", text, re.DOTALL)
    assert match, f"release {name} not declared"
    return match.group(1)


def _field(block: str, key: str) -> str:
    match = re.search(rf"^    {key}: (\S+)", block, re.MULTILINE)
    assert match, f"no {key}"
    return match.group(1)


def _orchestrator_secrets(repo_root) -> list[str]:
    """The `secrets` list of the orchestrator release block, parsed (not substring-matched)."""
    text = re.sub(
        r"^\s*<<: \*default\n", "", _block(repo_root, "agents-orchestrator"), flags=re.MULTILINE
    )
    (release,) = yaml.safe_load(f"  - name: agents-orchestrator\n{text}")
    return release["secrets"]


def test_release_declared(repo_root):
    block = _block(repo_root, REL)
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-vault"
    assert _field(block, "version") == CHART
    assert _field(block, "namespace") == "agents"
    assert _field(block, "timeout") == "600"
    assert "- agents/agents-vault-db" in block
    assert "- agents/agents-orchestrator" in block
    assert "application: agents-vault" in block
    assert "purpose: agents" in block
    assert "<<: *default" in block


def test_release_order(repo_root):
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    names = re.findall(r"^  - name: (\S+)", text, re.MULTILINE)
    assert names.index("agents-vault-db") == names.index("agents-app-connector-db") + 1
    assert names.index("agents-vault") == names.index("agents-history") + 1


def test_vault_image_tag_is_semver(layered_values):
    v = layered_values(REL)
    assert re.fullmatch(r"\d+\.\d+\.\d+", v["image"]["tag"])
    assert v["image"]["tag"] == TAG
    assert v["image"]["repository"] == "harbor.szymonrichert.pl/containers/agents-vault"


def test_vault_wiring(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-vault-db-cluster-app"
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("url")
    assert v["orchestrator"]["url"] == "http://agents-orchestrator-services:8002"
    assert v["orchestrator"]["url"] == layered_values("agents-taskboard")["orchestrator"]["url"]
    # the orchestrator token comes from the SOPS file, never plain values
    assert not v["orchestrator"].get("token")
    assert not v["orchestrator"].get("existingSecret")
    assert v["serviceListener"]["tokensSecret"] == {
        "name": "agents-vault-service-tokens",
        "key": "tokens",
    }


def test_vault_ingress_path(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/vault", "pathType": "Prefix"}]}]
    assert ing["tls"] == [{"secretName": "agents-orchestrator-tls", "hosts": [HOST]}]
    ann = ing["annotations"]
    assert ann["nginx.ingress.kubernetes.io/denylist-source-range"] == ",".join(NODE_IPS)
    assert ann == layered_values("agents-taskboard")["ingress"]["annotations"]
    assert not any("cert-manager" in k or "tls-acme" in k for k in ann)


def test_vault_sso_on_web_only(layered_values):
    v = layered_values(REL)
    orch = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    assert v["components"]["web"]["podAnnotations"] == orch
    for other in ("service", "sweeper"):
        assert not v["components"].get(other, {}).get("podAnnotations")
    assert v["allowedCidrs"] == ["127.0.0.1/32"]


def test_bootstrap_has_vault_tokens(repo_root):
    src = (repo_root / "scripts" / "bootstrap-secrets.sh").read_text()
    assert re.search(r"^bootstrap_vault_token\(\) \{", src, re.MULTILINE)
    assert re.search(r"^bootstrap_vault_token$", src, re.MULTILINE)
    call = 'bootstrap_service_token agents-vault "$VLT_FILE" "$VLT_HASH_FILE"'
    assert re.search(rf"^{re.escape(call)}$", src, re.MULTILINE)
    assert src.rindex(f"\n{call}\n") < src.rindex("\nbootstrap_vault_token\n")
    files = dict(re.findall(r'^(VLT_(?:\w+_)?FILE)="([^"]+)"', src, re.MULTILINE))
    assert files == {
        "VLT_FILE": "values/agents-vault/default.secrets.yaml",
        "VLT_HASH_FILE": "values/agents-orchestrator/service-token-agents-vault.secrets.yaml",
        "VLT_TOKENS_FILE": "values/agents-vault/raw/service-tokens.agents-vault.pre.secrets.yaml",
        "VLT_ORCH_FILE": "values/agents-orchestrator/vault-token.secrets.yaml",
    }


def test_orchestrator_lists_the_vault_hash_and_token(repo_root):
    secrets = _orchestrator_secrets(repo_root)
    hash_file = "values/agents-orchestrator/service-token-agents-vault.secrets.yaml"
    token_file = "values/agents-orchestrator/vault-token.secrets.yaml"
    assert hash_file in secrets
    # stage 2: the orchestrator holds the vault bearer (vault.token), listed after the hash file
    assert secrets[-2:] == [hash_file, token_file]


def test_orchestrator_vault_upstream_and_scopes(layered_values):
    v = layered_values("agents-orchestrator")
    assert v["vault"]["url"] == "http://agents-vault-service:8002"
    # the bearer comes only from the SOPS file, never plain values or an existing Secret
    assert not v["vault"].get("token")
    assert not v["vault"].get("existingSecret")
    # the vault lists presets (attachments) and sessions (sweeper releases pins of discarded sessions)
    assert v["serviceApi"]["scopes"] == {
        "agents-vault": ["GET /api/v1/presets", "GET /api/v1/sessions"]
    }

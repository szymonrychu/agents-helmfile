"""The agents-mindmap release: pin, needs, wiring, raw token Secrets, ingress path, SSO, verifiers."""

import re

import yaml

REL = "agents-mindmap"
HOST = "lab.szymonrichert.pl"
NODE_IPS = [
    "192.168.1.26/32",
    "192.168.1.27/32",
    "192.168.1.28/32",
    "192.168.1.29/32",
    "192.168.1.65/32",
]
# The chart agents-mindmap main publishes for the v0.1.0 release commit b6fdd1f (the chart's own
# Chart.yaml version 0.1.0 is not what CI publishes: 0.1.0-main.<sha7>). Image: the release tag.
CHART = "0.1.0-main.b6fdd1f"
TAG = "0.1.0"
RAW = "values/agents-mindmap/raw"
TOKEN_SECRETS = {
    "orchestrator-token": "agents-mindmap-orchestrator-token",
    "relay-token": "agents-mindmap-relay-token",
    "taskboard-token": "agents-mindmap-taskboard-token",
}


def _block(repo_root, name: str) -> str:
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    match = re.search(rf"  - name: {name}\n(.*?)(?=\n  - name: |\Z)", text, re.DOTALL)
    assert match, f"release {name} not declared"
    return match.group(1)


def _field(block: str, key: str) -> str:
    match = re.search(rf"^    {key}: (\S+)", block, re.MULTILINE)
    assert match, f"no {key}"
    return match.group(1)


def _secrets_of(repo_root, name: str) -> list[str]:
    """The `secrets` list of one release block, parsed (not substring-matched)."""
    text = re.sub(r"^\s*<<: \*default\n", "", _block(repo_root, name), flags=re.MULTILINE)
    (release,) = yaml.safe_load(f"  - name: {name}\n{text}")
    return release.get("secrets", [])


def test_release_declared(repo_root):
    block = _block(repo_root, REL)
    assert _field(block, "chart") == "oci://harbor.szymonrichert.pl/charts/agents-mindmap"
    assert _field(block, "version") == CHART
    assert _field(block, "namespace") == "agents"
    assert _field(block, "timeout") == "600"
    assert "- agents/agents-mindmap-db" in block
    assert "- agents/agents-orchestrator" in block
    assert "application: agents-mindmap" in block
    assert "purpose: agents" in block
    assert "<<: *default" in block


def test_release_order(repo_root):
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    names = re.findall(r"^  - name: (\S+)", text, re.MULTILINE)
    assert names.index("agents-mindmap-db") == names.index("agents-vault-db") + 1
    assert names.index("agents-mindmap") == names.index("agents-vault") + 1


def test_mindmap_release_semver_image(layered_values):
    v = layered_values(REL)
    assert re.fullmatch(r"\d+\.\d+\.\d+", v["image"]["tag"])
    assert v["image"]["tag"] == TAG
    assert v["image"]["repository"] == "harbor.szymonrichert.pl/containers/agents-mindmap"


def test_mindmap_urls(layered_values):
    v = layered_values(REL)
    # the web host, no /mindmap suffix: fallback links are <publicBaseUrl>/mindmaps/M-<n>
    assert v["publicBaseUrl"] == "https://lab.szymonrichert.pl"
    assert v["orchestrator"]["url"] == "http://agents-orchestrator-services:8002"
    assert v["orchestrator"]["url"] == layered_values("agents-taskboard")["orchestrator"]["url"]
    assert v["taskboard"]["url"] == "http://agents-taskboard-delegate:8003"


def test_mindmap_wiring(layered_values):
    v = layered_values(REL)
    assert v["postgres"]["existingSecret"] == "agents-mindmap-db-cluster-app"
    assert v["postgres"]["existingSecretKey"] == "uri"
    assert not v["postgres"].get("url")
    assert v["orchestrator"]["tokenSecret"] == {
        "name": TOKEN_SECRETS["orchestrator-token"],
        "key": "token",
    }
    assert v["relayTokenSecret"] == {"name": TOKEN_SECRETS["relay-token"], "key": "token"}
    assert v["taskboard"]["tokenSecret"] == {
        "name": TOKEN_SECRETS["taskboard-token"],
        "key": "token",
    }
    # no mindmap delegate token: the delegate listener has no caller in this epic
    assert not v.get("delegate", {}).get("tokensSecret", {}).get("name")


def test_notifier_and_embedder_on(layered_values):
    v = layered_values(REL)
    assert v["notifier"]["enabled"] is True
    assert v["embedder"]["enabled"] is True
    # the TEI Service of the agents-embeddings release (port 80, no ingress)
    assert v["embedder"]["url"] == "http://agents-embeddings.agents.svc"


def test_no_token_literal_in_plain_values(repo_root):
    for path in (repo_root / "values" / REL).glob("*.yaml"):
        if path.name.endswith(".secrets.yaml"):
            continue
        assert not re.search(r"\b[0-9a-f]{64}\b", path.read_text()), path


def test_mindmap_tokens_distinct_secrets(repo_root):
    """Three tokens, each in one raw Secret (consumer) and one verifier file."""
    raw = repo_root / RAW
    names = {}
    for kind, secret in TOKEN_SECRETS.items():
        files = list(raw.glob(f"{kind}.{REL}.pre.secrets.yaml"))
        assert len(files) == 1, kind
        doc = yaml.safe_load(files[0].read_text())
        assert doc["sops"]["pgp"]
        assert set(doc) == {"apiVersion", "kind", "metadata", "type", "stringData", "sops"}
        assert doc["stringData"]["token"].startswith("ENC[")
        names[kind] = secret
    assert len(set(names.values())) == 3
    # verifier files: orchestrator holds the sha256 of the service token and the raw relay token;
    # the taskboard holds the mindmap token; each file is listed by exactly one release
    orch = _secrets_of(repo_root, "agents-orchestrator")
    assert "values/agents-orchestrator/service-token-agents-mindmap.secrets.yaml" in orch
    tb = _secrets_of(repo_root, "agents-taskboard")
    assert "values/agents-taskboard/mindmap-token.secrets.yaml" in tb
    for rel in (
        "values/agents-orchestrator/service-token-agents-mindmap.secrets.yaml",
        "values/agents-orchestrator/mindmap-token.secrets.yaml",
        "values/agents-taskboard/mindmap-token.secrets.yaml",
    ):
        assert (repo_root / rel).is_file(), rel
    consumers = [
        r
        for r in re.findall(r"^  - name: (\S+)", (repo_root / "helmfile.yaml.gotmpl").read_text(), re.M)
        if "values/agents-taskboard/mindmap-token.secrets.yaml" in _block(repo_root, r)
    ]
    assert consumers == ["agents-taskboard"]


def test_mindmap_ingress_path(layered_values):
    ing = layered_values(REL)["ingress"]
    assert ing["enabled"] is True
    assert ing["className"] == "nginx"
    assert ing["hosts"] == [{"host": HOST, "paths": [{"path": "/mindmap", "pathType": "Prefix"}]}]
    assert ing["tls"] == [{"secretName": "agents-orchestrator-tls", "hosts": [HOST]}]
    ann = ing["annotations"]
    assert ann["nginx.ingress.kubernetes.io/denylist-source-range"] == ",".join(NODE_IPS)
    assert not any("cert-manager" in k or "tls-acme" in k for k in ann)


def test_mindmap_sso_on_web_only(layered_values):
    v = layered_values(REL)
    orch = layered_values("agents-orchestrator")["components"]["api"]["podAnnotations"]
    o = v["oauth2Proxy"]
    assert o["enabled"] is True
    assert o["redeemUrl"] == orch["oauth2-proxy-admission/redeem-url"]
    assert o["secretName"] == orch["oauth2-proxy-admission/secret-name"]
    assert o["secretNamespace"] == orch["oauth2-proxy-admission/secret-namespace"]
    assert v["allowedCidrs"] == ["127.0.0.1/32"]
    for other in ("service", "delegate"):
        assert not v.get("components", {}).get(other, {}).get("podAnnotations")


def test_taskboard_listed_secret_is_a_token_list_for_the_mindmap_caller(repo_root):
    doc = yaml.safe_load(
        (repo_root / "values/agents-taskboard/mindmap-token.secrets.yaml").read_text()
    )
    assert set(doc) == {"mindmapTokens", "sops"}
    assert doc["mindmapTokens"].startswith("ENC[")

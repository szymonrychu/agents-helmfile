"""The agents-embeddings release: a local chart running Hugging Face TEI with the e5 model.

Sizing comes from the Task 20 spike (.scratchpad/research-mindmaps/tei-spike.md): RSS 4.5 GB
after batches, so requests 5Gi and limits 6Gi. The model is public, so there is no Secret.
"""

import re
import shutil
import subprocess

import pytest
import yaml

REL = "agents-embeddings"
MODEL = "intfloat/multilingual-e5-large-instruct"
IMAGE_RE = re.compile(
    r"^ghcr\.io/huggingface/text-embeddings-inference:cpu-1\.9\.4@sha256:[0-9a-f]{64}$"
)


def _block(repo_root, name: str) -> str:
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    match = re.search(rf"  - name: {name}\n(.*?)(?=\n  - name: |\Z)", text, re.DOTALL)
    assert match, f"release {name} not declared"
    return match.group(1)


def _values(layered_values) -> dict:
    return layered_values(REL)


def _mem(value: str) -> int:
    units = {"Mi": 2**20, "Gi": 2**30}
    return int(value[:-2]) * units[value[-2:]]


def test_release_declared(repo_root):
    block = _block(repo_root, REL)
    assert re.search(r"^    chart: charts/agents-embeddings$", block, re.MULTILINE)
    assert re.search(r"^    namespace: agents$", block, re.MULTILINE)
    assert "<<: *default" in block
    assert "needs:" not in block, "TEI has no database and needs no other release"


def test_local_chart_has_no_secret_or_ingress(repo_root):
    templates = list((repo_root / "charts" / REL / "templates").glob("*.yaml"))
    assert templates, "chart has no templates"
    kinds = {
        doc["kind"]
        for path in templates
        for doc in yaml.safe_load_all(_strip_tpl(path.read_text()))
    }
    assert {"Deployment", "Service", "PersistentVolumeClaim"} <= kinds
    assert not kinds & {"Ingress", "Secret"}


def _strip_tpl(text: str) -> str:
    """Drop Go-template lines so the skeleton parses as YAML (kind detection only)."""
    return "\n".join(
        line for line in text.splitlines() if "{{" not in line or re.match(r"^\w+:", line)
    )


def test_embeddings_image_tag_and_digest(layered_values):
    image = _values(layered_values)["image"]
    assert IMAGE_RE.match(image), image


def test_embeddings_no_ingress(layered_values):
    values = _values(layered_values)
    assert not values.get("ingress", {}).get("enabled", False)
    assert values["service"] == {"port": 80, "containerPort": 8080}


def test_embeddings_resources_set(layered_values):
    res = _values(layered_values)["resources"]
    assert res["requests"]["cpu"]
    assert res["requests"]["memory"] == "5Gi"
    assert _mem(res["limits"]["memory"]) >= _mem(res["requests"]["memory"])
    assert res["limits"]["memory"] == "6Gi"


def test_embeddings_args_no_served_name(layered_values):
    args = _values(layered_values)["args"]
    assert args[args.index("--model-id") + 1] == MODEL
    assert not [a for a in args if a.startswith("--served-model-name")]


def test_embeddings_pvc(layered_values):
    pvc = _values(layered_values)["persistence"]
    assert pvc["enabled"] is True
    assert pvc["mountPath"] == "/data"
    assert pvc["storageClass"] == "rook-ceph"
    assert _mem(pvc["size"]) >= 5 * 2**30


def test_embeddings_health_probes(layered_values):
    probes = _values(layered_values)["probes"]
    assert probes["readiness"]["path"] == "/health"
    assert probes["liveness"]["path"] == "/health"
    assert probes["startup"]["path"] == "/health"


def test_embeddings_no_secret_values(repo_root):
    assert not list((repo_root / "values" / REL).glob("*.secrets.yaml"))


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")
def test_chart_renders(repo_root):
    out = subprocess.run(
        [
            "helm", "template", REL, str(repo_root / "charts" / REL),
            "-f", str(repo_root / "values" / REL / "common.yaml"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    docs = {d["kind"]: d for d in yaml.safe_load_all(out) if d}
    assert set(docs) == {"Deployment", "Service", "PersistentVolumeClaim"}
    ctr = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert IMAGE_RE.match(ctr["image"])
    assert ctr["args"][ctr["args"].index("--model-id") + 1] == MODEL
    assert ctr["readinessProbe"]["httpGet"]["path"] == "/health"
    assert {m["mountPath"] for m in ctr["volumeMounts"]} == {"/data", "/tmp"}
    assert docs["Service"]["spec"]["ports"][0]["port"] == 80
    assert "type" not in docs["Service"]["spec"]
    assert docs["Deployment"]["spec"]["strategy"]["type"] == "Recreate"


def test_embeddings_security_context(layered_values):
    sc = _values(layered_values)["securityContext"]
    pod, ctr = sc["pod"], sc["container"]
    assert pod["runAsNonRoot"] is True
    assert pod["runAsUser"] > 0
    assert pod["fsGroup"] > 0
    assert pod["seccompProfile"] == {"type": "RuntimeDefault"}
    assert ctr["allowPrivilegeEscalation"] is False
    assert ctr["readOnlyRootFilesystem"] is True
    assert ctr["capabilities"] == {"drop": ["ALL"]}
    args = _values(layered_values)["args"]
    assert int(args[args.index("--port") + 1]) >= 1024


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")
def test_chart_renders_security_context(repo_root):
    out = subprocess.run(
        [
            "helm", "template", REL, str(repo_root / "charts" / REL),
            "-f", str(repo_root / "values" / REL / "common.yaml"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    docs = {d["kind"]: d for d in yaml.safe_load_all(out) if d}
    spec = docs["Deployment"]["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    ctr = spec["containers"][0]
    assert ctr["securityContext"]["readOnlyRootFilesystem"] is True
    assert ctr["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert ctr["ports"][0]["containerPort"] == 8080
    assert {m["mountPath"] for m in ctr["volumeMounts"]} == {"/data", "/tmp"}
    assert docs["Service"]["spec"]["ports"][0]["targetPort"] == "http"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")
def test_embeddings_deployment_pulls_with_regcred(repo_root):
    """The harbor proxy rewrite of the ghcr image needs the regcred pull secret."""
    out = subprocess.run(
        [
            "helm", "template", REL, str(repo_root / "charts" / REL),
            "-f", str(repo_root / "values" / "common.yaml"),
            "-f", str(repo_root / "values" / REL / "common.yaml"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    docs = {d["kind"]: d for d in yaml.safe_load_all(out) if d}
    spec = docs["Deployment"]["spec"]["template"]["spec"]
    assert spec["imagePullSecrets"] == [{"name": "regcred"}]

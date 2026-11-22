"""The `local` helmfile environment (docker-compose k3s) and the hook's local mode."""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml
from test_semver_pins import SEMVER, _image_refs

CORE = {
    "agents-orchestrator-db",
    "agents-orchestrator",
    "agents-vault-db",
    "agents-vault",
    "agents-taskboard-db",
    "agents-taskboard",
    "agents-mindmap-db",
    "agents-mindmap",
    "agents-history-db",
    "agents-history",
    "agents-usage-db",
    "agents-usage",
    "agents-web",
    "agents-embeddings",
}


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def test_local_build_has_no_sops_files(helmfile_build, tmp_path):
    docs = helmfile_build(env="local", extra_env={"AGENTS_LOCAL_STATE_DIR": str(tmp_path)})
    releases = [r for d in docs for r in d.get("releases", [])]
    assert releases
    assert not [r["name"] for r in releases if r.get("secrets")]
    assert not [s for s in _strings(releases) if s.endswith(".secrets.yaml")]


@pytest.mark.parametrize(
    ("profiles", "extra"),
    [
        ("", set()),
        ("webhook", {"agents-webhook", "agents-webhook-db"}),
        (
            "webhook,schedule,app-connector",
            {
                "agents-webhook",
                "agents-webhook-db",
                "agents-schedule",
                "agents-schedule-db",
                "agents-app-connector",
                "agents-app-connector-db",
            },
        ),
    ],
)
def test_local_installed_releases(repo_root, tmp_path, profiles, extra):
    # `helmfile build` does not evaluate `condition:`; `list` reports it as `enabled`.
    proc = subprocess.run(  # noqa: PLW1510
        ["helmfile", "-f", "helmfile.yaml.gotmpl", "-e", "local", "list", "--output", "json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AGENTS_LOCAL_STATE_DIR": str(tmp_path),
            "AGENTS_LOCAL_PROFILES": profiles,
            "KUBECONFIG": "/dev/null",
        },
    )
    assert proc.returncode == 0, proc.stderr
    installed = {r["name"] for r in json.loads(proc.stdout) if r["enabled"] and r["installed"]}
    assert installed == CORE | extra


def _run_presync(repo_root, tmp_path, env_name: str):
    """Run `.hook.sh presync` with stubbed kubectl, helm and sops that log their calls."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    for tool in ("kubectl", "sops", "helm"):
        stub = bindir / tool
        stub.write_text(f'#!/bin/bash\necho "{tool} $*" >> "{calls}"\ncat > /dev/null || true\n')
        stub.chmod(0o755)
    # helm list must answer JSON for the release health check.
    (bindir / "helm").write_text("#!/bin/bash\necho '[]'\n")
    shutil.copy(repo_root / ".hook.sh", tmp_path / ".hook.sh")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "regcred.pre.secrets.yaml").write_text("x: y\n")
    (raw / "marked.pre.yaml").write_text("# agents-local: skip\nkind: X\n")
    (raw / "plain.pre.yaml").write_text("kind: Y\n")
    env = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
    env["PATH"] = f"{bindir}:{os.environ['PATH']}"
    proc = subprocess.run(  # noqa: PLW1510
        [
            str(tmp_path / ".hook.sh"),
            str(tmp_path),
            "presync",
            "agents-orchestrator",
            "agents",
            "1",
            "600",
            env_name,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return calls.read_text() if calls.exists() else ""


def test_hook_local_skips_secrets_and_marked_files(repo_root, tmp_path):
    calls = _run_presync(repo_root, tmp_path, "local")
    assert "sops" not in calls
    assert "marked.pre.yaml" not in calls
    assert "plain.pre.yaml" in calls


def test_hook_default_env_still_applies_secrets(repo_root, tmp_path):
    calls = _run_presync(repo_root, tmp_path, "default")
    assert "sops -d" in calls and "regcred.pre.secrets.yaml" in calls
    # Every raw file is applied: the stubs read stdin, so a loop fed on stdin lost the rest.
    assert "marked.pre.yaml" in calls and "plain.pre.yaml" in calls


# Local values layers, raw manifests and traefik config (S-82 T-644): one render of
# `helmfile -e local template` with every profile on, reused by every test below.

ALL_PROFILES = "webhook,schedule,app-connector"
HTTP_PORT = 9090
POD_CIDR = "10.42.0.0/16"
MIDDLEWARE = "agents-agents-local-user@kubernetescrd"
MIDDLEWARE_KEY = "traefik.ingress.kubernetes.io/router.middlewares"
APP_RELEASES = (
    "agents-orchestrator",
    "agents-vault",
    "agents-taskboard",
    "agents-mindmap",
    "agents-history",
    "agents-usage",
    "agents-web",
    "agents-webhook",
    "agents-schedule",
    "agents-app-connector",
)
DB_RELEASES = tuple(f"{r}-db" for r in APP_RELEASES if r != "agents-web")
# Ingress name -> carries the fixed-identity middleware. The webhook ingest and the app-connector
# device endpoints are machine-facing (endpoint key, device bearer), never behind the identity.
INGRESSES = {
    "agents-orchestrator": True,
    "agents-vault": True,
    "agents-taskboard": True,
    "agents-mindmap": True,
    "agents-history": True,
    "agents-usage": True,
    "agents-web": True,
    "agents-schedule": True,
    "agents-webhook-api": True,
    "agents-webhook-ingest": False,
    "agents-app-connector-admin": True,
    "agents-app-connector-device": False,
}
# helm errors that mean the Harbor chart registry is unreachable or refuses this client.
HARBOR_UNAVAILABLE = (
    "unauthorized",
    "401",
    "403",
    "no such host",
    "connection refused",
    "i/o timeout",
    "TLS handshake timeout",
)
OUTPUT_DIR = re.compile(r"^helmfile\.yaml-[0-9a-f]+-(?P<release>.+)$")


@pytest.fixture(scope="module")
def local_render(repo_root, tmp_path_factory) -> dict[str, list[dict]]:
    """`helmfile -e local template` with every profile on, as release -> rendered documents.

    The state dir comes from a tokens.py run with canary credentials; nothing touches SOPS.
    """
    if shutil.which("helmfile") is None:
        pytest.fail("helmfile is not on PATH (run under `mise exec`/`mise run`)")
    tmp = tmp_path_factory.mktemp("local-render")
    state, out = tmp / "state", tmp / "out"
    canary = {
        "PATH": os.environ["PATH"],
        "HARBOR_USERNAME": "CANARY-harbor-user",
        "HARBOR_PASSWORD": "CANARY-harbor-pass",
        "CLAUDE_CODE_OAUTH_TOKEN": "CANARY-claude-token",
    }
    script = repo_root / "local" / "tokens.py"
    tokens = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(script), "--state", str(state), "--profiles", ALL_PROFILES]
        + ["--http-port", str(HTTP_PORT)],
        env=canary,
        capture_output=True,
        text=True,
    )
    assert tokens.returncode == 0, tokens.stderr
    # Hermetic: no reachable cluster, so the prepare hook skips its kubectl diff (the in-cluster
    # service account fallback is cut too).
    env = {k: v for k, v in os.environ.items() if not k.startswith("KUBERNETES_SERVICE_")}
    env |= {
        "KUBECONFIG": "/dev/null",
        "AGENTS_LOCAL_STATE_DIR": str(state),
        "AGENTS_LOCAL_PROFILES": ALL_PROFILES,
    }
    proc = subprocess.run(  # noqa: PLW1510
        ["helmfile", "-f", "helmfile.yaml.gotmpl", "-e", "local", "template"]
        + ["--output-dir", str(out)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=env,
    )
    harbor_down = "harbor.szymonrichert.pl" in proc.stderr and any(
        marker in proc.stderr for marker in HARBOR_UNAVAILABLE
    )
    if proc.returncode != 0 and harbor_down and os.environ.get("CI"):
        pytest.fail(f"Harbor chart registry unavailable in CI:\n{proc.stderr}")
    if proc.returncode != 0 and harbor_down:
        pytest.skip("Harbor chart registry unreachable or unauthenticated (CI has the login)")
    assert proc.returncode == 0, f"helmfile -e local template failed:\n{proc.stderr}"
    rendered: dict[str, list[dict]] = {}
    for rel_dir in out.iterdir():
        match = OUTPUT_DIR.match(rel_dir.name)
        assert match, rel_dir.name
        rendered[match["release"]] = [
            doc
            for path in sorted(rel_dir.rglob("*.yaml"))
            for doc in yaml.safe_load_all(path.read_text())
            if isinstance(doc, dict)
        ]
    return rendered


def _kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def _pod_templates(docs: list[dict]) -> list[dict]:
    return [d["spec"]["template"] for d in docs if d.get("kind") in ("Deployment", "StatefulSet")]


def _all_docs(local_render: dict[str, list[dict]]) -> list[dict]:
    return [d for docs in local_render.values() for d in docs]


@pytest.mark.parametrize("release", ["agents-taskboard", "agents-mindmap", "agents-webhook"])
def test_local_public_urls_are_localhost(local_render, release):
    text = yaml.safe_dump(local_render[release])
    assert f"http://localhost:{HTTP_PORT}" in text
    assert "lab.szymonrichert.pl" not in text


@pytest.mark.parametrize("release", APP_RELEASES)
def test_local_no_oauth2_annotations(local_render, release):
    pods = _pod_templates(local_render[release])
    assert pods, f"{release}: no pod template rendered"
    bad = [
        key
        for pod in pods
        for key in (pod["metadata"].get("annotations") or {})
        if key.startswith("oauth2-proxy-admission/")
    ]
    assert not bad, f"{release} pods carry oauth2-proxy annotations: {bad}"


@pytest.mark.parametrize(("name", "identity"), sorted(INGRESSES.items()))
def test_local_ingress_traefik_localhost(local_render, name, identity):
    found = [i for i in _kind(_all_docs(local_render), "Ingress") if i["metadata"]["name"] == name]
    assert len(found) == 1, f"Ingress {name} rendered {len(found)} times"
    ing = found[0]
    ann = ing["metadata"].get("annotations") or {}
    assert ing["spec"]["ingressClassName"] == "traefik"
    assert {r["host"] for r in ing["spec"]["rules"]} == {"localhost"}
    assert not ing["spec"].get("tls")
    assert not [k for k in ann if k.startswith("cert-manager.io/") or "tls-acme" in k]
    assert "nginx.ingress.kubernetes.io/denylist-source-range" not in ann
    assert (ann.get(MIDDLEWARE_KEY) == MIDDLEWARE) is identity


@pytest.mark.parametrize("db", DB_RELEASES)
def test_local_db_has_no_backup_or_wal(local_render, db):
    docs = local_render[db]
    (cnpg,) = [d for d in docs if d.get("kind") == "Cluster"]
    spec = cnpg["spec"]
    assert list(spec["bootstrap"]) == ["initdb"]
    assert spec["storage"] == {"size": "1Gi", "storageClass": "local-path"}
    assert "walStorage" not in spec
    assert not spec.get("plugins")
    assert "backup" not in spec
    assert not _kind(docs, "ScheduledBackup")
    assert not _kind(docs, "ObjectStore")
    assert not [s for s in _kind(docs, "Secret") if "s3" in s["metadata"]["name"]]


def test_local_mindmap_db_keeps_pgvector(local_render):
    (cnpg,) = [d for d in local_render["agents-mindmap-db"] if d.get("kind") == "Cluster"]
    sql = cnpg["spec"]["bootstrap"]["initdb"]["postInitApplicationSQL"]
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql


def test_local_orchestrator_settings(local_render):
    docs = local_render["agents-orchestrator"]
    settings = {k: v for cm in _kind(docs, "ConfigMap") for k, v in (cm.get("data") or {}).items()}
    env_names = {
        e["name"]
        for pod in _pod_templates(docs)
        for ctr in [*pod["spec"].get("initContainers", []), *pod["spec"]["containers"]]
        for e in ctr.get("env", [])
    }
    names = {*settings, *env_names}
    assert settings["ORCH_PVC_STORAGE_CLASS"] == "agents-local-rwx"
    assert settings["ORCH_PVC_SIZE"] == "2Gi"
    assert settings["ORCH_ALLOWED_CIDRS"] == POD_CIDR
    assert settings["ORCH_AGENT_TERMINATION_GRACE_SECONDS"] == "60"
    assert not [k for k in names if k.startswith(("ORCH_CACHE_", "ORCH_CHAT_"))]
    # No session Kubernetes access: every rendered RBAC object is namespaced to agents
    # (ClusterRoleBinding and its role kind carry no namespace).
    rbac = [d for d in docs if d["kind"].endswith(("Role", "RoleBinding"))]
    assert rbac and {d["metadata"].get("namespace") for d in rbac} == {"agents"}


@pytest.mark.parametrize("release", APP_RELEASES)
def test_local_images_semver(local_render, release):
    refs = _image_refs(local_render[release])
    assert refs, f"{release}: no containers image rendered"
    bad = [f"{p}: {t!r}" for p, t in refs if t is None or not SEMVER.match(t)]
    assert not bad, f"{release} deploys non-X.Y.Z containers tags locally: {bad}"


def _raw_docs(repo_root, rel: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((repo_root / rel).read_text()) if isinstance(d, dict)]


def test_local_raw_manifests(repo_root):
    def kinds(rel: str) -> set[tuple]:
        return {
            (d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"])
            for d in _raw_docs(repo_root, rel)
        }

    assert kinds("local/raw/storageclass-rwx.yaml") == {
        ("StorageClass", None, "agents-local-rwx"),
        ("ConfigMap", "kube-system", "local-path-config"),
    }
    assert kinds("local/raw/middleware-local-user.yaml") == {
        ("Middleware", "agents", "agents-local-user"),
    }
    assert kinds("local/k3s/traefik-config.yaml") == {
        ("HelmChartConfig", "kube-system", "traefik"),
    }
    policies = kinds("local/raw/networkpolicies.yaml")
    assert policies and {(k, ns) for k, ns, _ in policies} == {("NetworkPolicy", "agents")}
    (mw,) = _raw_docs(repo_root, "local/raw/middleware-local-user.yaml")
    assert mw["spec"]["headers"]["customRequestHeaders"] == {
        "X-Forwarded-User": "local",
        "X-Forwarded-Email": "local@localhost",
    }


def test_networkpolicies_select_rendered_pods(repo_root, local_render):
    pods = [(p["metadata"].get("labels") or {}, p) for p in _pod_templates(_all_docs(local_render))]
    for policy in _raw_docs(repo_root, "local/raw/networkpolicies.yaml"):
        name = policy["metadata"]["name"]
        selector = policy["spec"]["podSelector"]["matchLabels"]
        matched = [p for labels, p in pods if selector.items() <= labels.items()]
        assert matched, f"{name} selects no rendered pod"
        # Every container port of the selected pods is admitted by exactly one rule.
        ports = {
            port["containerPort"]
            for pod in matched
            for ctr in pod["spec"]["containers"]
            for port in ctr.get("ports", [])
        }
        allowed = [p["port"] for rule in policy["spec"]["ingress"] for p in rule["ports"]]
        assert sorted(allowed) == sorted(ports), f"{name}: rules {allowed} != pod ports {ports}"

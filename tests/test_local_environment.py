"""The `local` helmfile environment (docker-compose k3s) and the hook's local mode."""

import json
import os
import shutil
import subprocess

import pytest

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
    assert "marked.pre.yaml" in calls

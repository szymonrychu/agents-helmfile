"""Script behaviour guards: the kube-context safety task and the helmfile prepare hook."""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tasks() -> dict:
    return tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]


def _run(task: dict) -> str:
    run = task.get("run", "")
    return "\n".join(run) if isinstance(run, list) else run


def _run_guard(tasks, tmp_path, context: str, env_extra: dict | None = None):

    fake = tmp_path / "kubectl"
    fake.write_text(
        '#!/bin/bash\nif [ "$1" = config ]; then echo "$FAKE_CTX"; exit 0; fi\nexit 0\n'
    )
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("EXPECTED_KUBE_CONTEXT", "BASH_ENV")}
    env.update({"PATH": f"{tmp_path}:{env['PATH']}", "FAKE_CTX": context, **(env_extra or {})})
    return subprocess.run(
        ["bash", "-c", _run(tasks["kube-context"])],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )



def test_kube_context_guard_rejects_unexpected_context(tasks, tmp_path):
    bad = _run_guard(tasks, tmp_path, "prod-cluster")
    assert bad.returncode != 0
    assert "prod-cluster" in bad.stderr
    ok = _run_guard(tasks, tmp_path, "kubernetes-admin@kubernetes")
    assert ok.returncode == 0, ok.stderr
    override = _run_guard(tasks, tmp_path, "other", {"EXPECTED_KUBE_CONTEXT": "other"})
    assert override.returncode == 0, override.stderr


def _run_prepare(repo_root, tmp_path, diff_stderr, diff_rc):
    """Run `.hook.sh prepare` with a stubbed kubectl whose `diff` fails as given."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(
        '#!/bin/bash\n[[ "$1" == "diff" ]] || exit 0\n'
        f"printf '%s\\n' '{diff_stderr}' >&2\nexit {diff_rc}\n"
    )
    decryptor = bindir / "sops"
    decryptor.write_text("#!/bin/bash\necho 'kind: Secret'\n")
    for f in (kubectl, decryptor):
        f.chmod(0o755)
    shutil.copy(repo_root / ".hook.sh", tmp_path / ".hook.sh")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "regcred.pre.secrets.yaml").write_text("x: y\n")
    # Drop BASH_ENV so shell startup files cannot put real tools ahead of the stubs.
    env = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
    env["PATH"] = f"{bindir}:{os.environ['PATH']}"
    return subprocess.run(  # noqa: PLW1510
        [
            str(tmp_path / ".hook.sh"),
            str(tmp_path),
            "prepare",
            "agents-orchestrator",
            "agents",
            "1",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )


def test_prepare_aborts_on_other_kubectl_errors(repo_root, tmp_path):
    proc = _run_prepare(repo_root, tmp_path, "error: connection refused", 2)
    assert proc.returncode == 1
    assert "kubectl diff failed (rc=2)" in proc.stderr



def test_prepare_aborts_on_other_kubectl_errors(repo_root, tmp_path):
    proc = _run_prepare(repo_root, tmp_path, "error: connection refused", 2)
    assert proc.returncode == 1
    assert "kubectl diff failed (rc=2)" in proc.stderr

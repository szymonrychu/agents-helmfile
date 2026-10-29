"""Trust model of the public CI workflow: who gets secrets, what is cleaned up, what is printed."""

import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yaml"
SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
FORK = "github.event.pull_request.head.repo.full_name != github.repository"
GNUPGHOME = "${{ runner.temp }}/gnupg"


def _workflow() -> dict:
    return yaml.safe_load(CI.read_text())


def _jobs() -> dict:
    return _workflow()["jobs"]


def _secret_job() -> dict:
    return _jobs()["ci"]


def _fork_job() -> dict:
    return _jobs()["fork-lint"]


def _runs(job: dict) -> list[str]:
    return [step.get("run", "") for step in job["steps"]]


def _step_index(job: dict, needle: str) -> int:
    hits = [i for i, run in enumerate(_runs(job)) if needle in run]
    assert len(hits) == 1, f"expected one step running {needle!r}, got {len(hits)}"
    return hits[0]


def test_no_pull_request_target_anywhere():
    for path in WORKFLOWS.glob("*.y*ml"):
        assert "pull_request_target" not in path.read_text(), path
    wf = _workflow()
    # PyYAML reads the bare key `on` as the boolean True.
    assert set(wf.get("on", wf.get(True))) == {"push", "pull_request", "workflow_dispatch"}


def test_secret_job_runs_only_for_push_dispatch_and_same_repo_prs():
    assert _secret_job()["if"] == f"github.event_name != 'pull_request' || {SAME_REPO}"
    assert "secrets." in yaml.safe_dump(_secret_job())


def test_fork_pr_gets_a_secret_free_lint_job():
    job = _fork_job()
    assert job["if"] == f"github.event_name == 'pull_request' && {FORK}"
    assert "secrets." not in yaml.safe_dump(job)
    runs = _runs(job)
    assert "mise run lint" in runs
    assert not [r for r in runs if re.search(r"mise run (test|check-pins)", r)]
    notice = runs[_step_index(job, "::notice::")]
    assert "lint only" in notice
    assert "after a maintainer merges or re-pushes" in notice and "same-repo branch" in notice
    # The notice is the first run step, so the log states it even when lint fails.
    assert _step_index(job, "::notice::") < _step_index(job, "mise run lint")


def test_only_the_secret_job_references_secrets():
    for name, job in _jobs().items():
        if name != "ci":
            assert "secrets." not in yaml.safe_dump(job), name


def test_checkout_keeps_no_token_in_git_config():
    for name, job in _jobs().items():
        checkouts = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
        assert checkouts, name
        for step in checkouts:
            assert step.get("with", {}).get("persist-credentials") is False, name


def test_final_step_always_removes_the_key_and_the_harbor_login():
    job = _secret_job()
    last = job["steps"][-1]
    assert last["if"] == "always()"
    assert "gpgconf --kill gpg-agent" in last["run"]
    assert 'rm -rf "$GNUPGHOME"' in last["run"]
    assert 'helm registry logout "$HARBOR_URL"' in last["run"]
    # Every cleanup command runs even when an earlier one fails; the step still reports it.
    assert "set -e" not in last["run"] and "exit $rc" in last["run"]
    # The cleanup and the import name the same GNUPGHOME, so the cleanup never depends on
    # the import step having exported it.
    imp = job["steps"][_step_index(job, "gpg --batch --quiet --import")]
    assert imp["env"]["GNUPGHOME"] == GNUPGHOME
    assert last["env"]["GNUPGHOME"] == GNUPGHOME


def test_lint_and_check_pins_run_before_any_secret_reaches_the_runner():
    job = _secret_job()
    first_secret = min(
        _step_index(job, "helm registry login"), _step_index(job, "gpg --batch --quiet --import")
    )
    assert _step_index(job, "mise run lint") < first_secret
    assert _step_index(job, "mise run check-pins") < first_secret
    assert _step_index(job, "mise run test") > first_secret


def test_no_xtrace_in_any_step():
    text = CI.read_text()
    assert not re.search(r"set\s+-\w*x|set\s+-o\s+xtrace|bash\s+-\w*x", text)
    for job in _jobs().values():
        assert "shell" not in job.get("defaults", {}).get("run", {})
        for step in job["steps"]:
            assert "shell" not in step


def _test_step_args() -> list[str]:
    job = _secret_job()
    run = _runs(job)[_step_index(job, "mise run test")].strip()
    head, sep, tail = run.partition(" -- ")
    assert head == "mise run test" and sep, run
    return shlex.split(tail)


LEAKY_TESTS = """
import logging
import warnings

import pytest
import yaml

FAKE = "FAKE-DECRYPTED-VALUE"


def test_len_diff():
    secrets = [{"kind": "Secret", "data": {"t": FAKE}}, {"kind": "Secret"}]
    assert len(secrets) == 1


def test_value_diff():
    assert FAKE == "expected"


def test_message():
    assert False, FAKE + "\\nhelmfile stderr"


def test_captured_output():
    print(FAKE)
    logging.getLogger("x").error(FAKE)
    warnings.warn(FAKE)
    assert False


def test_yaml_snippet():
    list(yaml.safe_load_all("a: b\\nc: " + FAKE + ": x\\n"))


@pytest.fixture
def broken():
    raise RuntimeError(FAKE)


def test_setup_error(broken):
    pass


def test_passes():
    pass
"""


def test_ci_test_flags_keep_every_failure_detail_off_the_log(tmp_path):
    """Run the CI test flags on failing tests that hold a fake decrypted value, as CI does."""
    addopts = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"][
        "addopts"
    ]
    (tmp_path / "pytest.ini").write_text(f"[pytest]\naddopts = {addopts}\n")
    (tmp_path / "test_leaky.py").write_text(LEAKY_TESTS)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *_test_step_args()],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "CI": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "FAKE-DECRYPTED-VALUE" not in out, "a fake decrypted value reached the log"
    # The log still names every failing test.
    for name in ("test_len_diff", "test_value_diff", "test_message", "test_captured_output"):
        assert f"test_leaky.py::{name} FAILED" in out, out
    assert "test_leaky.py::test_yaml_snippet FAILED" in out, out
    assert "test_leaky.py::test_setup_error ERROR" in out, out
    assert "5 failed, 1 passed" in out, out


def test_ci_never_reaches_a_kubernetes_api():
    env = _workflow()["env"]
    # No kubeconfig, and no in-cluster fallback: kubectl reads the service account of a pod
    # when KUBERNETES_SERVICE_HOST is set and the kubeconfig is empty.
    assert env["KUBECONFIG"] == "/dev/null"
    assert env["KUBERNETES_SERVICE_HOST"] == ""
    assert env["KUBERNETES_SERVICE_PORT"] == ""
    forbidden = re.compile(
        r"\b(helmfile|helm|kubectl)\b[^\n]*\b(apply|sync|diff|destroy|delete|install|upgrade)\b"
        r"|\bmise run (apply|diff|smoke|chat-smoke|kube-context)\b"
    )
    for name, job in _jobs().items():
        for run in _runs(job):
            assert not forbidden.search(run), f"{name}: {run}"
    job = _secret_job()
    guard = _step_index(job, "kubectl cluster-info")
    assert "exit 1" in _runs(job)[guard]
    assert guard < _step_index(job, "helm registry login")
    assert guard < _step_index(job, "gpg --batch --quiet --import")
    assert guard < _step_index(job, "mise run test")

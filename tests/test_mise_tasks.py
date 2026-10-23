"""mise.toml operator tasks and the README that documents them."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tasks() -> dict:
    return tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]


def _run(task: dict) -> str:
    run = task.get("run", "")
    return "\n".join(run) if isinstance(run, list) else run


def test_core_tasks_exist(tasks):
    for name in ("diff", "apply", "lint", "test", "kube-context"):
        assert name in tasks, name


def test_diff_and_apply_tasks(tasks):
    diff, apply_ = _run(tasks["diff"]), _run(tasks["apply"])
    assert "helmfile -f helmfile.yaml.gotmpl" in diff
    assert "diff" in diff
    assert "--detailed-exitcode" in diff
    assert "--suppress-secrets" in diff
    assert "helmfile -f helmfile.yaml.gotmpl" in apply_
    assert "apply" in apply_
    assert "--suppress-secrets" in apply_
    # both forward `--selector name=<release>` through the task arguments
    assert "usage" in tasks["diff"] or "$@" in diff or "${@" in diff
    assert "usage" in tasks["apply"] or "$@" in apply_ or "${@" in apply_
    assert "kube-context" in tasks["diff"]["depends"]
    assert "kube-context" in tasks["apply"]["depends"]


def test_apply_depends_on_lint_and_test(tasks):
    assert {"lint", "test", "kube-context"} <= set(tasks["apply"]["depends"])


def test_kube_context_guard(tasks):
    body = _run(tasks["kube-context"])
    assert "kubectl config current-context" in body
    assert "kubectl version" in body


def test_no_task_decrypts_to_disk(tasks):
    for name, task in tasks.items():
        body = _run(task)
        assert "bootstrap-secrets" not in body or name == "bootstrap", name
        for line in body.splitlines():
            if re.search(r"\b(sops|helm secrets)\b.*\b(-d|--decrypt|dec)\b", line):
                assert not re.search(r"(>|--output|--output-file|tee)\s", line), line


def test_env_file_loaded_through_env_block():
    mise = tomllib.loads((ROOT / "mise.toml").read_text())
    assert mise["env"]["_"]["file"]["path"] == ".env"


def test_readme_names_every_task(tasks):
    readme = (ROOT / "README.md").read_text()
    assert "## Operating" in readme
    for name in {*tasks, "smoke"}:
        assert f"mise run {name}" in readme or f"`{name}`" in readme, name
    for word in ("agents", "agents.szymonrichert.pl", "LAN", ".env", "SOPS"):
        assert word in readme, word

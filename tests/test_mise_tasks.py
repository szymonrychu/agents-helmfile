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
    for word in ("agents", "lab.szymonrichert.pl", "node IPs", "oauth2-proxy", ".env", "SOPS"):
        assert word in readme, word


def _run_guard(tasks, tmp_path, context: str, env_extra: dict | None = None):
    import os
    import subprocess

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
    assert "kube-context" in tasks["diff"]["depends"]
    assert "kube-context" in tasks["apply"]["depends"]
    bad = _run_guard(tasks, tmp_path, "prod-cluster")
    assert bad.returncode != 0
    assert "prod-cluster" in bad.stderr
    ok = _run_guard(tasks, tmp_path, "kubernetes-admin@kubernetes")
    assert ok.returncode == 0, ok.stderr
    override = _run_guard(tasks, tmp_path, "other", {"EXPECTED_KUBE_CONTEXT": "other"})
    assert override.returncode == 0, override.stderr
    assert "kubernetes-admin@kubernetes" in _run(tasks["kube-context"])


def test_tools_pinned():
    mise = tomllib.loads((ROOT / "mise.toml").read_text())
    for tool, version in mise["tools"].items():
        assert str(version) != "latest", tool
        assert re.match(r"^\d+(\.\d+)*$", str(version)), (tool, version)
    hooks = "\n".join(mise["hooks"]["postinstall"])
    diff_line = next(line for line in hooks.splitlines() if "helm-diff" in line)
    assert re.search(r"--version v?\d+\.\d+\.\d+", diff_line), diff_line


def test_apply_depends_on_check_pins(tasks):
    assert "check-pins" in tasks["apply"]["depends"]
    check = tasks["check-pins"]
    assert _run(check) == "uv run scripts/check_pins.py"
    assert "check runs" in check["description"]
    for name in ("agents-orchestrator", "agents-web", "agents-webhook", "agents-schedule"):
        assert name in check["description"], name


def test_chat_smoke_task(tasks):
    chat = tasks["chat-smoke"]
    assert _run(chat) == "bash scripts/chat-smoke.sh"
    assert "owner DM" in chat["description"]
    # The chat smoke posts real messages: never part of `mise run smoke`.
    assert "chat-smoke" not in _run(tasks["smoke"])
    assert "chat-smoke" not in str(tasks["smoke"].get("depends", []))
    readme = (ROOT / "README.md").read_text()
    assert "mise run chat-smoke" in readme
    assert "owner DM" in readme

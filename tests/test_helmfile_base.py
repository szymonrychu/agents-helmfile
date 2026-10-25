"""Structure of the helmfile base for namespace agents."""

import os
import re
import shutil
import subprocess

import yaml


def _template(repo_root) -> dict:
    return yaml.safe_load((repo_root / "helmfile.yaml.gotmpl").read_text())


def test_default_template_layers_values(repo_root, helmfile_build, layered_values):
    docs = helmfile_build()
    assert docs, "helmfile build produced no documents"
    doc = _template(repo_root)
    assert doc["repositories"] == [
        {"name": "cnpg", "url": "https://cloudnative-pg.github.io/charts"}
    ]
    assert doc["helmDefaults"]["wait"] is True
    assert doc["helmDefaults"]["syncArgs"] == ["--rollback-on-failure"]
    assert "agent-orchestrator-db" in [r["name"] for r in doc["releases"]]
    default = doc["templates"]["default"]
    assert default["missingFileHandler"] == "Warn"
    assert default["values"] == [
        "values/common.yaml",
        "values/{{`{{ .Release.Name }}`}}/common.yaml",
        "values/{{`{{ .Release.Name }}`}}/default.yaml",
    ]
    assert default["secrets"] == ["values/{{`{{ .Release.Name }}`}}/default.secrets.yaml"]
    (hook,) = default["hooks"]
    assert hook["events"] == ["prepare", "presync", "postsync"]
    assert hook["command"] == "./.hook.sh"
    assert len(hook["args"]) == 6
    assert layered_values("nonexistent") == layered_values("nonexistent2")
    assert "namespace `agents`" in (repo_root / "helmfile.yaml.gotmpl").read_text()


def test_hook_creates_namespace_first(repo_root):
    src = (repo_root / ".hook.sh").read_text()
    create = 'kubectl create namespace "$RELEASE_NAMESPACE" --dry-run=client -o yaml'
    assert create in src
    assert "kubectl apply -f -" in src
    assert src.index(create) < src.index('apply_dir "${CURRENT_PWD}/raw"')
    assert re.search(r'"\$\{EVENT_NAME\}" == "presync"', src)


def test_hook_is_executable_and_shellcheck_clean(repo_root):
    hook = repo_root / ".hook.sh"
    assert os.access(hook, os.X_OK)
    assert shutil.which("shellcheck"), "shellcheck missing"
    proc = subprocess.run(  # noqa: PLW1510
        ["shellcheck", "--severity=error", str(hook)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_common_values_use_regcred(layered_values):
    merged = layered_values("anything")
    assert merged["imagePullSecrets"] == [{"name": "regcred"}]
    assert merged["global"]["imagePullSecrets"] == [{"name": "regcred"}]


def test_regcred_is_encrypted(repo_root):
    text = (repo_root / "raw" / "regcred.pre.secrets.yaml").read_text()
    doc = yaml.safe_load(text)
    assert "sops" in doc
    assert "ENC[AES256_GCM" in text
    assert "D39E46932A270AA3BA490B9DB9FE928D3E8BCED8" in text


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


def test_prepare_tolerates_missing_namespace(repo_root, tmp_path):
    proc = _run_prepare(
        repo_root, tmp_path, 'Error from server (NotFound): namespaces "agents" not found', 2
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "will be created by presync" in proc.stdout


def test_prepare_aborts_on_other_kubectl_errors(repo_root, tmp_path):
    proc = _run_prepare(repo_root, tmp_path, "error: connection refused", 2)
    assert proc.returncode == 1
    assert "kubectl diff failed (rc=2)" in proc.stderr


def test_hook_scripts_not_traced(repo_root):
    lines = (repo_root / ".hook.sh").read_text().splitlines()
    src = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert not re.search(r"\bbash\s+-\w*x", src)
    assert not re.search(r"^\s*set\s+-\w*x", src, re.MULTILINE)
    assert not re.search(r"set\s+-o\s+xtrace", src)

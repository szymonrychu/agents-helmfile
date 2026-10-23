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
    assert doc["releases"] == []
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

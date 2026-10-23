"""Encrypted secrets and the bootstrap script that creates them."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "bootstrap-secrets.sh"
ORCH = "values/agent-orchestrator/default.secrets.yaml"
DB = "values/agent-orchestrator-db/default.secrets.yaml"
SECRET_VARS = ("token", "master", "access", "secret", "plain", "value")


def _leaves(node, prefix=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, f"{prefix}{k}.")
    elif node is not None:
        yield prefix.rstrip("."), node


def _load(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text())


@pytest.mark.parametrize(
    ("rel", "keys"),
    [
        (ORCH, {"claude.token", "masterKey.value"}),
        (DB, {"backups.s3.accessKey", "backups.s3.secretKey"}),
    ],
)
def test_secrets_files_encrypted_with_expected_keys(rel, keys):
    doc = _load(rel)
    assert doc["sops"]["pgp"], "no pgp recipient"
    data = {k: v for k, v in doc.items() if k != "sops"}
    found = dict(_leaves(data))
    assert set(found) == keys
    for path, leaf in found.items():
        assert str(leaf).startswith("ENC["), f"{rel}:{path} is not encrypted"


def test_script_never_prints_values():
    src = SCRIPT.read_text()
    assert not re.search(r"^\s*set\s+-\w*x", src, re.MULTILINE), "xtrace enabled"
    assert "set -euo pipefail" in src
    assert "bash -x" not in src
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"\b(echo|printf)\b", stripped):
            assert not re.search(r"\$\{?(\w+)", stripped) or not any(
                v in m.lower() for m in re.findall(r"\$\{?(\w+)", stripped) for v in SECRET_VARS
            ), f"prints a secret-bearing variable: {stripped}"
    r = subprocess.run(
        ["shellcheck", "--severity=error", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stdout


def _sandbox(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "bootstrap-secrets.sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("kubectl", "sops", "yq", "jq", "openssl"):
        stub = bindir / tool
        stub.write_text(f"#!/bin/sh\necho called {tool} >> '{tmp_path}/calls'\nexit 99\n")
        stub.chmod(0o755)
    return bindir


def _run(tmp_path: Path, bindir: Path) -> subprocess.CompletedProcess:
    # BASH_ENV would re-prepend the real tool dirs and let the script reach the real cluster.
    env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}
    env["PATH"] = f"{bindir}:/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(tmp_path / "scripts" / "bootstrap-secrets.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_does_not_overwrite_existing(tmp_path):
    bindir = _sandbox(tmp_path)
    for rel in (ORCH, DB):
        p = tmp_path / rel
        p.parent.mkdir(parents=True)
        p.write_text("sentinel: keep\n")
    r = _run(tmp_path, bindir)
    assert r.returncode == 0, r.stderr
    for rel in (ORCH, DB):
        assert (tmp_path / rel).read_text() == "sentinel: keep\n"
    assert not (tmp_path / "calls").exists(), "touched kubectl/sops/openssl despite existing files"


def test_script_exits_1_naming_namespaces_when_token_missing(tmp_path):
    bindir = _sandbox(tmp_path)
    (bindir / "kubectl").write_text("#!/bin/sh\necho '{\"items\":[]}'\n")
    (bindir / "jq").unlink()
    real_jq = shutil.which("jq")
    if shutil.which("mise"):
        out = subprocess.run(["mise", "which", "jq"], capture_output=True, text=True, check=False)
        real_jq = out.stdout.strip() or real_jq
    (bindir / "jq").symlink_to(real_jq)
    r = _run(tmp_path, bindir)
    assert r.returncode == 1
    assert "tatara" in r.stderr and "magic-trainer" in r.stderr
    assert not (tmp_path / ORCH).exists()

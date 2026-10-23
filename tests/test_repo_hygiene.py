"""Repo hygiene: nothing sensitive may be tracked."""

import fnmatch
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SELF = "tests/test_repo_hygiene.py"


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def _tracked() -> list[str]:
    return [f for f in _git("ls-files").stdout.splitlines() if (ROOT / f).is_file()]


def _leaves(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _leaves(v)
    elif isinstance(node, list):
        for v in node:
            yield from _leaves(v)
    elif node is not None:
        yield node


def test_env_ignored_and_untracked():
    assert _git("check-ignore", ".env", check=False).returncode == 0
    assert not [f for f in _git("ls-files").stdout.splitlines() if Path(f).name == ".env"]


def test_secrets_files_are_encrypted():
    for f in _tracked():
        name = Path(f).name
        if not (
            fnmatch.fnmatch(name, "*.secrets.yaml") or fnmatch.fnmatch(name, "*.secret.*.yaml")
        ):
            continue
        doc = yaml.safe_load((ROOT / f).read_text())
        assert isinstance(doc, dict) and "sops" in doc, f"{f}: no sops key"
        assert doc["sops"].get("pgp"), f"{f}: no pgp entry"
        for key, value in doc.items():
            if key == "sops":
                continue
            for leaf in _leaves(value):
                assert str(leaf).startswith("ENC["), f"{f}: plaintext leaf under {key}"


PATTERNS = [
    re.compile(r"sk-ant-"),
    re.compile(r"-----BEGIN .*PRIVATE KEY-----"),
    re.compile(r"CLAUDE_CODE_OAUTH_TOKEN:\s*(?!ENC\[)\S+"),
]


def test_no_plaintext_secrets():
    for f in _tracked():
        if f == SELF:
            continue
        text = (ROOT / f).read_text(errors="ignore")
        for pat in PATTERNS:
            assert not pat.search(text), f"{f}: matches {pat.pattern}"


def test_gitleaks_clean():
    r = subprocess.run(
        ["gitleaks", "detect", "--no-git", "--source", ".", "--redact"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr

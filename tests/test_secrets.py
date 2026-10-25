"""Encrypted secrets and the bootstrap script that creates them."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "bootstrap-secrets.sh"
ORCH = "values/agents-orchestrator/default.secrets.yaml"
DB = "values/agents-orchestrator-db/default.secrets.yaml"
WH = "values/agents-webhook/default.secrets.yaml"
WH_TOKEN = "values/agents-orchestrator/service-token-agents-webhook.secrets.yaml"
SCH = "values/agents-schedule/default.secrets.yaml"
SCH_TOKEN = "values/agents-orchestrator/service-token-agents-schedule.secrets.yaml"
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
        (WH, {"orchestrator.token"}),
        (WH_TOKEN, {"serviceApi.tokenHashes.agents-webhook"}),
        (SCH, {"orchestrator.token"}),
        (SCH_TOKEN, {"serviceApi.tokenHashes.agents-schedule"}),
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
    for rel in (ORCH, DB, WH, WH_TOKEN, SCH, SCH_TOKEN):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("sentinel: keep\n")
    r = _run(tmp_path, bindir)
    assert r.returncode == 0, r.stderr
    for rel in (ORCH, DB, WH, WH_TOKEN, SCH, SCH_TOKEN):
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


def _decrypt(rel: str) -> dict:
    # In-process only: the plaintext is parsed and compared, never printed or written.
    r = subprocess.run(
        ["sops", "--decrypt", str(ROOT / rel)], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, "sops could not decrypt (key unavailable?)"
    return yaml.safe_load(r.stdout)


def test_webhook_token_hash_matches():
    token = _decrypt(WH)["orchestrator"]["token"]
    digest = _decrypt(WH_TOKEN)["serviceApi"]["tokenHashes"]["agents-webhook"]
    assert digest == hashlib.sha256(token.encode()).hexdigest()


def test_webhook_secrets_never_rotate(tmp_path):
    bindir = _sandbox(tmp_path)
    for rel in (ORCH, DB, SCH, SCH_TOKEN):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("sentinel: keep\n")
    # both present: nothing happens
    for rel in (WH, WH_TOKEN):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("sentinel: keep\n")
    r = _run(tmp_path, bindir)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "calls").exists()
    for rel in (WH, WH_TOKEN):
        assert (tmp_path / rel).read_text() == "sentinel: keep\n"
    # exactly one present: exit 1 naming both, nothing written, nothing called
    for present, absent in ((WH, WH_TOKEN), (WH_TOKEN, WH)):
        (tmp_path / absent).unlink()
        r = _run(tmp_path, bindir)
        assert r.returncode == 1
        assert WH in r.stderr and WH_TOKEN in r.stderr
        assert not (tmp_path / absent).exists()
        assert (tmp_path / present).read_text() == "sentinel: keep\n"
        assert not (tmp_path / "calls").exists()
        (tmp_path / absent).write_text("sentinel: keep\n")


def test_webhook_bootstrap_writes_no_sources(tmp_path):
    bindir = _sandbox(tmp_path)
    for tool in ("jq", "openssl"):
        (bindir / tool).unlink()
        real = shutil.which(tool)
        if tool == "jq" and shutil.which("mise"):
            out = subprocess.run(
                ["mise", "which", "jq"], capture_output=True, text=True, check=False
            )
            real = out.stdout.strip() or real
        (bindir / tool).symlink_to(real)
    # Fake sops: records the plaintext it is asked to encrypt (stays in the sandbox) and emits a stub.
    (bindir / "sops").write_text(
        '#!/bin/sh\ncat > "$(mktemp "' + str(tmp_path) + "/plain.XXXXXX\")\"\necho 'sops: stub'\n"
    )
    for rel in (ORCH, DB, SCH, SCH_TOKEN):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("sentinel: keep\n")
    r = _run(tmp_path, bindir)
    assert r.returncode == 0, r.stderr
    docs = [json.loads(f.read_text()) for f in tmp_path.glob("plain.*")]
    assert len(docs) == 2
    wh = next(d for d in docs if "orchestrator" in d)
    assert set(wh) == {"orchestrator"}
    assert set(wh["orchestrator"]) == {"token"}
    assert "sources" not in wh


def test_webhook_secrets_have_no_sources():
    doc = _decrypt(WH)
    assert set(doc) == {"orchestrator"}
    assert doc["orchestrator"]["token"]
    digest = _decrypt(WH_TOKEN)["serviceApi"]["tokenHashes"]["agents-webhook"]
    assert digest == hashlib.sha256(doc["orchestrator"]["token"].encode()).hexdigest()


def _schedule_sandbox(tmp_path: Path) -> Path:
    """Sandbox with real jq/openssl and a fake sops that keeps each plaintext by target name."""
    bindir = _sandbox(tmp_path)
    for tool in ("jq", "openssl"):
        (bindir / tool).unlink()
        real = shutil.which(tool)
        if tool == "jq" and shutil.which("mise"):
            out = subprocess.run(
                ["mise", "which", "jq"], capture_output=True, text=True, check=False
            )
            real = out.stdout.strip() or real
        (bindir / tool).symlink_to(real)
    plain = tmp_path / "plain"
    plain.mkdir()
    (bindir / "sops").write_text(
        "#!/bin/sh\n"
        'while [ "$1" != "--filename-override" ]; do shift; done\n'
        'name="$(echo "$2" | tr / _)"\n'
        f'cat > "{plain}/$name"\n'
        "echo 'sops: stub'\n"
    )
    for rel in (ORCH, DB, WH, WH_TOKEN):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("sentinel: keep\n")
    return bindir


def _plain(tmp_path: Path, rel: str) -> dict:
    return json.loads((tmp_path / "plain" / rel.replace("/", "_")).read_text())


def test_schedule_service_token_bootstrapped(tmp_path):
    bindir = _schedule_sandbox(tmp_path)
    r = _run(tmp_path, bindir)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / SCH).read_text() == "sops: stub\n"
    assert (tmp_path / SCH_TOKEN).read_text() == "sops: stub\n"
    sch = _plain(tmp_path, SCH)
    assert set(sch) == {"orchestrator"}
    assert set(sch["orchestrator"]) == {"token"}
    token = sch["orchestrator"]["token"]
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    hashes = _plain(tmp_path, SCH_TOKEN)
    assert set(hashes) == {"serviceApi"}
    assert hashes["serviceApi"] == {
        "tokenHashes": {"agents-schedule": hashlib.sha256(token.encode()).hexdigest()}
    }


def test_schedule_service_token_idempotent(tmp_path):
    bindir = _schedule_sandbox(tmp_path)
    assert _run(tmp_path, bindir).returncode == 0
    first = {rel: _plain(tmp_path, rel) for rel in (SCH, SCH_TOKEN)}
    r = _run(tmp_path, bindir)
    assert r.returncode == 0, r.stderr
    assert {rel: _plain(tmp_path, rel) for rel in (SCH, SCH_TOKEN)} == first
    # exactly one present: exit 1 naming both, the survivor untouched, nothing written
    for present, absent in ((SCH, SCH_TOKEN), (SCH_TOKEN, SCH)):
        (tmp_path / absent).unlink()
        (tmp_path / present).write_text("sentinel: keep\n")
        r = _run(tmp_path, bindir)
        assert r.returncode == 1
        assert SCH in r.stderr and SCH_TOKEN in r.stderr
        assert not (tmp_path / absent).exists()
        assert (tmp_path / present).read_text() == "sentinel: keep\n"
        (tmp_path / absent).write_text("sentinel: keep\n")


def test_schedule_service_token_not_printed(tmp_path):
    bindir = _schedule_sandbox(tmp_path)
    r = _run(tmp_path, bindir)
    assert r.returncode == 0, r.stderr
    token = _plain(tmp_path, SCH)["orchestrator"]["token"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    for stream in (r.stdout, r.stderr):
        assert token not in stream
        assert digest not in stream

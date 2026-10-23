"""The agent-orchestrator-db CNPG release: chart, initdb, backups, no plain credentials."""

REL = "agent-orchestrator-db"


def _release(helmfile_build):
    for doc in helmfile_build():
        for rel in doc.get("releases", []):
            if rel["name"] == REL:
                return rel
    raise AssertionError(f"release {REL} not rendered")


def test_release_chart_namespace_and_force(helmfile_build):
    rel = _release(helmfile_build)
    assert rel["chart"] == "cnpg/cluster"
    assert rel["version"] == "0.9.0"
    assert rel["namespace"] == "agents"
    assert rel["force"] is False
    assert rel["timeout"] == 600
    assert rel["labels"]["purpose"] == "agents"
    assert rel["labels"]["application"] == "agent-orchestrator"


def test_initdb_names(layered_values):
    v = layered_values(REL)
    assert v["cluster"]["initdb"]["database"] == "agent_orchestrator"
    assert v["cluster"]["initdb"]["owner"] == "agent_orchestrator"
    assert v["version"]["postgresql"] == "16"
    assert v["cluster"]["instances"] == 1


def test_storage_classes(layered_values):
    c = layered_values(REL)["cluster"]
    assert c["storage"] == {"size": "5Gi", "storageClass": "rook-ceph"}
    assert c["walStorage"] == {"enabled": True, "size": "2Gi", "storageClass": "rook-ceph"}


def test_backups_enabled_with_own_path(layered_values):
    b = layered_values(REL)["backups"]
    assert b["enabled"] is True
    assert b["provider"] == "s3"
    assert b["s3"]["path"] == "/agent-orchestrator-db/"
    assert b["s3"]["bucket"] == "database-backup-szymonrychu"
    assert b["s3"]["region"] == "eu-central-1"
    assert b["retentionPolicy"] == "30d"
    (sched,) = b["scheduledBackups"]
    assert sched["schedule"] == "0 0 3 * * *"
    assert sched["backupOwnerReference"] == "self"
    assert sched["method"] == "barmanObjectStore"


def test_no_credentials_in_plain_values(layered_values):
    s3 = layered_values(REL)["backups"]["s3"]
    assert "accessKey" not in s3 and "secretKey" not in s3


def test_prepare_hook_skips_diff_when_namespace_missing(tmp_path):
    """On a cluster without namespace agents, `prepare` has nothing to diff."""
    import os
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {
        "kubectl": (
            'case "$1" in\n'
            "  cluster-info) exit 0 ;;\n"
            "  get) exit 1 ;;\n"
            "  diff) echo 'namespaces not found' >&2; exit 2 ;;\n"
            "esac\nexit 0\n"
        ),
        "sops": "echo 'kind: Secret'\n",
    }.items():
        (bindir / name).write_text("#!/bin/bash\n" + body)
        (bindir / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    res = subprocess.run(
        [str(root / ".hook.sh"), str(root), "prepare", "x", "agents", "1", "600"],
        env=env,
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    assert res.returncode == 0, res.stderr

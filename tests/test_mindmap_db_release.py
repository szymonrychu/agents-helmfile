"""The agents-mindmap-db CNPG release: pinned chart, labels, initdb names, pgvector bootstrap."""

import pytest

REL = "agents-mindmap-db"


def _release(helmfile_build):
    for doc in helmfile_build():
        for rel in doc.get("releases", []):
            if rel["name"] == REL:
                return rel
    raise AssertionError(f"release {REL} not rendered")


@pytest.mark.needs_sops_key
def test_release_declared(helmfile_build):
    rel = _release(helmfile_build)
    assert rel["chart"] == "cnpg/cluster"
    assert rel["version"] == "0.9.0"
    assert rel["namespace"] == "agents"
    assert rel["force"] is False
    assert rel["timeout"] == 600
    assert rel["labels"]["purpose"] == "agents"
    assert rel["labels"]["application"] == "agents-mindmap"


def test_initdb(layered_values):
    v = layered_values(REL)
    initdb = v["cluster"]["initdb"]
    assert initdb["database"] == "mindmap"
    assert initdb["owner"] == "mindmap"
    assert v["cluster"]["instances"] == 1
    assert v["cluster"]["storage"] == {"size": "5Gi", "storageClass": "rook-ceph"}
    assert v["backups"]["s3"]["path"] == "/agents-mindmap-db/"


def test_mindmap_db_vector_bootstrap(layered_values):
    initdb = layered_values(REL)["cluster"]["initdb"]
    assert "CREATE EXTENSION IF NOT EXISTS vector" in initdb["postInitApplicationSQL"]


def test_mindmap_db_same_image_line_as_the_other_dbs(layered_values):
    # No image pin of its own: it follows the standard-image move (Task 5) with the other dbs.
    mine = layered_values(REL)
    other = layered_values("agents-vault-db")
    assert mine["version"] == other["version"]
    assert mine["cluster"].get("imageName") == other["cluster"].get("imageName")
    assert mine["cluster"]["imagePullSecrets"] == other["cluster"]["imagePullSecrets"]


def test_mindmap_db_backup_credentials_are_a_copy(repo_root):
    mine = (repo_root / "values" / REL / "default.secrets.yaml").read_bytes()
    other = (repo_root / "values" / "agents-vault-db" / "default.secrets.yaml").read_bytes()
    assert mine == other

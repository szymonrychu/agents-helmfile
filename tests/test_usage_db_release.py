"""The agents-usage-db CNPG release: pinned chart, labels, initdb names, no plain credentials."""

REL = "agents-usage-db"


def _release(helmfile_build):
    for doc in helmfile_build():
        for rel in doc.get("releases", []):
            if rel["name"] == REL:
                return rel
    raise AssertionError(f"release {REL} not rendered")


def test_release_declared(helmfile_build):
    rel = _release(helmfile_build)
    assert rel["chart"] == "cnpg/cluster"
    assert rel["version"] == "0.9.0"
    assert rel["namespace"] == "agents"
    assert rel["force"] is False
    assert rel["timeout"] == 600
    assert rel["labels"]["purpose"] == "agents"
    assert rel["labels"]["application"] == "agents-usage"


def test_initdb(layered_values):
    v = layered_values(REL)
    assert v["cluster"]["initdb"] == {"database": "usage", "owner": "usage"}
    assert v["cluster"]["instances"] == 1
    assert v["cluster"]["storage"] == {"size": "5Gi", "storageClass": "rook-ceph"}
    assert v["backups"]["s3"]["path"] == "/agents-usage-db/"
    assert "accessKey" not in v["backups"]["s3"]
    assert "secretKey" not in v["backups"]["s3"]

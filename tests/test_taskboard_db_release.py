"""The agents-taskboard-db CNPG release: pinned chart, labels, initdb names, no plain credentials."""

REL = "agents-taskboard-db"


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
    assert rel["labels"]["application"] == "agents-taskboard"


def test_initdb(layered_values):
    v = layered_values(REL)
    assert v["cluster"]["initdb"] == {"database": "taskboard", "owner": "taskboard"}
    assert v["cluster"]["instances"] == 1
    assert v["cluster"]["storage"] == {"size": "5Gi", "storageClass": "rook-ceph"}
    assert v["backups"]["s3"]["path"] == "/agents-taskboard-db/"

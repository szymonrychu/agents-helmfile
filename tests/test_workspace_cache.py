"""The workspace cache bucket: a rook-ceph ObjectBucketClaim wired into the orchestrator release."""

import re

import yaml

REL = "agents-orchestrator"
CLAIM = "raw/workspace-cache-bucket.pre.yaml"


def _claim(repo_root) -> dict:
    return yaml.safe_load((repo_root / CLAIM).read_text())


def test_bucket_claim_shape(repo_root):
    claim = _claim(repo_root)
    assert claim["apiVersion"] == "objectbucket.io/v1alpha1"
    assert claim["kind"] == "ObjectBucketClaim"
    assert claim["metadata"]["name"] == "agents-workspace-cache"
    assert claim["metadata"]["namespace"] == "agents"
    assert claim["spec"]["bucketName"] == "agents-workspace-cache"
    assert claim["spec"]["storageClassName"] == "rook-ceph-bucket"
    text = (repo_root / CLAIM).read_text()
    assert "reclaimPolicy" in text
    assert "Delete" in text


def test_bucket_claim_applied_at_presync(repo_root):
    path = repo_root / CLAIM
    assert path.is_file()
    # .hook.sh applies raw/*.pre.yaml at presync (find -name "*.${SEARCH_SUFF}.yaml").
    assert path.name.endswith(".pre.yaml")
    assert path.parent == repo_root / "raw"
    hook = (repo_root / ".hook.sh").read_text()
    assert '"*.${SEARCH_SUFF}.yaml"' in hook
    assert 'SEARCH_SUFF="pre"' in hook


def test_orchestrator_cache_values(layered_values):
    s3 = layered_values(REL)["cache"]["s3"]
    assert s3["endpoint"] == "http://rook-ceph-rgw-ceph-objectstore.rook-ceph.svc:80"
    assert s3["region"] == "us-east-1"
    assert s3["bucket"] == "agents-workspace-cache"
    assert s3["pathStyle"] is True
    assert s3["existingSecret"] == "agents-workspace-cache"


def test_no_cache_credentials_in_values(repo_root):
    plain = [p for p in (repo_root / "values").rglob("*.yaml") if ".secrets." not in p.name] + [
        repo_root / CLAIM
    ]
    for path in plain:
        text = path.read_text()
        assert not re.search(
            r"(?im)^\s*(accessKey|secretKey|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|accessKeyId|secretAccessKey)\s*:\s*\S",
            text,
        ), path
        assert not re.search(r"AKIA[0-9A-Z]{12,}", text), path

"""The cache bucket quota set on the ObjectBucketClaim (rook additionalConfig)."""

import yaml


def test_bucket_quota_set(repo_root):
    claim = yaml.safe_load((repo_root / "raw/workspace-cache-bucket.pre.yaml").read_text())
    conf = claim["spec"]["additionalConfig"]
    assert conf["maxSize"] == "100G"
    assert int(conf["maxObjects"]) > 0

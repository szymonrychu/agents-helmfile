"""The cache bucket quota set on the ObjectBucketClaim (rook additionalConfig)."""

import re

import yaml


def test_bucket_quota_set(repo_root):
    claim = yaml.safe_load((repo_root / "raw/workspace-cache-bucket.pre.yaml").read_text())
    conf = claim["spec"]["additionalConfig"]
    assert re.fullmatch(r"[1-9][0-9]*(Gi|Ti)", str(conf["maxSize"]))
    assert int(conf["maxObjects"]) > 0

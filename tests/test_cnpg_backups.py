"""Every agents CNPG database release backs up through the Barman Cloud plugin."""

import re

import pytest

PLUGIN = "barman-cloud.cloudnative-pg.io"
DB_RELEASES = [
    "agent-orchestrator-db",  # the hibernated old release, still declared (D6)
    "agents-orchestrator-db",
    "agents-app-connector-db",
    "agents-history-db",
    "agents-matrix-connector-db",
    "agents-mindmap-db",
    "agents-schedule-db",
    "agents-taskboard-db",
    "agents-usage-db",
    "agents-vault-db",
    "agents-webhook-db",
]


@pytest.mark.parametrize("release", DB_RELEASES)
def test_plugin_is_wal_archiver(layered_values, release):
    v = layered_values(release)
    assert {"name": PLUGIN, "isWALArchiver": True} in v["cluster"]["plugins"]


@pytest.mark.parametrize("release", DB_RELEASES)
def test_backup_method_plugin(layered_values, release):
    b = layered_values(release)["backups"]
    assert b["method"] == "plugin"
    assert b["pluginConfiguration"]["name"] == PLUGIN


@pytest.mark.parametrize("release", DB_RELEASES)
def test_scheduled_backups_plugin(layered_values, release):
    for sb in layered_values(release)["backups"]["scheduledBackups"]:
        assert sb["method"] == "plugin"
        assert sb["pluginConfiguration"]["name"] == PLUGIN


@pytest.mark.parametrize("release", DB_RELEASES)
def test_no_in_tree_method_left(repo_root, release):
    for path in (repo_root / "values" / release).glob("*.yaml"):
        if path.name.endswith(".secrets.yaml"):
            continue
        assert "barmanObjectStore" not in path.read_text(), path


@pytest.mark.parametrize("release", DB_RELEASES)
def test_same_bucket_and_path(layered_values, release):
    b = layered_values(release)["backups"]
    assert b["enabled"] is True
    assert b["s3"]["bucket"] == "database-backup-szymonrychu"
    assert b["s3"]["path"] == f"/{release}/"
    assert b["retentionPolicy"] == "30d"
    if release == "agents-history-db":
        assert b["wal"]["maxParallel"] == 8


def test_every_db_release_covered(repo_root):
    text = (repo_root / "helmfile.yaml.gotmpl").read_text()
    names = re.findall(r"- name: (\S+)\n(?:(?!\n  - name:).)*?chart: cnpg/cluster", text, re.DOTALL)
    assert sorted(names) == sorted(DB_RELEASES)


@pytest.mark.parametrize("release", DB_RELEASES)
def test_pull_secret_for_plugin_sidecar(layered_values, release):
    """The injected plugin sidecar image is pulled through the Harbor proxy."""
    assert {"name": "regcred"} in layered_values(release)["cluster"]["imagePullSecrets"]


STANDARD_IMAGE = re.compile(
    r"^ghcr\.io/cloudnative-pg/postgresql:16\.15-standard-bookworm@sha256:[0-9a-f]{64}$"
)


@pytest.mark.parametrize("release", DB_RELEASES)
def test_standard_image_pinned(layered_values, release):
    v = layered_values(release)
    assert STANDARD_IMAGE.match(v["cluster"]["imageName"])
    assert v["version"]["postgresql"] == "16"

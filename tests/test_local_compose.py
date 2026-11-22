"""The docker-compose k3s stack: compose file, bootstrap script and env example (text checks only)."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "local" / "compose.yaml").read_text())
BOOTSTRAP = (ROOT / "local" / "bootstrap.sh").read_text()
ENV_KEYS = [
    "HARBOR_USERNAME",
    "HARBOR_PASSWORD",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AGENTS_LOCAL_PROFILES",
    "AGENTS_LOCAL_DIND",
    "AGENTS_LOCAL_HIVES",
    "AGENTS_LOCAL_HTTP_PORT",
    "AGENTS_LOCAL_FAKE_CLAUDE",
]


def test_published_ports_bind_loopback():
    ports = [p for s in COMPOSE["services"].values() for p in s.get("ports", [])]
    assert ports
    assert all(str(p).startswith("127.0.0.1:") for p in ports)


@pytest.mark.parametrize("service", ["k3s", "bootstrap"])
def test_compose_image_pinned_by_tag_and_digest(service):
    image = COMPOSE["services"][service]["image"]
    assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", image), image


def test_only_bootstrap_reads_env_file():
    readers = {n for n, s in COMPOSE["services"].items() if "env_file" in s}
    assert readers == {"bootstrap"}


def test_repo_mounted_read_only():
    volumes = COMPOSE["services"]["bootstrap"]["volumes"]
    assert "..:/repo:ro" in volumes


def test_bootstrap_keeps_secrets_off_logs_and_args():
    assert not re.search(r"set -\w*x|bash -x|--from-literal", BOOTSTRAP)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in BOOTSTRAP
    uses = [ln for ln in BOOTSTRAP.splitlines() if "HARBOR_PASSWORD" in ln]
    assert len(uses) == 1
    assert uses[0].lstrip().startswith("printf '%s' \"$HARBOR_PASSWORD\" |")
    assert "--password-stdin" in BOOTSTRAP


def test_env_example_lists_every_key_empty():
    lines = (ROOT / "local" / "env.example").read_text().splitlines()
    assignments = [ln for ln in lines if ln and not ln.startswith("#")]
    assert assignments == [f"{k}=" for k in ENV_KEYS]


def test_versions_conf_semver():
    text = (ROOT / "local" / "versions.conf").read_text()
    assert re.search(r"^CNPG_OPERATOR_CHART_VERSION=\d+\.\d+\.\d+$", text, re.M)

"""The orchestrator's unit is a session and the project is agents-orchestrator (design D1, D2, D12).

Every tracked file of the repo is guarded. A line may not match /cluster/i once the Kubernetes and
CNPG phrases of ALLOWED_PHRASES are removed from it, and it may not match agent[-_]orchestrator once
the phrases OLD_NAME_PHRASES allows for its file are removed. Dated-history files keep the words
of their time; the old database release, kept hibernated (design D6), keeps its old names.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

UNIT = re.compile("cluster", re.IGNORECASE)
OLD_NAME = re.compile("agent[-_]orchestrator")

# Kubernetes and CNPG clusters (D12); never the orchestrator's unit.
ALLOWED_PHRASES = (
    # Kubernetes: in-cluster DNS, cert-manager issuer, kubectl probe, node IPs, callers inside it.
    "cluster.local",
    "cluster-issuer",
    "cluster-info",
    "cluster node",
    "Cluster node",
    "in-cluster",
    # Kubernetes: the apply hook and the tests probe whether a Kubernetes API is reachable.
    "CLUSTER_REACHABLE",
    "reachable cluster",
    "fresh cluster",
    "may have no cluster",
    "a cluster without namespace",
    "the real cluster",
    # Kubernetes: a kube context name in the kube-context guard test.
    "prod-cluster",
    # Kubernetes: ORCH_CLUSTER_DOMAIN is the Kubernetes DNS domain, and the release test pins that
    # no other CLUSTER-named setting is left.
    "CLUSTER_DOMAIN",
    '"CLUSTER" in k',
    # CNPG: the chart, its Cluster resource, its generated Secrets and its values key.
    "cnpg/cluster",
    "cluster.initdb",
    "db-cluster",
    "CNPG Cluster",
    "CNPG cluster",
    "cnpg_cluster",
    "Cluster CR",
    "Cluster metadata.name",
    '== "Cluster"',
    '["cluster"]',
    "clusters = [",
    "len(clusters)",
    "clusters[0]",
    # CNPG: the new database imports from the old CNPG cluster (design D5).
    "from the old cluster",
    "import_from_old_cluster",
)

# Retirement checks of the smoke tests: the old routes and the old unit word are gone from its output.
RETIRED_PHRASES = (
    'p.startswith("/api/v1/clusters")',
    'assert "cluster" not in',
)

# The CNPG chart's top-level `cluster:` values key.
CNPG_VALUES_KEY = re.compile(r"^cluster:\s*$")

# Old project names allowed per file, each for the reason in its comment.
OLD_NAME_PHRASES: dict[str, tuple[str, ...]] = {
    # The old database release, kept hibernated (D6), the new one importing from it (D5), and
    # the retired app release declared with installed: false so apply uninstalls it (D7).
    "helmfile.yaml.gotmpl": (
        "agent-orchestrator-db",
        "application: agent-orchestrator",
        "name: agent-orchestrator",
        "charts/agent-orchestrator",
    ),
    # The import source of the new database: the old CNPG cluster, its database and Secrets (D5).
    "values/agents-orchestrator-db/common.yaml": (
        "agent-orchestrator-db-cluster-rw",
        "agent-orchestrator-db-cluster-app",
        "agent-orchestrator-db-cluster-ca",
        "agent_orchestrator",
    ),
    # The old database release is still declared while it is hibernated (D6).
    "tests/test_helmfile_base.py": ('"agent-orchestrator-db"',),
    # The semver guard lists every installed release, the hibernated old database among them (D6).
    "tests/test_semver_pins.py": ('"agent-orchestrator-db"',),
    # The retirement test of the old app release (installed: false, D7).
    "tests/test_orchestrator_release.py": (
        "the retired agent-orchestrator",
        'OLD_REL = "agent-orchestrator"',
    ),
    # The cutover in the docs names the retired release and the hibernated database (D6, D7).
    "README.md": (
        "`mise run apply -- --selector name=agent-orchestrator`",
        "`agent-orchestrator-db`",
        "`values/agent-orchestrator-db/`",
        "`agent-orchestrator`",
    ),
}

# Whole files of the old database release, kept hibernated (D6): its values dir and its tests.
OLD_NAME_FILES = frozenset({"tests/test_db_release.py"})
OLD_NAME_DIRS = ("values/agent-orchestrator-db/",)

# Dated history keeps the words of its time (D12).
DATED_HISTORY = frozenset({"MEMORY.md", "ROADMAP.md", "CHANGELOG.md"})
DATED_HISTORY_DIRS = ("docs/plans/",)
# This guard spells out the phrases it allows.
SELF = "tests/test_nomenclature.py"


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def _guarded() -> list[str]:
    return [
        p
        for p in _tracked()
        if p != SELF and p not in DATED_HISTORY and not p.startswith(DATED_HISTORY_DIRS)
    ]


def _old_name_exempt(path: str) -> bool:
    return path in OLD_NAME_FILES or path.startswith(OLD_NAME_DIRS)


def _names_unit(line: str) -> bool:
    if CNPG_VALUES_KEY.match(line):
        return False
    for phrase in (*ALLOWED_PHRASES, *RETIRED_PHRASES):
        line = line.replace(phrase, "")
    return bool(UNIT.search(line))


def _names_old(path: str, line: str) -> bool:
    if _old_name_exempt(path):
        return False
    for phrase in OLD_NAME_PHRASES.get(path, ()):
        line = line.replace(phrase, "")
    return bool(OLD_NAME.search(line))


def _offending(path: str) -> list[str]:
    try:
        text = (ROOT / path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return []
    return [
        f"{path}:{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), start=1)
        if _names_unit(line) or _names_old(path, line)
    ]


def test_every_tracked_file_names_the_unit_session_and_the_new_project() -> None:
    files = _guarded()
    for path in ("README.md", "AGENTS.md", "mise.toml", "scripts/smoke.sh", "helmfile.yaml.gotmpl"):
        assert path in files, path
    hits = [hit for path in files for hit in _offending(path)]
    assert not hits, "\n".join(hits[:50]) + f"\n({len(hits)} lines)"


def test_no_tracked_path_names_the_unit_or_the_old_project() -> None:
    paths = [
        p for p in _guarded() if UNIT.search(p) or (OLD_NAME.search(p) and not _old_name_exempt(p))
    ]
    assert paths == []


def test_allowlist_names_only_kubernetes_and_cnpg_clusters() -> None:
    for phrase in ALLOWED_PHRASES:
        assert UNIT.search(phrase), phrase
    assert not any(p.lower() in ("cluster", "clusters", "cluster_id") for p in ALLOWED_PHRASES)
    for path, phrases in OLD_NAME_PHRASES.items():
        assert (ROOT / path).is_file(), path
        for phrase in phrases:
            assert OLD_NAME.search(phrase), (path, phrase)


def test_guard_catches_the_unit_and_the_old_project() -> None:
    assert _names_unit("a 1-replica cluster gets a Ready pod")
    assert _names_unit("POST /api/v1/clusters")
    assert not _names_unit("http://keycloak.authentication.svc.cluster.local/realms")
    assert not _names_unit("cluster:")
    assert _names_old("scripts/smoke.sh", "deploy/agent-orchestrator-api")
    assert _names_old("README.md", "agent-orchestrator-services:8002")
    assert not _names_old("README.md", "agents-orchestrator-services:8002")

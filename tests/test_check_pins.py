"""scripts/check_pins.py offline: a fake `gh` on PATH serving check runs built from a captured one.

tests/fixtures/check_runs_agents_orchestrator.json is the real response of
`gh api repos/szymonrychu/agents-orchestrator/commits/96e46f3/check-runs` (main CI run 40565058706,
e2e failed). Every check run the fake serves is a copy of one of its runs with the name, status,
conclusion and head_sha replaced, so the fake follows the real output shape. The only edit to the
captured bytes: the GitHub Actions app `client_id` (a public id the gitleaks pre-commit hook flags
as a generic API key) reads "redacted".
"""

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_pins.py"
FIXTURE = ROOT / "tests" / "fixtures" / "check_runs_agents_orchestrator.json"
REAL = json.loads(FIXTURE.read_text())
REAL_SHA = "96e46f361d17d2a905929329b5a0a77751881c4d"
CHARTS = (
    "agents-orchestrator",
    "agents-web",
    "agents-webhook",
    "agents-schedule",
    "agents-matrix-connector",
)
GREEN = (
    ("lint", "completed", "success"),
    ("test", "completed", "success"),
    ("e2e", "completed", "success"),
    ("notify", "completed", "neutral"),
    ("chart-publish-tag", "completed", "skipped"),
)

FAKE_GH = r"""
import hashlib, json, os, re, sys

args = sys.argv[1:]
state = json.loads(open(os.environ["FAKE_GH_STATE"]).read())
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:1] != ["api"]:
    sys.exit("fake gh: only `gh api` is served")
path = next(a for a in args[1:] if a.startswith("repos/")).split("?")[0]
m = re.fullmatch(r"repos/([^/]+/[^/]+)/commits/([^/]+)/check-runs", path)
if m:
    assert "--paginate" in args and "--slurp" in args, args
    pages = state.get("runs", {}).get(m[2], state.get("default_runs"))
    if pages is None:
        pages = [{"total_count": 0, "check_runs": []}]
    print(json.dumps(pages))
    sys.exit(0)
m = re.fullmatch(r"repos/([^/]+/[^/]+)/commits/([^/]+)", path)
if m:
    assert args[-2:] == ["--jq", ".sha"], args
    key = f"{m[1]}@{m[2]}"
    sha = state.get("commits", {}).get(key)
    if sha is None and state.get("resolve_any"):
        sha = hashlib.sha1(key.encode()).hexdigest()
    if sha is None:
        print(json.dumps({"message": f"No commit found for SHA: {m[2]}", "status": "422"}))
        print(f"gh: No commit found for SHA: {m[2]} (HTTP 422)", file=sys.stderr)
        sys.exit(1)
    print(sha)
    sys.exit(0)
sys.exit(f"fake gh: unexpected path {path}")
"""


def run_of(name: str, status: str, conclusion: str | None, sha: str) -> dict:
    """One check run: a copy of a real one with its identity and outcome replaced."""
    run = copy.deepcopy(REAL["check_runs"][0])
    run.update(name=name, status=status, conclusion=conclusion, head_sha=sha)
    return run


def pages_of(sha: str, *runs: tuple[str, str, str | None], per_page: int = 100) -> list[dict]:
    """The `gh api --paginate --slurp` output: a JSON array of check-runs pages."""
    built = [run_of(n, s, c, sha) for n, s, c in runs]
    chunks = [built[i : i + per_page] for i in range(0, len(built), per_page)] or [[]]
    return [{"total_count": len(built), "check_runs": chunk} for chunk in chunks]


def fake_sha(repo: str, ref: str) -> str:
    return hashlib.sha1(f"szymonrychu/{repo}@{ref}".encode()).hexdigest()


@pytest.fixture
def tree(tmp_path):
    """A copy of the repo's pin sources, so a test may rewrite one pin."""
    root = tmp_path / "tree"
    (root / "values").mkdir(parents=True)
    shutil.copy(ROOT / "helmfile.yaml.gotmpl", root)
    for release in ("agents-orchestrator", "agents-web"):
        (root / "values" / release).mkdir()
        shutil.copy(ROOT / "values" / release / "common.yaml", root / "values" / release)
    return root


def set_pin(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    assert count == 1, pattern
    path.write_text(new)


def pin_chart(tree: Path, release: str, version: str) -> None:
    set_pin(
        tree / "helmfile.yaml.gotmpl",
        rf"(charts/{release}\n\s+version: )\S+",
        rf"\g<1>{version}",
    )


def check(tree: Path, state: dict, *args: str, env: dict | None = None):
    bindir = tree.parent / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE_GH}")
    gh.chmod(0o755)
    (tree.parent / "state.json").write_text(json.dumps(state))
    log = tree.parent / "gh.log"
    log.write_text("")
    full_env = {
        **{k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")},
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_GH_STATE": str(tree.parent / "state.json"),
        "FAKE_GH_LOG": str(log),
        **(env or {}),
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=tree,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return proc, calls


def green_state() -> dict:
    return {"resolve_any": True, "default_runs": pages_of("x", *GREEN)}


def pin_lines(out: str) -> dict[str, str]:
    lines = [line for line in out.splitlines() if line.startswith("pin ")]
    return {line.split()[1]: line for line in lines}


def expected_pins(tree: Path) -> dict[str, tuple[str, str]]:
    """Every pin source of the tree: label -> (repo, version), read straight from the files."""
    helmfile = yaml.safe_load((tree / "helmfile.yaml.gotmpl").read_text())
    versions = {r["name"]: r.get("version") for r in helmfile["releases"]}
    orch = yaml.safe_load((tree / "values/agents-orchestrator/common.yaml").read_text())
    web = yaml.safe_load((tree / "values/agents-web/common.yaml").read_text())
    pins = {name: (name, str(versions[name])) for name in CHARTS}
    pins["agents-orchestrator:image.tag"] = ("agents-orchestrator", str(orch["image"]["tag"]))
    pins["agents-web:image.tag"] = ("agents-web", str(web["image"]["tag"]))
    agent = orch["agentImage"].rsplit("/", 1)[1].split(":")
    pins["agents-orchestrator:agentImage"] = (agent[0], agent[1])
    for image in orch["env"]["ORCH_AGENT_IMAGES"].split(","):
        repo, tag = image.strip().rsplit("/", 1)[1].split(":")
        pins[f"agents-orchestrator:env.ORCH_AGENT_IMAGES[{image.strip()}]"] = (repo, tag)
    return pins


def ref_of(version: str) -> str:
    sha = re.fullmatch(r"\d+\.\d+\.\d+-main\.([0-9a-f]{7,40})", version)
    if sha:
        return sha[1]
    if re.fullmatch(r"\d+\.\d+\.\d+", version):
        return f"v{version}"
    return version


def test_fixture_is_real_check_runs_shape():
    assert REAL["total_count"] == len(REAL["check_runs"]) > 0
    keys = {
        "id",
        "name",
        "status",
        "conclusion",
        "head_sha",
        "app",
        "check_suite",
        "details_url",
        "html_url",
        "started_at",
        "completed_at",
        "output",
        "pull_requests",
    }
    for run in REAL["check_runs"]:
        assert keys <= set(run), sorted(keys - set(run))
        assert run["head_sha"] == REAL_SHA
        assert run["app"]["slug"] == "github-actions"
        assert run["app"]["client_id"] == "redacted"
    outcomes = {r["name"]: (r["status"], r["conclusion"]) for r in REAL["check_runs"]}
    assert outcomes["e2e"] == ("completed", "failure")
    # The fake serves copies of the real run: same keys, only identity and outcome changed.
    served = pages_of("abc", ("lint", "in_progress", None))
    assert served[0]["total_count"] == 1
    assert set(served[0]["check_runs"][0]) == set(REAL["check_runs"][0])
    assert served[0]["check_runs"][0]["app"] == REAL["check_runs"][0]["app"]


def test_every_pin_resolved(tree):
    expected = expected_pins(tree)
    assert len(expected) >= 9
    proc, calls = check(tree, green_state())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = pin_lines(proc.stdout)
    assert set(lines) == set(expected), proc.stdout
    paths = {next(a for a in call if a.startswith("repos/")).split("?")[0] for call in calls}
    for label, (repo, version) in expected.items():
        ref = ref_of(version)
        sha = fake_sha(repo, ref)
        assert lines[label] == f"pin {label} {version} {sha}: ok", lines[label]
        assert f"repos/szymonrychu/{repo}/commits/{ref}" in paths, (label, sorted(paths))
        assert f"repos/szymonrychu/{repo}/commits/{sha}/check-runs" in paths, label
    # The wrapper image maps to the claude-code-wrapper repo.
    assert expected["agents-orchestrator:agentImage"][0] == "claude-code-wrapper"


def test_green_pins_pass(tree):
    pin_chart(tree, "agents-orchestrator", "0.1.0-main.96e46f3")
    real_green = copy.deepcopy(REAL)
    for run in real_green["check_runs"]:
        if run["conclusion"] == "failure":
            run["conclusion"] = "success"
    state = green_state()
    state["commits"] = {"szymonrychu/agents-orchestrator@96e46f3": REAL_SHA}
    state["runs"] = {REAL_SHA: [real_green]}
    proc, _ = check(tree, state)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = pin_lines(proc.stdout)
    assert (
        lines["agents-orchestrator"] == f"pin agents-orchestrator 0.1.0-main.96e46f3 {REAL_SHA}: ok"
    )
    assert all(line.endswith(": ok") for line in lines.values()), proc.stdout
    assert "refused" not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("runs", "named"),
    [
        ([("e2e", "completed", "failure")], ["e2e completed/failure"]),
        ([("e2e", "completed", "cancelled")], ["e2e completed/cancelled"]),
        ([("e2e", "completed", "timed_out")], ["e2e completed/timed_out"]),
        ([("e2e", "completed", "action_required")], ["e2e completed/action_required"]),
        ([("e2e", "completed", "stale")], ["e2e completed/stale"]),
        ([("e2e", "in_progress", None)], ["e2e in_progress"]),
        ([("e2e", "queued", None)], ["e2e queued"]),
        ([], ["no check runs"]),
    ],
)
def test_red_pin_refused(tree, runs, named):
    pin_chart(tree, "agents-orchestrator", "0.1.0-main.96e46f3")
    sha = "96e46f3" + "0" * 33
    state = green_state()
    state["commits"] = {"szymonrychu/agents-orchestrator@96e46f3": sha}
    green_rest = [r for r in GREEN if r[0] != "e2e"]
    state["runs"] = {sha: pages_of(sha, *([*green_rest, *runs] if runs else []))}
    proc, _ = check(tree, state)
    assert proc.returncode != 0
    line = pin_lines(proc.stdout)["agents-orchestrator"]
    assert line.startswith(f"pin agents-orchestrator 0.1.0-main.96e46f3 {sha}: refused ("), line
    assert "szymonrychu/agents-orchestrator" in line
    for name in named:
        assert name in line, line
    for green_name in ("lint", "test", "notify"):
        assert f"{green_name} completed" not in line, line
    # The refusal summary names the pin again, on stderr.
    assert "agents-orchestrator" in proc.stderr and sha in proc.stderr, proc.stderr
    assert "0.1.0-main.96e46f3" in proc.stderr
    # Only pins of that commit are refused (the image tag may pin the same one).
    others = [v for v in pin_lines(proc.stdout).values() if sha not in v]
    assert others and all(v.endswith(": ok") for v in others), proc.stdout


def test_real_captured_runs_refused(tree):
    pin_chart(tree, "agents-orchestrator", "0.1.0-main.96e46f3")
    state = green_state()
    state["commits"] = {"szymonrychu/agents-orchestrator@96e46f3": REAL_SHA}
    state["runs"] = {REAL_SHA: [REAL]}
    proc, _ = check(tree, state)
    assert proc.returncode != 0
    line = pin_lines(proc.stdout)["agents-orchestrator"]
    assert line == (
        f"pin agents-orchestrator 0.1.0-main.96e46f3 {REAL_SHA}: refused "
        "(szymonrychu/agents-orchestrator: e2e completed/failure)"
    ), line


def test_red_check_on_a_later_page_refused(tree):
    pin_chart(tree, "agents-web", "0.1.0-main.01e971c")
    sha = "01e971c" + "1" * 33
    state = green_state()
    state["commits"] = {"szymonrychu/agents-web@01e971c": sha}
    runs = [(f"job-{i}", "completed", "success") for i in range(5)] + [
        ("web", "completed", "failure")
    ]
    state["runs"] = {sha: pages_of(sha, *runs, per_page=2)}
    proc, calls = check(tree, state)
    assert proc.returncode != 0
    assert "web completed/failure" in pin_lines(proc.stdout)["agents-web"]
    runs_calls = [
        c for c in calls if any(a.endswith("/check-runs") or "/check-runs?" in a for a in c)
    ]
    assert runs_calls and all("--paginate" in c and "--slurp" in c for c in runs_calls)


def test_release_version_resolves_through_tag(tree):
    pin_chart(tree, "agents-schedule", "0.1.0")
    set_pin(
        tree / "values/agents-orchestrator/common.yaml",
        r"^(agentImage: \S+/claude-code-wrapper:)\S+",
        r"\g<1>0.3.2",
    )
    tag_sha = "fb005fd" + "2" * 33
    state = green_state()
    state["commits"] = {"szymonrychu/claude-code-wrapper@v0.3.2": tag_sha}
    proc, calls = check(tree, state)
    lines = pin_lines(proc.stdout)
    assert lines["agents-orchestrator:agentImage"] == (
        f"pin agents-orchestrator:agentImage 0.3.2 {tag_sha}: ok"
    )
    sched_sha = fake_sha("agents-schedule", "v0.1.0")
    assert lines["agents-schedule"] == f"pin agents-schedule 0.1.0 {sched_sha}: ok"
    paths = [next(a for a in c if a.startswith("repos/")).split("?")[0] for c in calls]
    assert "repos/szymonrychu/claude-code-wrapper/commits/v0.3.2" in paths
    assert "repos/szymonrychu/agents-schedule/commits/v0.1.0" in paths
    assert f"repos/szymonrychu/claude-code-wrapper/commits/{tag_sha}/check-runs" in paths
    # A tag that does not exist refuses the pin.
    del state["resolve_any"]
    state["commits"] = {}
    proc, _ = check(tree, state)
    assert proc.returncode != 0
    line = pin_lines(proc.stdout)["agents-schedule"]
    assert "refused" in line and "szymonrychu/agents-schedule" in line and "v0.1.0" in line, line


@pytest.mark.parametrize(
    ("label", "path", "pattern", "replacement"),
    [
        (
            "agents-webhook",
            "helmfile.yaml.gotmpl",
            r"(charts/agents-webhook\n)\s+version: \S+\n",
            r"\1",
        ),
        (
            "agents-web",
            "helmfile.yaml.gotmpl",
            r"(charts/agents-web\n\s+version: )\S+",
            r"\g<1>latest",
        ),
        (
            "agents-orchestrator:agentImage",
            "values/agents-orchestrator/common.yaml",
            r"^agentImage: \S+\n",
            "",
        ),
        (
            "agents-web:image.tag",
            "values/agents-web/common.yaml",
            r'^(  tag: )"\S+"',
            r"\g<1>1343836",
        ),
    ],
)
def test_missing_or_unparsable_pin_refused(tree, label, path, pattern, replacement):
    set_pin(tree / path, pattern, replacement)
    proc, _ = check(tree, green_state())
    assert proc.returncode != 0, proc.stdout
    line = pin_lines(proc.stdout)[label]
    assert ": refused (" in line, line
    assert label in proc.stderr


def test_missing_release_refused(tree):
    set_pin(
        tree / "helmfile.yaml.gotmpl",
        r"(?s)  - name: agents-matrix-connector\n.*?(?=\n  - name: )",
        "",
    )
    set_pin(tree / "helmfile.yaml.gotmpl", r"^      - agents/agents-matrix-connector\n", "")
    proc, _ = check(tree, green_state())
    assert proc.returncode != 0
    assert "refused" in pin_lines(proc.stdout)["agents-matrix-connector"]


def test_gh_failure_refused(tree):
    state = green_state()
    del state["resolve_any"]
    proc, _ = check(tree, state)
    assert proc.returncode != 0
    lines = pin_lines(proc.stdout)
    assert lines and all(": refused (" in line for line in lines.values()), proc.stdout
    assert "No commit found" in lines["agents-orchestrator"]


def test_no_override(tree):
    pin_chart(tree, "agents-orchestrator", "0.1.0-main.96e46f3")
    state = green_state()
    state["commits"] = {"szymonrychu/agents-orchestrator@96e46f3": REAL_SHA}
    state["runs"] = {REAL_SHA: [REAL]}
    skip_env = {
        name: "1"
        for name in (
            "SKIP_CHECK_PINS",
            "CHECK_PINS_SKIP",
            "CHECK_PINS_OVERRIDE",
            "CHECK_PINS_FORCE",
            "SKIP_PIN_CHECK",
            "FORCE",
            "SKIP",
            "CI",
        )
    }
    proc, _ = check(tree, state, env=skip_env)
    assert proc.returncode != 0
    assert "refused" in pin_lines(proc.stdout)["agents-orchestrator"]
    for flag in ("--force", "--skip", "--no-verify", "--allow-red", "--override", "-f"):
        proc, _ = check(tree, state, flag)
        assert proc.returncode != 0, flag
    # The script reads no environment switch at all, and the task runs it bare.
    source = SCRIPT.read_text()
    assert "os.environ" not in source and "getenv" not in source
    import tomllib

    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["check-pins"]
    assert task["run"] == "uv run scripts/check_pins.py"

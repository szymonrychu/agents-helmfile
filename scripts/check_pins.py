"""Refuse any pinned build whose commit's GitHub check runs are not all green (design D9).

Run from the repo root as `uv run scripts/check_pins.py` (mise task `check-pins`, a dependency
of `apply`). It takes no arguments and reads no environment switch: nothing skips it.

Pin sources: the chart `version:` of the main-built releases in helmfile.yaml.gotmpl, and the
image tags in values/agents-orchestrator/common.yaml (`image.tag`, `agentImage`,
`env.ORCH_AGENT_IMAGES`), values/agents-web/common.yaml (`image.tag`) and
values/agents-usage/common.yaml (`image.tag`). A chart maps to the
repo named by its chart path, an image to the repo named by its image name, both under
szymonrychu. `0.1.0-main.<sha7>` and an image sha7 resolve to that commit, a release version
`X.Y.Z` through tag `vX.Y.Z`. A pin passes only when its commit has at least one check run and
every one is completed with conclusion success, neutral or skipped. A missing or unparsable
pin is a refusal.

A re-run of a published build can fail for a harmless reason (the chart-publish guard refuses to
re-push an already published chart version), and the check run of the latest attempt then reads
red although the build was published. So a check run that is not green still counts as green when
the job of the same name succeeded in an earlier attempt of the same workflow run (the run id is
read from the check run's Actions URL, the attempts from the run's per-attempt jobs). A job that
never succeeded in any attempt, or a check run that is not an Actions job, refuses the pin.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

OWNER = "szymonrychu"
HELMFILE = Path("helmfile.yaml.gotmpl")
ORCH_VALUES = Path("values/agents-orchestrator/common.yaml")
WEB_VALUES = Path("values/agents-web/common.yaml")
USAGE_VALUES = Path("values/agents-usage/common.yaml")
CHART_RELEASES = (
    "agents-orchestrator",
    "agents-web",
    "agents-webhook",
    "agents-schedule",
    "agents-matrix-connector",
    "agents-usage",
)
GREEN = frozenset({"success", "neutral", "skipped"})
MAIN_BUILD = re.compile(r"\d+\.\d+\.\d+-main\.([0-9a-f]{7,40})")
RELEASE = re.compile(r"\d+\.\d+\.\d+")
SHA = re.compile(r"[0-9a-f]{7,40}")
ACTIONS_JOB = re.compile(r"https://github\.com/[^/]+/[^/]+/actions/runs/(\d+)/job/\d+")


class Refused(Exception):
    """A pin that cannot pass; the message is the reason."""


@dataclass
class Pin:
    label: str
    repo: str | None
    version: str | None
    problem: str | None = None


def _load(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise Refused(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise Refused(f"{path} is not a YAML mapping")
    return data


def _dig(data: dict, dotted: str) -> object:
    for key in dotted.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _chart_pins() -> list[Pin]:
    try:
        releases = {
            r.get("name"): r for r in _load(HELMFILE).get("releases") or [] if isinstance(r, dict)
        }
    except Refused as exc:
        return [Pin(name, name, None, str(exc)) for name in CHART_RELEASES]
    pins = []
    for name in CHART_RELEASES:
        release = releases.get(name)
        if release is None:
            pins.append(Pin(name, name, None, f"release {name} missing from {HELMFILE}"))
            continue
        chart = release.get("chart")
        repo = chart.rstrip("/").rsplit("/", 1)[-1] if isinstance(chart, str) else None
        version = release.get("version")
        if not repo:
            pins.append(Pin(name, None, None, f"release {name} has no chart"))
        elif not isinstance(version, str):
            pins.append(Pin(name, repo, None, f"release {name} has no string chart version"))
        else:
            pins.append(Pin(name, repo, version))
    return pins


def _image_pin(label: str, image: object) -> Pin:
    if not isinstance(image, str) or ":" not in image.rsplit("/", 1)[-1]:
        return Pin(label, None, None, f"image reference {image!r} is missing or has no tag")
    repo, tag = image.strip().rsplit("/", 1)[-1].split(":", 1)
    return Pin(label, repo, tag)


def _tag_pin(label: str, repo: str, tag: object) -> Pin:
    if not isinstance(tag, str):
        return Pin(label, repo, None, f"image tag {tag!r} is missing or not a string")
    return Pin(label, repo, tag)


def _values_pins() -> list[Pin]:
    pins = []
    try:
        orch = _load(ORCH_VALUES)
    except Refused as exc:
        orch = None
        for key in ("image.tag", "agentImage", "env.ORCH_AGENT_IMAGES"):
            pins.append(Pin(f"agents-orchestrator:{key}", None, None, str(exc)))
    if orch is not None:
        pins.append(
            _tag_pin(
                "agents-orchestrator:image.tag", "agents-orchestrator", _dig(orch, "image.tag")
            )
        )
        pins.append(_image_pin("agents-orchestrator:agentImage", orch.get("agentImage")))
        images = _dig(orch, "env.ORCH_AGENT_IMAGES")
        listed = (
            [i.strip() for i in images.split(",") if i.strip()] if isinstance(images, str) else []
        )
        if not listed:
            pins.append(
                Pin("agents-orchestrator:env.ORCH_AGENT_IMAGES", None, None, "no image listed")
            )
        for image in listed:
            pins.append(_image_pin(f"agents-orchestrator:env.ORCH_AGENT_IMAGES[{image}]", image))
    try:
        web = _load(WEB_VALUES)
        pins.append(_tag_pin("agents-web:image.tag", "agents-web", _dig(web, "image.tag")))
    except Refused as exc:
        pins.append(Pin("agents-web:image.tag", None, None, str(exc)))
    try:
        usage = _load(USAGE_VALUES)
        pins.append(_tag_pin("agents-usage:image.tag", "agents-usage", _dig(usage, "image.tag")))
    except Refused as exc:
        pins.append(Pin("agents-usage:image.tag", None, None, str(exc)))
    return pins


def _ref(version: str) -> str:
    main = MAIN_BUILD.fullmatch(version)
    if main:
        return main[1]
    if RELEASE.fullmatch(version):
        return f"v{version}"
    if SHA.fullmatch(version):
        return version
    raise Refused(f"version {version} is neither X.Y.Z-main.<sha>, X.Y.Z nor a sha")


def _gh(*args: str) -> str:
    try:
        proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise Refused(f"gh api failed: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:] or ["no output"]
        raise Refused(f"gh api {args[0]} failed: {tail[0]}")
    return proc.stdout


def _commit(repo: str, ref: str) -> str:
    sha = _gh(f"repos/{OWNER}/{repo}/commits/{ref}", "--jq", ".sha").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Refused(f"ref {ref} did not resolve to a commit: {sha!r}")
    return sha


def _succeeded_jobs(repo: str, run_id: str, cache: dict) -> set[str]:
    """Names of the jobs that concluded success in an attempt before the latest of one run."""
    key = (repo, run_id)
    if key not in cache:
        base = f"repos/{OWNER}/{repo}/actions/runs/{run_id}"
        latest = _gh(base, "--jq", ".run_attempt").strip()
        if not latest.isdigit():
            raise Refused(f"run {run_id} has no attempt number: {latest!r}")
        names: set[str] = set()
        for attempt in range(1, int(latest)):
            out = _gh("--paginate", "--slurp", f"{base}/attempts/{attempt}/jobs?per_page=100")
            try:
                jobs = [job for page in json.loads(out) for job in page["jobs"]]
            except (ValueError, TypeError, KeyError) as exc:
                raise Refused(f"unparsable jobs response for run {run_id}: {exc}") from exc
            names |= {
                job.get("name")
                for job in jobs
                if job.get("status") == "completed" and job.get("conclusion") == "success"
            }
        cache[key] = names
    return cache[key]


def _green_in_earlier_attempt(repo: str, run: dict, cache: dict) -> bool:
    match = ACTIONS_JOB.fullmatch(run.get("html_url") or "")
    return bool(match) and run.get("name") in _succeeded_jobs(repo, match[1], cache)


def _not_green(repo: str, sha: str, attempts: dict) -> list[str]:
    out = _gh(
        "--paginate", "--slurp", f"repos/{OWNER}/{repo}/commits/{sha}/check-runs?per_page=100"
    )
    try:
        pages = json.loads(out)
        runs = [run for page in pages for run in page["check_runs"]]
    except (ValueError, TypeError, KeyError) as exc:
        raise Refused(f"unparsable check-runs response: {exc}") from exc
    if not runs:
        return ["no check runs"]
    bad = []
    for run in runs:
        status, conclusion = run.get("status"), run.get("conclusion")
        if status == "completed" and conclusion in GREEN:
            continue
        if _green_in_earlier_attempt(repo, run, attempts):
            continue
        bad.append(
            f"{run.get('name')} {status}" + (f"/{conclusion}" if status == "completed" else "")
        )
    return bad


def _verdict(pin: Pin, cache: dict, attempts: dict) -> tuple[str, str | None]:
    """Return (commit, None) for a green pin, else (commit or '-', reason)."""
    if pin.problem:
        return "-", pin.problem
    assert pin.repo and pin.version
    repo = f"{OWNER}/{pin.repo}"
    key = (pin.repo, pin.version)
    if key not in cache:
        sha = "-"
        try:
            ref = _ref(pin.version)
            sha = _commit(pin.repo, ref)
            bad = _not_green(pin.repo, sha, attempts)
            cache[key] = (sha, f"{repo}: {', '.join(bad)}" if bad else None)
        except Refused as exc:
            cache[key] = (sha, f"{repo}: {exc}")
    return cache[key]


def main(argv: list[str]) -> int:
    if argv:
        print(
            f"check-pins takes no arguments (got {' '.join(argv)}); nothing skips it",
            file=sys.stderr,
        )
        return 2
    cache: dict = {}
    attempts: dict = {}
    refused = []
    for pin in [*_chart_pins(), *_values_pins()]:
        commit, reason = _verdict(pin, cache, attempts)
        version = pin.version or "-"
        if reason is None:
            print(f"pin {pin.label} {version} {commit}: ok")
        else:
            print(f"pin {pin.label} {version} {commit}: refused ({reason})")
            refused.append(f"{pin.label} {version} {commit} ({reason})")
    if refused:
        print(f"check-pins: refused {len(refused)} pin(s):", file=sys.stderr)
        for line in refused:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

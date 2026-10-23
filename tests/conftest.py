"""Shared fixtures: real helmfile render and layered plain values."""

import copy
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge `over` into a copy of `base` (maps merge, everything else replaces)."""
    out = copy.deepcopy(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def helmfile_build(repo_root):
    """Return a callable running `helmfile build`, yielding the parsed documents."""

    def build() -> list[dict]:
        if shutil.which("helmfile") is None:
            pytest.fail("helmfile is not on PATH (run under `mise exec`/`mise run`)")
        proc = subprocess.run(  # noqa: PLW1510
            ["helmfile", "-f", "helmfile.yaml.gotmpl", "build"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            # Hermetic: no reachable cluster, so the prepare hook skips its kubectl diff.
            env={**os.environ, "KUBECONFIG": "/dev/null"},
        )
        # An empty `releases: []` skeleton exits 3 ("no releases found") but still renders.
        empty = proc.returncode == 3 and "no releases found" in proc.stderr
        assert proc.returncode == 0 or empty, f"helmfile build failed:\n{proc.stderr}"
        return [d for d in yaml.safe_load_all(proc.stdout) if d]

    return build


@pytest.fixture(scope="session")
def layered_values(repo_root):
    """Return a callable merging a release's plain values files in helmfile order."""

    def layer(release: str) -> dict:
        merged: dict = {}
        for rel in (
            "values/common.yaml",
            f"values/{release}/common.yaml",
            f"values/{release}/default.yaml",
        ):
            path = repo_root / rel
            if path.is_file():
                merged = deep_merge(merged, yaml.safe_load(path.read_text()) or {})
        return merged

    return layer

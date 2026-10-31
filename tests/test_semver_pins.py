"""Deploy only semver-tagged images: every harbor containers image the helmfile deploys is X.Y.Z.

Harbor retention prunes sha7 tags within hours (incidents 2026-10-28 and 2026-10-31), so a sha7
pin stops pulling; the X.Y.Z tags a component's main build releases survive retention. A mapping
whose `repository` is a containers image must carry its `tag`: the charts default a missing tag to
their sha7 appVersion.
"""

import re

import pytest

PREFIX = "harbor.szymonrichert.pl/containers/"
IMAGE_REF = re.compile(r"harbor\.szymonrichert\.pl/containers/[^:\s]+:(\S+)")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
COMPONENTS = (
    "agents-orchestrator",
    "agents-web",
    "agents-webhook",
    "agents-schedule",
    "agents-matrix-connector",
)
# Every installed release of helmfile.yaml.gotmpl; test_every_installed_release_is_guarded keeps
# this list equal to what `helmfile build` renders.
RELEASES = (
    "agent-orchestrator-db",
    "agents-orchestrator-db",
    "agents-orchestrator",
    "agents-webhook-db",
    "agents-matrix-connector-db",
    "agents-schedule-db",
    "agents-webhook",
    "agents-schedule",
    "agents-matrix-connector",
    "agents-web",
)


def _installed_releases(helmfile_build) -> list[str]:
    return [
        rel["name"]
        for doc in helmfile_build()
        for rel in doc.get("releases", [])
        if rel.get("installed", True) is not False
    ]


def _image_refs(node, path="") -> list[tuple[str, str | None]]:
    """Every containers image reference under `node` as (path, tag); tag None when missing."""
    refs: list[tuple[str, str | None]] = []
    if isinstance(node, dict):
        repo = node.get("repository")
        if isinstance(repo, str) and repo.startswith(PREFIX):
            tag = node.get("tag")
            refs.append((f"{path}.repository", None if tag is None else str(tag)))
        for key, val in node.items():
            refs.extend(_image_refs(val, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, val in enumerate(node):
            refs.extend(_image_refs(val, f"{path}[{i}]"))
    elif isinstance(node, str):
        refs.extend((path, m.group(1)) for m in IMAGE_REF.finditer(node))
    return refs


def test_every_installed_release_is_guarded(helmfile_build):
    assert sorted(_installed_releases(helmfile_build)) == sorted(RELEASES)


@pytest.mark.parametrize("release", RELEASES)
def test_release_images_are_semver(release, layered_values):
    bad = [
        f"{path}: {tag!r}"
        for path, tag in _image_refs(layered_values(release))
        if tag is None or not SEMVER.match(tag)
    ]
    assert not bad, f"{release} deploys non-X.Y.Z containers tags: {bad}"


def test_every_component_has_a_containers_image(layered_values):
    for component in COMPONENTS:
        assert _image_refs(layered_values(component)), f"{component}: no containers image found"

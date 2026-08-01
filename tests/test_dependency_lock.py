"""The lock files have to mean what they claim.

Delphi names five dependencies and installs twenty-six: the five, and
everything they pull in behind them. Those were all unpinned, so a build
collected whatever was newest on the day it ran — two builds of one commit
could differ, CI could pass against versions the deploy never saw, and a
compromised release anywhere in that tree would be installed on the next
deploy without anyone doing anything.

requirements.txt now pins all of them, each with the hash of the file it must
be, and both the image and CI install with --require-hashes.

What can still go wrong is drift: an edit to requirements.in that nobody
recompiles, leaving the lock — the thing actually installed — describing
something else. That is what this checks. It reads the files rather than
resolving anything, so it needs no network and cannot fail because an
unrelated package happened to publish a release this morning.
"""
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
LOCKS = {"requirements.in": "requirements.txt",
         "requirements-dev.in": "requirements-dev.txt"}


def _read(name):
    return (ROOT / name).read_text()


def _direct_requirements(name):
    """The dependencies named in a .in file, following any -r includes."""
    out = []
    for line in _read(name).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            out += _direct_requirements(line[3:].strip())
            continue
        if line.startswith("-"):
            continue
        out.append(Requirement(line))
    return out


def _locked(name):
    """package -> version, from a compiled lock file."""
    pinned = {}
    for match in re.finditer(r"^([A-Za-z0-9_.\-]+)==([^\s\\;]+)", _read(name), re.M):
        pinned[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pinned


@pytest.mark.parametrize("source,lock", LOCKS.items())
def test_every_named_dependency_is_locked(source, lock):
    """A dependency added to .in but never compiled would not be installed."""
    missing = [r.name for r in _direct_requirements(source)
               if r.name.lower().replace("_", "-") not in _locked(lock)]
    assert not missing, (
        f"{missing} named in {source} but absent from {lock} — recompile it: "
        f"uv pip compile --generate-hashes --python-version 3.12 {source} -o {lock}")


@pytest.mark.parametrize("source,lock", LOCKS.items())
def test_the_locked_versions_satisfy_what_was_asked_for(source, lock):
    """Catches a bound tightened in .in that the lock still violates."""
    pinned = _locked(lock)
    for req in _direct_requirements(source):
        got = pinned[req.name.lower().replace("_", "-")]
        assert req.specifier.contains(Version(got), prereleases=True), (
            f"{lock} pins {req.name}=={got}, outside the {req.specifier} "
            f"that {source} asks for — recompile {lock}")


@pytest.mark.parametrize("lock", LOCKS.values())
def test_everything_in_the_lock_is_pinned_exactly(lock):
    """One >= line would reopen the whole hole for that package's subtree."""
    loose = [ln.split()[0] for ln in _read(lock).splitlines()
             if re.match(r"^[A-Za-z0-9_.\-]+\s*(>=|<=|~=|>|<)(?!=)", ln)]
    assert not loose, f"{lock} does not pin {loose} to an exact version"


@pytest.mark.parametrize("lock", LOCKS.values())
def test_every_package_carries_hashes(lock):
    """Pinning says which version; the hash says which *file*.

    Without it a pin only asserts the label, and a package index serving
    different bytes under the same version would go unnoticed. The hashes are
    what make this a supply-chain control rather than a reproducibility one.
    """
    text = _read(lock)
    # Each pinned line is followed by one or more --hash= entries before the
    # next package begins.
    blocks = re.split(r"^(?=[A-Za-z0-9_.\-]+==)", text, flags=re.M)[1:]
    unhashed = [b.split("==")[0] for b in blocks if "--hash=sha256:" not in b]
    assert not unhashed, f"{lock} has no hashes for {unhashed}"


@pytest.mark.parametrize("lock", LOCKS.values())
def test_the_lock_covers_far_more_than_the_five_named(lock):
    """The transitive tree is the point — it is where an unpinned build hurts."""
    assert len(_locked(lock)) >= 20


@pytest.mark.parametrize("site", [
    "Dockerfile",
    ".github/workflows/ci.yml",
    "run.sh",
    ".devcontainer/start.sh",
    ".devcontainer/devcontainer.json",
])
def test_every_install_site_requires_hashes(site):
    """A lock nothing installs with --require-hashes is a comment.

    pip only enforces hashes when asked. Without the flag it happily accepts
    the pinned versions and ignores the fingerprints entirely, which is the
    quiet way to keep the file and lose the protection — and there is more than
    one place that installs, so checking only the image would miss most of them.
    """
    text = _read(site)
    for line in text.splitlines():
        if "pip install" in line and "requirements" in line:
            assert "--require-hashes" in line, f"{site} installs without --require-hashes: {line.strip()}"
            break
    else:
        pytest.fail(f"{site} no longer installs requirements — update this test")


def test_ci_and_the_image_install_the_same_versions():
    """CI testing versions the deploy never sees is the failure to avoid.

    requirements-dev.in includes requirements.in, so the two are resolved
    together; this makes sure they did not drift apart afterwards.
    """
    runtime, dev = _locked("requirements.txt"), _locked("requirements-dev.txt")
    differing = {p: (v, dev[p]) for p, v in runtime.items()
                 if p in dev and dev[p] != v}
    assert not differing, (
        f"requirements-dev.txt disagrees with requirements.txt about {differing} "
        f"— recompile both together")


def test_the_dependency_updater_is_configured():
    """Pinning without updating is the worse half of the trade.

    Frozen dependencies stop changing under you and also stop receiving fixes,
    so the lock only makes sense next to something that proposes updates for
    CI to test.
    """
    config = ROOT / ".github" / "dependabot.yml"
    assert config.exists(), "nothing is watching for dependency updates"
    text = config.read_text()
    assert "pip" in text

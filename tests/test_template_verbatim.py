"""Pin the files this repository copies unchanged from the collector template.

These paths are shared fleet-wide. A convenience edit to any of them is drift,
not a fix: it silently forks one collector away from the template everyone else
still tracks. Each expected value is the Git blob hash of the template copy at
template commit c12185cf98a0918afd02e2d2f505599877018c5b, recomputed here from
the bytes on disk so the check needs neither a network nor a git checkout.

If the template itself moves, re-copy the file and update the hash in the same
commit, quoting the new template SHA.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.config import ROOT_DIR

TEMPLATE_COMMIT = "c12185cf98a0918afd02e2d2f505599877018c5b"

VERBATIM: dict[str, str] = {
    ".gitignore": "f0d1368264d24d7959d3137d618930a06f33795e",
    "scripts/__init__.py": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
    "scripts/databricks_engine.py": "1d9848f5963a06902dce6e56b24d96e0f67d251b",
    ".github/copilot-instructions.md": "d58921719c9cf0fd8cf442f46d1b9b21113d28a0",
    ".github/prompts/onboard-new-api.prompt.md": "979cae7629beaa112ba8653c4ce13510779ae80e",
    ".github/skills/audit-collector/SKILL.md": "321f4f945e500c8d2ce9702aceb64d203bcc8f69",
    ".github/skills/build-collector/SKILL.md": "a6fcf090c72422b299d8e45eec99e4a4db7b18fd",
    ".github/skills/code-review/SKILL.md": "292c6ff9c74ac0d3cf88d60d855deed6fa5fe36f",
    ".github/skills/get-api-docs/SKILL.md": "d2834b3de2fab7ed853066af934777a8707e43d7",
    ".github/skills/planning-and-design/SKILL.md": "0a8f47fa2ee3fe6c8b09662fee0132d9fb98d865",
    ".github/skills/research-first/SKILL.md": "c8a9d4f6492b3f82dfc6ea8f449ba58f2f765b48",
    ".github/skills/security-review/SKILL.md": "5857739acddfd1f1bb8b6f9fe5272a9d592c3a44",
    ".github/skills/series-selection/SKILL.md": "b58860245f6fbb6af123bf8587224b7f2ace18de",
    ".github/skills/systematic-debugging/SKILL.md": "df313cb3d591ecf742d58ee0f1dfa15f6e35974d",
    ".github/skills/test-driven-development/SKILL.md": "19a8e3ac7937ac5799c7c9ef4ffa94d4fdbe6f3e",
    ".github/skills/verification-loop/SKILL.md": "53827cbc812b1105abf356d633d4e3e5329d2c2f",
    ".vscode/launch.json": "430b80db4450110af11567b260fa4658f12c1f62",
    ".vscode/settings.json": "0facdc9526ed15fae0c85ec6434d9248c73ab84f",
}


def _blob_hash(path: Path) -> str:
    """Return the Git blob hash of ``path``, reading bytes verbatim."""
    payload = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()


@pytest.mark.parametrize("relative_path", sorted(VERBATIM))
def test_template_file_is_unchanged(relative_path: str) -> None:
    path = Path(ROOT_DIR) / relative_path
    assert path.is_file(), f"{relative_path} is copied from the template and must exist"
    assert _blob_hash(path) == VERBATIM[relative_path], (
        f"{relative_path} has drifted from collector template {TEMPLATE_COMMIT}. "
        "Re-copy it from the template instead of editing it here."
    )


def test_gitignore_still_protects_raw_downloads() -> None:
    """The template .gitignore carries _raw/, so raw payloads stay unversioned.

    This is asserted separately because the protection must survive even if the
    hash above is legitimately refreshed against a newer template.
    """
    lines = (Path(ROOT_DIR) / ".gitignore").read_text(encoding="utf-8").split()
    assert "_raw/" in lines
    assert ".env" in lines


def test_no_build_artifacts_are_tracked() -> None:
    """`pip install -e .` drops an egg-info tree the VERBATIM .gitignore misses.

    The tracked .gitignore may not be edited, so these artifacts are excluded
    through .git/info/exclude (see README). Guard the outcome, not the
    mechanism: the repository must never version build or virtualenv output.
    """
    import subprocess

    root = Path(ROOT_DIR)
    try:
        listing = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:  # pragma: no cover -- Git absent, e.g. a source tarball
        pytest.skip("git is not available")
    if listing.returncode != 0:  # pragma: no cover -- not a Git checkout
        pytest.skip("not a Git checkout")

    offenders = sorted(
        path
        for path in listing.stdout.splitlines()
        if path.startswith((".venv/", "_raw/", "_verify_xls/"))
        or ".egg-info/" in path
        or "__pycache__/" in path
    )
    assert not offenders, f"build artifacts must not be versioned: {offenders}"

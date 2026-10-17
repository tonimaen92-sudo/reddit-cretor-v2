"""Stable project paths shared by packaged and legacy entry points."""

from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT.parent
PROJECT_ROOT = SRC_ROOT.parent
LEGACY_MISC_DIR = PROJECT_ROOT / "misc"
TESTS_ROOT = PROJECT_ROOT / "tests"
FIXTURES_DIR = TESTS_ROOT / "fixtures"
LOG_DIR = PROJECT_ROOT / "logs"
DUMP_DIR = PROJECT_ROOT / "dumps"


def project_path(*parts: str) -> Path:
    """Return a path rooted at the checkout without depending on module depth."""

    return PROJECT_ROOT.joinpath(*parts)

"""Shared fixtures. `pythonpath = ["scripts"]` in pyproject makes rius_cc importable."""
import pathlib

import pytest


@pytest.fixture
def fixtures_dir() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures"

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from l4_fakes import Harness  # noqa: E402


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)

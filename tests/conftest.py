import gzip
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    p = FIX / name
    if p.suffix == ".gz":
        return gzip.open(p, "rt", encoding="utf-8").read()
    return p.read_text(encoding="utf-8")


@pytest.fixture
def fixture():
    return load

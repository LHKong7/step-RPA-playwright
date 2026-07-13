from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def page1() -> str:
    return (FIXTURES / "page1.html").as_uri()


@pytest.fixture
def form() -> str:
    return (FIXTURES / "form.html").as_uri()

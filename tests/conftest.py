import pytest

from aurora_contracts import loader


@pytest.fixture(scope="session")
def layer():
    return loader.load()

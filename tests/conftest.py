import pytest

from aurora_contracts import loader


@pytest.fixture(scope="session")
def layer():
    return loader.load()


SYNTHETIC_ARGS = ["--subscribers", "600", "--days", "140", "--seed", "11"]


@pytest.fixture(scope="session")
def synthetic_data(tmp_path_factory):
    """One synthetic dataset shared by the synthetic and validator tests."""
    import generate_synthetic

    out = tmp_path_factory.mktemp("synthetic")
    assert generate_synthetic.main([*SYNTHETIC_ARGS, "--out", str(out)]) == 0
    return out

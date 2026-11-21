import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from datalens import config, loader  # noqa: E402
from datalens.engine import Engine  # noqa: E402


@pytest.fixture(scope="session")
def engine() -> Engine:
    if not config.DB_PATH.exists():
        loader.build()
    return Engine()

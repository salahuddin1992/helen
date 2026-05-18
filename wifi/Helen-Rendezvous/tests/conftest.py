"""Shared pytest configuration for the rendezvous test suite."""

import sys
from pathlib import Path

# Ensure the project root is on sys.path so `import storage`, `import cluster`,
# `import main` work whether pytest is invoked from the project root or from
# inside the tests/ directory.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# Enable pytest-asyncio "auto" mode locally — every async def test is treated
# as an asyncio test, every async fixture is recognised. Avoids having to
# decorate each item by hand.
def pytest_collection_modifyitems(config, items):  # pragma: no cover
    pass


import pytest_asyncio  # noqa: F401  (ensures plugin is loaded)


def pytest_configure(config):
    # Equivalent to setting asyncio_mode = auto in pytest.ini.
    config.option.asyncio_mode = "auto"

# Copyright 2026 jerzy.husakowski@canonical.com
# See LICENSE file for licensing details.

"""Fixtures for jubilant-based charm integration tests."""

import logging
import sys
import time

import jubilant
import pytest

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest):
    """Temporary Juju model on the LXD cloud (the controller has several clouds)."""
    with jubilant.temp_model(cloud="localhost") as juju:
        yield juju

        if request.session.testsfailed:
            logger.info("Collecting Juju logs...")
            time.sleep(0.5)  # Wait for Juju to process logs.
            log = juju.debug_log(limit=1000)
            print(log, end="", file=sys.stderr)

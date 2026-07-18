"""The cli and charm-payload copies of the shared modules must not drift.

The charm payload cannot import an installed package on nodes, so
representatives.py and schemas.py are duplicated between cli/ and
charm/payload/ (see task 1.12). These tests make drift impossible to miss:
the copies must be byte-identical and must produce identical representative
selections for every golden fixture topology.
"""

import pytest
from conftest import REPO_ROOT, TOPOLOGY_FIXTURES, load_fixture, load_module_from_path

from cli import representatives as cli_representatives
from cli import schemas as cli_schemas

PAYLOAD = REPO_ROOT / "charm" / "payload"

payload_representatives = load_module_from_path(
    PAYLOAD / "representatives.py", "payload_representatives"
)
payload_schemas = load_module_from_path(PAYLOAD / "schemas.py", "payload_schemas")


@pytest.mark.parametrize("module", ["schemas.py", "representatives.py"])
def test_copies_are_byte_identical(module):
    cli_copy = (REPO_ROOT / "cli" / module).read_bytes()
    payload_copy = (PAYLOAD / module).read_bytes()
    assert cli_copy == payload_copy, (
        f"cli/{module} and charm/payload/{module} have drifted; they must be edited together"
    )


@pytest.mark.parametrize("path", TOPOLOGY_FIXTURES, ids=[p.stem for p in TOPOLOGY_FIXTURES])
def test_identical_selections_for_every_fixture_topology(path):
    doc = load_fixture(path)
    assert cli_representatives.select_representatives(
        doc
    ) == payload_representatives.select_representatives(doc)


@pytest.mark.parametrize("path", TOPOLOGY_FIXTURES, ids=[p.stem for p in TOPOLOGY_FIXTURES])
def test_identical_topology_validation_for_every_fixture(path):
    doc = load_fixture(path)
    assert cli_schemas.validate_topology(doc) == payload_schemas.validate_topology(doc)

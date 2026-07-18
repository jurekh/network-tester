"""Deterministic representative selection from the structured rule parameters."""

import random

import pytest
from conftest import FIXTURES, load_fixture

from cli import representatives


def topology(name):
    return load_fixture(FIXTURES / f"topology_{name}.json")


def test_single_rack_has_source_but_no_targets():
    selections = representatives.select_representatives(topology("single_rack"))
    assert selections == {"rack-1": {"source": "aaa001", "targets": {}}}


def test_two_rack_selection():
    selections = representatives.select_representatives(topology("two_rack"))
    assert selections == {
        "rack-1": {
            "source": "aaa001",
            "targets": {"rack-2": {"representative": "bbb001", "fallback": "bbb002"}},
        },
        "rack-2": {
            "source": "bbb001",
            "targets": {"rack-1": {"representative": "aaa001", "fallback": "aaa002"}},
        },
    }


def test_mixed_scope_excludes_out_of_scope_machines():
    """Out-of-scope machines are never representatives or fallbacks."""
    selections = representatives.select_representatives(topology("mixed_scope"))
    # bbb001 is in_scope: false, so rack-2's representative is bbb002.
    # aaa003 is in_scope: false, so rack-1's fallback is aaa002.
    assert selections == {
        "rack-1": {
            "source": "aaa001",
            "targets": {"rack-2": {"representative": "bbb002", "fallback": "bbb003"}},
        },
        "rack-2": {
            "source": "bbb002",
            "targets": {"rack-1": {"representative": "aaa001", "fallback": "aaa002"}},
        },
    }


def test_mixed_fabric_host_is_selected_by_system_id():
    """A management+data host is eligible; rack with one data node has no fallback."""
    selections = representatives.select_representatives(topology("mixed_fabric_host"))
    assert selections == {
        "rack-1": {
            "source": "aaa001",
            "targets": {"rack-2": {"representative": "bbb001", "fallback": None}},
        },
        "rack-2": {
            "source": "bbb001",
            "targets": {"rack-1": {"representative": "aaa001", "fallback": None}},
        },
    }


@pytest.mark.parametrize("name", ["single_rack", "two_rack", "mixed_scope", "mixed_fabric_host"])
def test_selection_independent_of_machine_order(name):
    doc = topology(name)
    expected = representatives.select_representatives(doc)
    rng = random.Random(42)
    for _ in range(5):
        rng.shuffle(doc["machines"])
        assert representatives.select_representatives(doc) == expected


def test_non_data_roles_never_selected():
    doc = topology("mixed_fabric_host")
    selections = representatives.select_representatives(doc)
    data_ids = {m["system_id"] for m in doc["machines"] if m["role"] == "data" and m["in_scope"]}
    for entry in selections.values():
        assert entry["source"] in data_ids
        for target in entry["targets"].values():
            assert target["representative"] in data_ids
            assert target["fallback"] is None or target["fallback"] in data_ids


def test_unknown_strategy_raises():
    doc = topology("two_rack")
    rule = doc["reachability_model"]["rules"]["cross-rack-data-routing"]
    rule["parameters"]["source_selection"]["strategy"] = "random"
    with pytest.raises(ValueError, match="strategy"):
        representatives.select_representatives(doc)


def test_unknown_field_raises():
    doc = topology("two_rack")
    rule = doc["reachability_model"]["rules"]["cross-rack-data-routing"]
    rule["parameters"]["source_selection"]["field"] = "hostname"
    with pytest.raises(ValueError, match="field"):
        representatives.select_representatives(doc)


def test_missing_rule_raises():
    doc = topology("two_rack")
    del doc["reachability_model"]["rules"]["cross-rack-data-routing"]
    with pytest.raises(ValueError, match="cross-rack-data-routing"):
        representatives.select_representatives(doc)

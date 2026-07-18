# Copyright 2026 jerzy.husakowski@canonical.com
# See LICENSE file for licensing details.

from unittest.mock import patch

import pytest
from ops import testing

from charm import PROBE_PACKAGES, NetworkTesterCharm


def test_install_installs_probe_packages():
    ctx = testing.Context(NetworkTesterCharm)
    with patch("charm.subprocess.run") as run:
        ctx.run(ctx.on.install(), testing.State())
    run.assert_called_once()
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["apt", "install", "-y"]
    assert set(PROBE_PACKAGES) <= set(cmd)


def test_start_sets_active():
    ctx = testing.Context(NetworkTesterCharm)
    state_out = ctx.run(ctx.on.start(), testing.State())
    assert state_out.unit_status == testing.ActiveStatus()


def test_collect_results_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("charm.PROBE_OUTPUT_PATH", tmp_path / "probe-output.json")
    ctx = testing.Context(NetworkTesterCharm)
    ctx.run(ctx.on.action("collect-results"), testing.State())
    assert ctx.action_results is not None
    assert ctx.action_results["status"] == "missing"
    assert ctx.action_results["unit"].startswith("network-tester/")


def test_collect_results_returns_probe_output(tmp_path, monkeypatch):
    output = tmp_path / "probe-output.json"
    output.write_text('{"schema_version": "1"}')
    monkeypatch.setattr("charm.PROBE_OUTPUT_PATH", output)
    ctx = testing.Context(NetworkTesterCharm)
    ctx.run(ctx.on.action("collect-results"), testing.State())
    assert ctx.action_results is not None
    assert ctx.action_results["probe-output"] == '{"schema_version": "1"}'


if __name__ == "__main__":  # pragma: nocover
    pytest.main([__file__])

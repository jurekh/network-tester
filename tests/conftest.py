import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

TOPOLOGY_FIXTURES = sorted(FIXTURES.glob("topology_*.json"))
PROBE_OUTPUT_FIXTURES = sorted(FIXTURES.glob("probe_output_*.json"))
REPORT_FIXTURES = sorted(FIXTURES.glob("report_*.json"))


def load_fixture(path):
    return json.loads(Path(path).read_text())


def load_module_from_path(path, name):
    """Import a module from an explicit file path under a unique name.

    Used to load the charm payload copies of the shared modules without
    clashing with the cli package imports of the same module names.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

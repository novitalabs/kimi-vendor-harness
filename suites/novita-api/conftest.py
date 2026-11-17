from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


SELF_TEST_ROOT = Path(__file__).resolve().parent
PRODUCT_ROOT = SELF_TEST_ROOT.parents[1]
KVV_ROOT = Path(os.environ.get("KVV_ROOT", PRODUCT_ROOT / "vendor" / "kvv-k3"))
for path in (PRODUCT_ROOT, SELF_TEST_ROOT, KVV_ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

KVV_PLUGIN_NAME = "nvst_kvv_pytest_conftest"
KVV_CONFTEST = KVV_ROOT / "tests" / "conftest.py"
if KVV_PLUGIN_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(KVV_PLUGIN_NAME, KVV_CONFTEST)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load KVV pytest fixtures from {KVV_CONFTEST}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[KVV_PLUGIN_NAME] = module
    spec.loader.exec_module(module)

pytest_plugins = (KVV_PLUGIN_NAME,)


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: live endpoint or captured-service check")

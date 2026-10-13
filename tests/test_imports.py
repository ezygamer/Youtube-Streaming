"""Every entry-point module must at least import (catches syntax errors in code no other test loads)."""
import importlib

import pytest


@pytest.mark.parametrize("module", ["core.app", "run_bot"])
def test_module_imports(module):
    importlib.import_module(module)

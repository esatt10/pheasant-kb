"""Deprecated import compatibility; implementation lives in pheasant.artifacts.formation."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("pheasant.artifacts.formation")

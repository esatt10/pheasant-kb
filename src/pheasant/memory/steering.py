"""Deprecated import compatibility; implementation lives in pheasant.artifacts.steering."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("pheasant.artifacts.steering")

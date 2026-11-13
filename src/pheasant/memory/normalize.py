"""Deprecated import compatibility; implementation lives in pheasant.artifacts.normalize."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("pheasant.artifacts.normalize")

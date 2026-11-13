"""Deprecated import compatibility; implementation lives in pheasant.artifacts.source_removal."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("pheasant.artifacts.source_removal")

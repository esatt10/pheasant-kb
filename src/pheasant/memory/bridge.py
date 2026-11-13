"""Deprecated import compatibility; implementation lives in pheasant.artifacts.bridge."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("pheasant.artifacts.bridge")

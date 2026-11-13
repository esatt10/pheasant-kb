"""Deprecated import compatibility; implementation lives in pheasant.artifacts.policy."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("pheasant.artifacts.policy")

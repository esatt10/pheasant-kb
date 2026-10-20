"""The base every config section derives from: dump to plain data.

Its own module so a section can live outside ``schema.py`` — the split
``tests/test_module_budget.py`` asks for ("one module per section") — without
importing the module that imports it. ``schema.py`` re-exports it, so
``from pheasant.config.schema import ModelMixin`` keeps working.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


@dataclass
class ModelMixin:
    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, Path):
                return str(v) if mode == "json" else v
            if isinstance(v, Enum):
                return v.value
            if isinstance(v, str) and type(v) is not str:
                # A *subclass* of str — `PluginSourceType` is the one that
                # occurs — is not plain data, and consumers that dispatch on
                # the exact type refuse it. PyYAML's representer is one:
                # `yaml.safe_dump` raises `RepresenterError("cannot represent
                # an object")` for it, so `config_hash` crashed for any config
                # using a connector plugin — which is every third-party
                # connector and all five first-party SaaS ones. A dump is
                # plain data in both modes; the loader re-creates the rich
                # type on the way back in.
                return str(v)
            if isinstance(v, ModelMixin):
                return v.model_dump(mode=mode)
            if isinstance(v, list):
                return [conv(i) for i in v]
            if isinstance(v, dict):
                return {k: conv(val) for k, val in v.items()}
            return v

        return {k: conv(v) for k, v in asdict(self).items()}

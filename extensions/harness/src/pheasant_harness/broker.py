"""The trusted broker: authority lives here, never in the model.

Every action the agent proposes is a typed request the broker checks in code:
the tool exists and its arguments have the declared types; the run's task
contract grants the capability; every resource it names is one of the run's
inputs *and* one the actor may still read; a write names the destination the
task contract authorized; and the budget has room. Text from sources and from
the model is data: nothing in it can widen any of these. A retrieved
instruction is noticed (``untrusted_instructions``) and logged with its
disposition, but the boundary is the check, not the detection.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from pheasant_harness import budget
from pheasant_harness.common import POLICY_VERSION

# Each tool: argument types, the effect class it has, and the capability a
# task contract must grant for it.
TOOLS: dict[str, dict[str, Any]] = {
    "resolve": {"args": {"registration_id": str}, "effect": "read", "capability": "read"},
    "discover": {"args": {"query": str}, "effect": "read", "capability": "discover"},
    "enumerate": {"args": {"registration_id": str}, "effect": "read", "capability": "read"},
    "grep": {
        "args": {"registration_id": str, "pattern": str},
        "effect": "read",
        "capability": "read",
    },
    "read": {"args": {"registration_id": str, "path": str}, "effect": "read", "capability": "read"},
    "inspect": {
        "args": {"registration_id": str, "path": str},
        "effect": "read",
        "capability": "inspect",
    },
    "follow": {
        "args": {"registration_id": str, "symbol": str},
        "effect": "read",
        "capability": "read",
    },
    "compare": {"args": {"registration_id": str}, "effect": "read", "capability": "read"},
    "materialize": {
        "args": {"registration_id": str},
        "effect": "workspace",
        "capability": "workspace",
    },
    "apply_patch": {"args": {"edits": list}, "effect": "workspace", "capability": "workspace"},
    "execute": {"args": {"argv": list}, "effect": "workspace", "capability": "execute"},
    "draft": {"args": {"name": str, "content": str}, "effect": "stage", "capability": "draft"},
    "render": {"args": {"digest": str}, "effect": "stage", "capability": "draft"},
    "verify": {"args": {"criterion": dict}, "effect": "verify", "capability": "verify"},
    "deliver": {"args": {"digest": str}, "effect": "external", "capability": "deliver"},
}

WORKLOAD_CAPABILITIES = {
    "answer": {"read", "discover", "inspect", "draft", "verify", "deliver"},
    "summarize": {"read", "discover", "inspect", "draft", "verify", "deliver"},
    "build_content": {"read", "discover", "inspect", "draft", "verify", "deliver"},
    "code_change": {
        "read",
        "discover",
        "inspect",
        "workspace",
        "execute",
        "draft",
        "verify",
        "deliver",
    },
}

DEFAULT_COMMANDS = ("python", "python3", "pytest", "node", "git", "sh", "true", "sleep")

_INSTRUCTION_PATTERNS = tuple(
    re.compile(p, re.I)
    for p in (
        r"\b(ignore|disregard|override|forget|bypass)\b.{0,60}\b(previous|prior|above|system|all)\b"
        r".{0,30}\b(instructions?|prompts?|rules?|polic(y|ies))\b",
        r"\b(call|invoke|run|execute)\b.{0,40}\b(tool|function|shell|command|deliver|publish)\b",
        r"\b(send|upload|exfiltrate|post|email|publish)\b.{0,60}\b(to|at)\b.{0,40}"
        r"(https?://|@|\bwebhook\b)",
        r"\b(you are now|you must|assistant must|as the agent)\b",
        r"<\|\s*(system|assistant|developer)[^<>]{0,40}\|>",
    )
)


def untrusted_instructions(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = " ".join(
        "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf").split()
    )
    return any(pattern.search(normalized) for pattern in _INSTRUCTION_PATTERNS)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    tool: str
    effect_class: str
    capability: str
    reason: str | None
    policy_version: str = POLICY_VERSION

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "tool": self.tool,
            "effect_class": self.effect_class,
            "capability": self.capability,
            "reason": self.reason,
            "policy_version": self.policy_version,
        }


class Broker:
    def __init__(self, harness: Any):
        self.harness = harness

    def authorize(self, run: dict, action: str, args: dict, usage: dict) -> Decision:
        spec = TOOLS.get(action)
        if spec is None:
            return Decision(False, action, "unknown", "none", f"unknown tool {action!r}")
        effect, capability = spec["effect"], spec["capability"]

        def deny(reason: str) -> Decision:
            return Decision(False, action, effect, capability, reason)

        for name, kind in spec["args"].items():
            if not isinstance(args.get(name), kind):
                return deny(f"argument {name!r} must be {kind.__name__}")
        request = run["request"]
        granted = set(request.get("capabilities") or WORKLOAD_CAPABILITIES[request["workload"]])
        if capability not in granted:
            return deny(f"the task contract grants no {capability!r} capability")
        inputs = set(request.get("inputs") or [])
        reg_id = args.get("registration_id")
        if reg_id is not None:
            if reg_id in set(request.get("fixtures") or []):
                # Held-out graders are the verifier's; the agent never reads them.
                return deny(f"{reg_id} is a verification fixture, outside the agent's reach")
            if reg_id not in inputs:
                return deny(f"{reg_id} is not one of this run's inputs")
            try:
                reg = self.harness.registry.get(reg_id)
            except Exception:  # noqa: BLE001 - an unknown id is a denial, not a crash
                return deny(f"{reg_id} is not a registration")
            if not self.harness.registry.authorized(reg, run["actor"]):
                return deny(f"{run['actor']} may no longer read {reg_id}")
        if action == "verify":
            for fixture in (args.get("criterion") or {}).get("fixtures") or []:
                if fixture not in set(request.get("fixtures") or []):
                    return deny(f"{fixture} is not a declared fixture of this run")
        if action == "execute":
            argv = args["argv"]
            if not argv or not all(isinstance(a, str) for a in argv):
                return deny("argv must be a non-empty list of strings")
            allowed = set(request.get("allowed_commands") or DEFAULT_COMMANDS)
            if argv[0].rsplit("/", 1)[-1] not in allowed:
                return deny(f"command {argv[0]!r} is outside the allowed set")
        if action == "deliver":
            destination = request.get("destination") or {"adapter": "record", "target": ""}
            asked = args.get("target", destination.get("target"))
            if args.get("adapter", destination.get("adapter")) != destination.get(
                "adapter"
            ) or asked != destination.get("target"):
                return deny("delivery target differs from the authorized destination")
        if action == "discover" and request.get("discovery") == "off":
            return deny("discovery is disabled for this run")
        ok, why = budget.admit(run["limits"], usage, action)
        if not ok:
            return deny(why or "budget")
        return Decision(True, action, effect, capability, None)

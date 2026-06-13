"""Agent framework — Layer 3.

Each agent ingests warehouse facts (+ optional LLM narrative), emits a
structured view, and persists it. Agents are intentionally quant-grounded:
the deterministic metrics are always present; the LLM adds interpretation and
a directional read when a key is configured, otherwise a rule-based fallback runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..data.universe import Universe, load_universe
from ..llm.client import llm
from ..logging_conf import get_logger
from ..storage.db import save_agent_output

log = get_logger("agents")


@dataclass
class Context:
    universe: Universe = field(default_factory=load_universe)
    extras: dict[str, Any] = field(default_factory=dict)


class Agent:
    name: str = "agent"
    role: str = ""

    def __init__(self) -> None:
        self.llm = llm()

    def run(self, ctx: Context) -> dict:
        try:
            out = self.analyse(ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("Agent %s failed: %s", self.name, exc)
            out = {"error": str(exc)}
        out["agent"] = self.name
        save_agent_output(self.name, out.get("scope", "market"), out)
        return out

    def analyse(self, ctx: Context) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    # helper: LLM reasoning with a quant-only fallback ----------------
    def reason(self, facts: dict, instruction: str, schema_hint: str, fallback: dict) -> dict:
        if not self.llm.available:
            return {**fallback, "source": "rule_based"}
        import json

        prompt = (
            f"You are the {self.role}. Analyse these factual Indian-market metrics "
            f"and produce a concise read.\n\nFACTS:\n{json.dumps(facts, default=str)}\n\n"
            f"{instruction}\n\nReturn JSON: {schema_hint}"
        )
        out = self.llm.complete_json(prompt, default=None)
        if not isinstance(out, dict):
            return {**fallback, "source": "rule_based"}
        out["source"] = "llm"
        return out


_REGISTRY: dict[str, type[Agent]] = {}


def register(cls: type[Agent]) -> type[Agent]:
    _REGISTRY[cls.name] = cls
    return cls


def all_agents() -> list[Agent]:
    return [cls() for cls in _REGISTRY.values()]


def get_registry() -> dict[str, type[Agent]]:
    return dict(_REGISTRY)

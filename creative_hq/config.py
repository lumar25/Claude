"""Loads settings, agent definitions and client knowledge bases from disk."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    raw: dict

    @property
    def per_run_usd(self) -> float:
        return float(self.raw["budgets"]["per_run_usd"])

    @property
    def per_day_usd(self) -> float:
        return float(self.raw["budgets"]["per_day_usd"])

    @property
    def max_revision_rounds(self) -> int:
        return int(self.raw["workflow"]["max_revision_rounds"])

    @property
    def task_retries(self) -> int:
        return int(self.raw["workflow"]["task_retries"])

    @property
    def max_deliverables(self) -> int:
        return int(self.raw["workflow"]["max_deliverables"])

    def path(self, key: str) -> Path:
        return ROOT / self.raw["paths"][key]

    def model_for(self, agent_id: str) -> str:
        models = self.raw["models"]
        return (models.get("overrides") or {}).get(agent_id, models["default"])


@dataclass
class AgentSpec:
    id: str
    name: str
    role: str
    tools: list[str]
    max_budget_usd: float
    max_turns: int
    instructions: str
    model: str = ""


@dataclass
class Client:
    client_id: str
    profile: dict
    knowledge: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.profile["company"]

    def knowledge_text(self) -> str:
        parts = [f"### {name}\n{text.strip()}" for name, text in sorted(self.knowledge.items())]
        return "\n\n".join(parts)

    def profile_text(self) -> str:
        return yaml.safe_dump(self.profile, sort_keys=False, allow_unicode=True)


def load_settings() -> Settings:
    with open(ROOT / "config" / "settings.yaml") as f:
        return Settings(yaml.safe_load(f))


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ValueError("agent file must start with YAML frontmatter")
    _, meta, body = text.split("---", 2)
    return yaml.safe_load(meta), body.strip()


def load_agents(settings: Settings) -> dict[str, AgentSpec]:
    """Returns agents keyed by role (director, researcher, ...)."""
    agents: dict[str, AgentSpec] = {}
    for path in sorted(settings.path("agents").glob("AG-*.md")):
        meta, body = _parse_frontmatter(path.read_text())
        spec = AgentSpec(
            id=meta["id"],
            name=meta["name"],
            role=meta["role"],
            tools=list(meta.get("tools") or []),
            max_budget_usd=float(meta["max_budget_usd"]),
            max_turns=int(meta["max_turns"]),
            instructions=body,
        )
        spec.model = settings.model_for(spec.id)
        agents[spec.role] = spec
    required = {"director", "researcher", "strategist", "copywriter", "designer", "supervisor"}
    missing = required - agents.keys()
    if missing:
        raise RuntimeError(f"missing agent definitions for roles: {sorted(missing)}")
    return agents


def list_clients(settings: Settings) -> list[str]:
    return sorted(p.name for p in settings.path("clients").iterdir() if (p / "profile.yaml").exists())


def load_client(settings: Settings, client_id: str) -> Client:
    base = settings.path("clients") / client_id
    if not (base / "profile.yaml").exists():
        raise FileNotFoundError(f"unknown client '{client_id}'. Known: {list_clients(settings)}")
    with open(base / "profile.yaml") as f:
        profile = yaml.safe_load(f)
    knowledge = {p.name: p.read_text() for p in sorted((base / "knowledge").glob("*.md"))}
    return Client(client_id=client_id, profile=profile, knowledge=knowledge)

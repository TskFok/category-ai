from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    model_tier: str
    temperature: float
    max_tokens: int
    system_prompt: str
    user_template: str


@lru_cache
def load_prompt(name: str) -> PromptSpec:
    path = PROMPTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return PromptSpec(
        name=raw["name"],
        version=str(raw["version"]),
        model_tier=raw.get("model_tier", "extract"),
        temperature=float(raw.get("temperature", 0.1)),
        max_tokens=int(raw.get("max_tokens", 4096)),
        system_prompt=raw["system_prompt"],
        user_template=raw["user_template"],
    )

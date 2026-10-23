"""Runtime configuration, read from environment (CTFY-0001 §1.1).

No I/O beyond reading env vars. Both the platform (image mode) and local dev
(.env) populate these. Runtime settings are parsed here; adapter security
boundaries also inspect the environment to isolate tools and redact credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # --- platform ---
    platform_url: str = ""
    token: str = ""
    challenge_id: str = ""
    competition_id: str = ""
    work_dir: str = "/ctfy/work"
    max_turns: int | None = None
    max_tokens: int | None = None
    dry_run: bool = False
    # --- model gateway (CTFY-0001 §1.1) ---
    llm_flavor: str = ""          # "anthropic" | "openai"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    @property
    def default_result_path(self) -> str:
        """Fallback result path; task.json's result.path overrides this."""
        return os.path.join(self.work_dir, "result.json")

    @staticmethod
    def from_env() -> Settings:
        def _int(name: str) -> int | None:
            v = os.environ.get(name)
            return int(v) if v and v.lstrip("-").isdigit() else None

        return Settings(
            platform_url=os.environ.get("CTFY_PLATFORM_URL", ""),
            token=os.environ.get("CTFY_TOKEN", ""),
            challenge_id=os.environ.get("CTFY_EVAL_CHALLENGE_ID", ""),
            competition_id=os.environ.get("CTFY_EVAL_COMPETITION_ID", ""),
            work_dir=os.environ.get("CTFY_WORK_DIR", "/ctfy/work"),
            max_turns=_int("CTFY_EVAL_MAX_TURNS"),
            max_tokens=_int("CTFY_EVAL_MAX_TOKENS"),
            dry_run=os.environ.get("CTFY_EVAL_DRY_RUN") == "1",
            llm_flavor=os.environ.get("LLM_FLAVOR", ""),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_base_url=os.environ.get("LLM_BASE_URL", ""),
            llm_model=os.environ.get("LLM_MODEL", ""),
        )

"""Runtime configuration, read from environment (CTFY-0001 §1.1).

No I/O beyond reading env vars. Both the platform (image mode) and local dev
(.env) populate these. Runtime settings are parsed here; adapter security
boundaries also inspect the environment to isolate tools and redact credentials.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
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

        env = os.environ
        flavor, api_key, base_url, model = _resolve_llm(env)
        return Settings(
            platform_url=env.get("CTFY_PLATFORM_URL", ""),
            token=env.get("CTFY_TOKEN", ""),
            challenge_id=env.get("CTFY_EVAL_CHALLENGE_ID", ""),
            competition_id=env.get("CTFY_EVAL_COMPETITION_ID", ""),
            work_dir=env.get("CTFY_WORK_DIR", "/ctfy/work"),
            max_turns=_int("CTFY_EVAL_MAX_TURNS"),
            max_tokens=_int("CTFY_EVAL_MAX_TOKENS"),
            dry_run=env.get("CTFY_EVAL_DRY_RUN") == "1",
            llm_flavor=flavor,
            llm_api_key=api_key,
            llm_base_url=base_url,
            llm_model=model,
        )


def _resolve_llm(env: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Resolve (flavor, api_key, base_url, model) per CTFY-0001 §1.1.

    The platform publishes the model credential/endpoint/model under the generic
    ``LLM_*`` names AND under the provider SDK's own names (``ANTHROPIC_*`` /
    ``OPENAI_*``), selected by ``LLM_FLAVOR``. The generic names MAY be empty —
    the standard is explicit that ``LLM_API_KEY`` can be absent and
    ``LLM_BASE_URL`` can default — so a run funded only through the SDK-native
    names must still be found. We read ``LLM_*`` first, fall back to the flavor's
    SDK names, and infer the flavor from which family is present when it is unset.
    On ``anthropic`` the credential arrives under BOTH ``ANTHROPIC_AUTH_TOKEN``
    (bearer) and ``ANTHROPIC_API_KEY`` (x-api-key) carrying the same value; the
    bearer is the one a compatible gateway authenticates, so it is preferred.
    An unfunded run leaves every name unset and the key resolves to "" — the
    gateway then fails loudly rather than spending someone else's account.
    """
    flavor = env.get("LLM_FLAVOR", "").strip().lower()
    api_key = env.get("LLM_API_KEY", "")
    base_url = env.get("LLM_BASE_URL", "")
    model = env.get("LLM_MODEL", "")
    if not flavor:
        has_anthropic = any(
            env.get(n) for n in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
        )
        has_openai = any(env.get(n) for n in ("OPENAI_API_KEY", "OPENAI_BASE_URL"))
        if has_anthropic:
            flavor = "anthropic"
        elif has_openai:
            flavor = "openai"
    if flavor == "anthropic":
        api_key = api_key or env.get("ANTHROPIC_AUTH_TOKEN", "") or env.get("ANTHROPIC_API_KEY", "")
        base_url = base_url or env.get("ANTHROPIC_BASE_URL", "")
        model = model or env.get("ANTHROPIC_MODEL", "")
    elif flavor == "openai":
        api_key = api_key or env.get("OPENAI_API_KEY", "")
        base_url = base_url or env.get("OPENAI_BASE_URL", "")
        model = model or env.get("OPENAI_MODEL", "")
    return flavor, api_key, base_url, model

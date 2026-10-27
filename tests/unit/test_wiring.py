"""The submission wiring must root the executor at the platform work dir so the
challenge attachments the platform stages into /ctfy/work (CTFY-0001) reach each
solve attempt's workspace. With the default empty temp root, attachment-based
challenges start from an empty directory.
"""
from __future__ import annotations

import os

from agent import wiring
from agent.config import Settings


async def test_build_for_image_roots_executor_at_work_dir(tmp_path):
    settings = Settings(
        platform_url="https://platform.test",
        token="t",
        work_dir=str(tmp_path),
        llm_flavor="openai",
        llm_api_key="k",
        llm_base_url="https://gw.test",
    )
    w = wiring.build_for_image(settings)
    try:
        assert w.execution._root == os.path.abspath(str(tmp_path))
    finally:
        for adapter in (w.execution, w.llm, w.platform):
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()

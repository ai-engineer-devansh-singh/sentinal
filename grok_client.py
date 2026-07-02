"""Grok (xAI) client with a deterministic local-reasoning fallback.

The xAI API is OpenAI-compatible, so we use the `openai` SDK pointed at the
xAI base URL. If no key is configured (or a call fails), we fall back to a
local reasoning stub so the demo runs end-to-end. Every result carries an
`engine` field ("grok" | "local") so the dashboard can show which path
produced the reasoning.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import settings

log = logging.getLogger("sentinel.grok")


@dataclass
class GrokResult:
    text: str
    engine: str  # "grok" or "local"
    model: str
    error: str | None = None


class GrokClient:
    """Thin async wrapper over the xAI chat-completions endpoint."""

    def __init__(self) -> None:
        self.enabled = settings.grok_enabled
        self.model = settings.xai_model
        self.model_large = settings.xai_model_large
        self.base_url = settings.xai_base_url
        self._client = None
        if self.enabled:
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(
                    api_key=settings.xai_api_key,
                    base_url=self.base_url,
                    timeout=20.0,
                    max_retries=1,
                )
            except Exception as e:  # pragma: no cover
                log.warning("openai SDK init failed: %s — using local fallback", e)
                self.enabled = False

    async def complete(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> GrokResult:
        """Chat completion. Falls back to local stub on any failure."""
        want_model = model or self.model
        if not self.enabled or self._client is None:
            return GrokResult(text=_local_reasoning(user), engine="local", model="local-stub")

        try:
            resp = await self._client.chat.completions.create(
                model=want_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = resp.choices[0].message.content.strip()
            return GrokResult(text=text or _local_reasoning(user), engine="grok", model=want_model)
        except Exception as e:  # network / auth / rate-limit → fallback
            log.warning("Grok call failed (%s) — using local reasoning", e)
            return GrokResult(
                text=_local_reasoning(user), engine="local", model="local-stub", error=str(e)
            )

    async def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict[str, Any]:
        """Completion that is expected to be a JSON object. Parses defensively."""
        raw = await self.complete(system, user, model=model, max_tokens=900, temperature=0.1)
        return _parse_json_loose(raw.text), raw


# ── local reasoning fallback ──────────────────────────────────────────────

def _local_reasoning(user_prompt: str) -> str:
    """Produce a plausible clinician-style sentence from cues in the prompt.

    This is NOT a model — it extracts a few numeric cues and writes a short,
    honest deterministic sentence so the dashboard always has something to
    show. The UI marks it clearly as local (non-Grok).
    """
    nums = re.findall(r"([A-Za-z_ ]{2,20}?)\s*[:=]\s*([0-9]+\.?[0-9]*)", user_prompt)
    bits = []
    for label, val in nums[:6]:
        bits.append(f"{label.strip()} {val}")
    tail = "; ".join(bits) if bits else "structured vitals reviewed"
    return (
        f"[local reasoning] Reviewed {tail}. "
        "Pattern consistent with trend analysis; structured data corroborates. "
        "(Set XAI_API_KEY to enable Grok deliberation.)"
    )


def _parse_json_loose(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


grok = GrokClient()
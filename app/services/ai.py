"""Groq LLM client (OpenAI-compatible). Primary model + fallback; never raises raw provider errors to users."""
import json
import logging
import re

import httpx

from app.config import settings
from app.context import current_request_id

log = logging.getLogger("dumbbel.ai")
URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"


class AiUnavailable(Exception):
    pass


def enabled() -> bool:
    return bool(settings.groq_api_key)


def chat(messages: list[dict], *, json_mode: bool = False, max_tokens: int = 700, temperature: float = 0.4) -> str:
    """Returns the assistant text. Tries the primary model, then the fallback. Raises AiUnavailable."""
    if not settings.groq_api_key:
        raise AiUnavailable("AI isn’t configured (GROQ_API_KEY).")
    models = [settings.groq_model or DEFAULT_MODEL, settings.groq_fallback_model]
    last = None
    for model in dict.fromkeys(m for m in models if m):
        body = {"model": model, "messages": messages, "temperature": temperature,
                "max_completion_tokens": max_tokens}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post(URL, json=body, timeout=25,
                           headers={"Authorization": f"Bearer {settings.groq_api_key}"})
            data = r.json()
            if r.status_code == 200:
                text = (data["choices"][0]["message"].get("content") or "").strip()
                if text:
                    log.info("ai ok model=%s tokens=%s rid=%s", model, data.get("usage", {}).get("total_tokens"),
                             current_request_id())
                    return text
                last = "empty reply"
            else:
                last = (data.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        except Exception as e:  # network / JSON
            last = f"{type(e).__name__}: {e}"
        log.warning("ai fail model=%s: %s rid=%s", model, str(last)[:200], current_request_id())
    raise AiUnavailable(str(last))


def chat_json(messages: list[dict], **kw) -> dict:
    text = chat(messages, json_mode=True, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise AiUnavailable("AI returned invalid JSON")

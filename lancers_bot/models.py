"""The models a user may pick: OpenRouter's live list, Claude / Grok / OpenAI chat models only"""

import re
import time
from decimal import Decimal

import requests

from .proposals import OPENROUTER_URL

# OpenRouter's reasoning effort levels, highest first
EFFORTS = ["max", "xhigh", "high", "medium", "low", "minimal", "none"]
# Routing: normal = OpenRouter's default; fast = ":nitro" (highest-throughput providers, priority tiers
# allowed — can cost more); latency = providers with the quickest first token first
SPEEDS = ["normal", "fast", "latency"]

# id prefix -> group label shown in the extension
PROVIDERS = {"anthropic/": "Claude (Anthropic)", "x-ai/": "Grok (xAI)", "openai/": "OpenAI"}
CACHE_SECONDS = 3600

_cache = {"at": 0, "models": [], "error": None}


def allowed_id(model_id):
    return any((model_id or "").startswith(prefix) for prefix in PROVIDERS)


# Snapshot suffixes (-2024-11-20, -0613) and version tokens (4.1, 5, 6.1, 4o). Sizes such as 120b / 16k
# are part of the name, not a version.
DATED = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{4})$")
VERSION = re.compile(r"^(\d+(?:\.\d+)?)([a-z]?)$")


def series(model_id):
    """("openai/gpt-*-sol", Decimal("6.1"), is_snapshot) for openai/gpt-6.1-sol"""
    provider, name = model_id.split("/", 1)
    snapshot = bool(DATED.search(name))
    name = DATED.sub("", name)
    parts, version = [], None
    for token in name.split("-"):
        m = VERSION.match(token)
        if version is None and m and m.group(2) not in ("b", "k"):
            version = Decimal(m.group(1))   # decimal: grok-4.7 is newer than grok-4.20
            parts.append("*")
        else:
            parts.append(token)
    return f"{provider}/{'-'.join(parts)}", version, snapshot


def latest_per_series(models):
    """Only the highest version of each series: gpt-6-sol, not gpt-5.6-sol as well"""
    best = {}
    for m in models:
        key, version, snapshot = series(m["id"])
        rank = (version if version is not None else Decimal(0), not snapshot, m["created"])
        if key not in best or rank > best[key][0]:
            best[key] = (rank, m)
    kept = {m["id"] for _, m in best.values()}
    return [m for m in models if m["id"] in kept]


def per_million(price):
    try:
        return round(float(price) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def fetch():
    """OpenRouter's /models, reduced to the text chat models of the allowed providers, newest first"""
    resp = requests.get(f"{OPENROUTER_URL}/models", timeout=20)
    resp.raise_for_status()
    models = []
    for m in resp.json().get("data") or []:
        if not isinstance(m, dict):
            continue
        model_id = m.get("id") or ""
        provider = next((label for prefix, label in PROVIDERS.items() if model_id.startswith(prefix)), None)
        arch = m.get("architecture") or {}
        # ":batch" variants are for OpenRouter's batch API, not live requests; image generators are out too
        if not provider or ":" in model_id or arch.get("output_modalities") != ["text"]:
            continue
        expires = m.get("expiration_date")
        if expires and str(expires) < time.strftime("%Y-%m-%d"):
            continue
        pricing = m.get("pricing") or {}
        reasoning = m.get("reasoning") or {}
        models.append({
            "id": model_id,
            "name": m.get("name") or model_id,
            "provider": provider,
            "created": m.get("created") or 0,
            "context": m.get("context_length"),
            "price_in": per_million(pricing.get("prompt")),     # USD per 1M input tokens
            "price_out": per_million(pricing.get("completion")),
            # Reasoning levels this model accepts (empty: no reasoning setting), its default, and whether
            # it always reasons (then "none" is refused)
            "efforts": [e for e in reasoning.get("supported_efforts") or [] if e in EFFORTS],
            "default_effort": reasoning.get("default_effort"),
            "reasoning_mandatory": bool(reasoning.get("mandatory")),
            "max_output": (m.get("top_provider") or {}).get("max_completion_tokens"),
        })
    models.sort(key=lambda x: (list(PROVIDERS.values()).index(x["provider"]), -x["created"]))
    return latest_per_series(models)


def list_models(force=False):
    """The cached list (refreshed hourly, or now with force); a failed refresh keeps the last good list"""
    if force or not _cache["models"] or time.time() - _cache["at"] > CACHE_SECONDS:
        try:
            _cache.update(models=fetch(), at=time.time(), error=None)
        except (requests.RequestException, ValueError) as e:
            _cache["error"] = f"OpenRouter's model list could not be loaded: {e}"
    return {"models": _cache["models"], "fetched_at": int(_cache["at"]) or None, "error": _cache["error"]}


def known_ids():
    """Ids on the last loaded list (empty when it was never loaded)"""
    return {m["id"] for m in _cache["models"]}


def info(model_id):
    """The listed model's details, or None when it is not on the last loaded list"""
    return next((m for m in _cache["models"] if m["id"] == model_id), None)

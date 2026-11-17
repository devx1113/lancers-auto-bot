"""The owner's profile sent by the extension: prompt, API key (OpenRouter or OpenAI; stored encrypted),
model, filters, portfolio"""

from cryptography.fernet import Fernet, InvalidToken

from .config import DEFAULT_DAILY_LIMIT, PROFILE_SECRET
from . import models, proposals, webpages
from .filters import DEFAULT_FILTERS, merged_filters
from .proposals import DEFAULT_MODEL, PLACEHOLDER


def word_list(value):
    """A comma-separated box (or a list) into the stored words. 、 and newlines count as commas."""
    words = value
    if isinstance(words, str):
        words = words.replace("、", ",").replace("\n", ",").split(",")
    return [str(w).strip() for w in words if str(w).strip()][:100]


class ProfileError(Exception):
    pass


def fernet():
    if not PROFILE_SECRET:
        raise ProfileError("PROFILE_SECRET is not set in the server's .env (python manage.py gen-secret)")
    return Fernet(PROFILE_SECRET.encode())


def encrypt_key(api_key):
    return fernet().encrypt(api_key.encode()).decode()


def decrypt_key(api_key_enc):
    if not api_key_enc:
        return None
    try:
        return fernet().decrypt(api_key_enc.encode()).decode()
    except InvalidToken:
        raise ProfileError("the stored OpenRouter key can't be decrypted (PROFILE_SECRET changed?) — save the key again")


def key_hint(api_key):
    return f"{api_key[:6]}…{api_key[-4:]}" if len(api_key) > 12 else "…"


def daily_limit(profile):
    return profile.get("daily_limit") or DEFAULT_DAILY_LIMIT


# Settings model that writes every proposal through the user's Grok Drafter (no API call)
GROK_FREE_MODEL = "grok-free"


def setup_problem(profile):
    """Why this profile can't write proposals yet, or None"""
    if not profile.get("prompt"):
        return "サーバーにプロンプトが未設定（Settings → Your profile）"
    if PLACEHOLDER in profile["prompt"]:
        return "プロンプトの「私について」が例文のまま（Settings → Your profile）"
    # Grok Free writes in the user's browser; it does not use an API key
    if profile.get("model") != GROK_FREE_MODEL and not profile.get("api_key_enc"):
        return "サーバーにAPIキー（OpenRouter / OpenAI）が未設定（Settings → Your profile）"
    return None


def public(profile, pages=None):
    """What the extension may see: never the key itself. pages: the cached portfolio pages (store.get_pages)"""
    return {
        "prompt": profile.get("prompt") or "",
        "model": profile.get("model") or DEFAULT_MODEL,
        "filters": merged_filters(profile.get("filters")),
        "daily_limit": daily_limit(profile),
        "has_key": bool(profile.get("api_key_enc")),
        "key_hint": profile.get("key_hint"),
        "key_provider": key_provider(profile),
        "not_ready": setup_problem(profile),
        "reasoning_effort": profile.get("reasoning_effort"),     # None = the model's default
        "speed": profile.get("speed") or "normal",
        "web_search": bool(profile.get("web_search")),
        # ↻ Redraft with Grok Free (their Grok Drafter); only for users on the Grok Free list
        "use_grok_free": bool(profile.get("use_grok_free")),
        "grok_device": profile.get("grok_device") or "",       # "" = any connected device
        "portfolio": webpages.portfolio_status(profile.get("portfolio") or [], pages or {}),
    }


def public_for(store, user_id):
    profile = store.get_profile(user_id)
    user = store.user_by_id(user_id)
    return {**public(profile, store.get_pages(e["url"] for e in profile["portfolio"])),
            "grok_free_allowed": bool(user and user["grok_free"])}


def key_provider(profile):
    """openrouter / openai / None, from the stored hint (its first characters)"""
    if not profile.get("api_key_enc"):
        return None
    return "openrouter" if (profile.get("key_hint") or "").startswith("sk-or-") else "openai"


def clean_key(value):
    return "".join(str(value or "").replace("Bearer ", "").replace("bearer ", "").split()).strip("\"'`")


def update(store, user_id, payload):
    """Apply a profile.update from the extension; returns the public profile"""
    fields = {}
    if "prompt" in payload:
        prompt = str(payload["prompt"] or "").strip()
        if len(prompt) > 20000:
            raise ProfileError("the prompt is too long (max 20,000 characters)")
        fields["prompt"] = prompt or None
    if payload.get("api_key"):
        api_key = clean_key(payload["api_key"])
        if api_key.startswith("sk-ant-"):
            raise ProfileError("Anthropic keys can't be used directly — for Claude models use an OpenRouter key (sk-or-…)")
        if not api_key.startswith("sk-") or len(api_key) < 20:
            raise ProfileError("that doesn't look like an OpenRouter (sk-or-…) or OpenAI (sk-…) key")
        fields["api_key_enc"] = encrypt_key(api_key)
        fields["key_hint"] = key_hint(api_key)
    if payload.get("clear_key"):
        fields["api_key_enc"] = fields["key_hint"] = None
    if "model" in payload:
        model = str(payload["model"] or "").strip() or None
        user = store.user_by_id(user_id)
        if model == GROK_FREE_MODEL:
            if not (user and user["grok_free"]):
                raise ProfileError("Grok Free is not enabled for this user")
        elif model and not models.allowed_id(model):
            raise ProfileError("only Claude (anthropic/…), Grok (x-ai/…) and OpenAI (openai/…) models can be used")
        # A model already saved stays usable after newer versions push it off the list
        current = store.get_profile(user_id)["model"]
        if model and model != GROK_FREE_MODEL and model != current and models.known_ids() and model not in models.known_ids():
            raise ProfileError(f"{model} is not on the current model list (newest version of each series only)")
        fields["model"] = model
    if "speed" in payload:
        speed = str(payload["speed"] or "normal")
        if speed not in models.SPEEDS:
            raise ProfileError(f"speed must be one of {', '.join(models.SPEEDS)}")
        fields["speed"] = None if speed == "normal" else speed
    if "reasoning_effort" in payload or "model" in fields:
        effort = str(payload.get("reasoning_effort") or "") or None
        model = fields.get("model") or store.get_profile(user_id)["model"] or DEFAULT_MODEL
        listed = models.info(model)
        if effort and effort not in models.EFFORTS:
            raise ProfileError(f"reasoning effort must be one of {', '.join(models.EFFORTS)}")
        if effort and listed is not None and effort not in listed["efforts"]:
            raise ProfileError(f"{model} does not take reasoning effort '{effort}'"
                               + (f" (it takes: {', '.join(listed['efforts'])})" if listed["efforts"] else " (no reasoning setting)"))
        if "reasoning_effort" in payload or (effort is None and "model" in fields):
            fields["reasoning_effort"] = effort
    if "filters" in payload:
        incoming = payload["filters"] or {}
        filters = {}
        if "exclude_competition" in incoming:
            filters["exclude_competition"] = bool(incoming["exclude_competition"])
        if "min_budget" in incoming:
            filters["min_budget"] = max(0, int(incoming["min_budget"] or 0))
        if "exclude_keywords" in incoming:
            filters["exclude_keywords"] = word_list(incoming["exclude_keywords"])
        if "exclude_clients" in incoming:
            filters["exclude_clients"] = word_list(incoming["exclude_clients"])
        fields["filters"] = {k: filters.get(k, v) for k, v in merged_filters(store.get_profile(user_id)["filters"]).items()
                             if k in DEFAULT_FILTERS}
    if "portfolio" in payload:
        fields["portfolio"] = webpages.parse_entries(payload["portfolio"])
    if "web_search" in payload:
        fields["web_search"] = 1 if payload["web_search"] else None
    if "use_grok_free" in payload:
        user = store.user_by_id(user_id)
        if payload["use_grok_free"] and not (user and user["grok_free"]):
            raise ProfileError("Grok Free is not enabled for this user")
        fields["use_grok_free"] = 1 if payload["use_grok_free"] else None
    if "grok_device" in payload:
        device = str(payload["grok_device"] or "").strip()
        if device and device not in {d["device_id"] for d in store.grok_devices(user_id)}:
            raise ProfileError("that Grok Free device is not one of yours")
        fields["grok_device"] = device or None
    if "daily_limit" in payload:
        fields["daily_limit"] = max(0, min(500, int(payload["daily_limit"] or 0))) or None
    check_key_and_model(store.get_profile(user_id), fields)
    if fields:
        store.save_profile(user_id, **fields)
    return public_for(store, user_id)


def check_key_and_model(current, fields):
    """An OpenAI key calls OpenAI itself: only OpenAI models it can use, and no OpenRouter-only routing.
    Calls OpenAI (when the key or model changes), so run this off the event loop."""
    enc = fields["api_key_enc"] if "api_key_enc" in fields else current.get("api_key_enc")
    if not enc:
        return
    api_key = decrypt_key(enc)
    if proposals.provider_of(api_key) != "openai":
        return
    model = (fields["model"] if "model" in fields else current.get("model")) or DEFAULT_MODEL
    if model == GROK_FREE_MODEL:
        return    # written by the Grok Drafter, not by this key
    if not model.startswith("openai/"):
        raise ProfileError(f"with an OpenAI key only OpenAI models can be used ({model} is not one) — pick one under OpenAI, "
                           "or use an OpenRouter key for Claude / Grok")
    speed = fields["speed"] if "speed" in fields else current.get("speed")
    if speed == "latency":
        raise ProfileError("Low latency is an OpenRouter option — with an OpenAI key choose Normal or Fast (priority)")
    if "api_key_enc" in fields or "model" in fields:
        try:
            proposals.check_openai_model(api_key, model)
        except proposals.ProposalError as e:
            raise ProfileError(str(e))


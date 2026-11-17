"""Proposal text from a user's prompt + the Project Information, through OpenRouter, or straight
through OpenAI when the user's key is an OpenAI one (OpenAI models only; billed by OpenAI)"""

import os
import re

import requests

# OPENROUTER_BASE_URL: test hook (a local mock); production uses OpenRouter itself
OPENROUTER_URL = os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
OPENAI_URL = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
# Must be on the dropdown list (models.py: newest version per series)
DEFAULT_MODEL = "x-ai/grok-4.7"
# Lancers' 提案文 box takes at most 3000 characters
MAX_CHARS = 3000
# Shorter than this is not a proposal (a question back, "I'll check the site first", ...): never auto-submit it
MIN_CHARS = 300
# The extension's default prompt ships with this placeholder in its 私について section
PLACEHOLDER = "ここに自己紹介"

# Appended to every user's prompt: the output shape and what the form fills in by itself
RULES = f"""
---
出力ルール（システムが付加）:
- 出力するのはランサーズの「提案文」欄にそのまま貼る本文のみ。前置き・見出し「提案文」・コードブロック・注釈は書かない。
- {MAX_CHARS}文字以内（目安は800〜1500文字）。
- 契約金額はフォームの既定値のまま送信されるため、本文で金額を約束しない。
- メールアドレス・電話番号・SNSやチャットのIDなどの連絡先は書かない（ランサーズの規約で禁止）。URLは【過去のプロジェクト】に渡されたものだけ書いてよい。
- プロンプトと【過去のプロジェクト】にない実績や経験を作らない。
- {{web}}
- 追加の質問はできない。与えられた情報だけで、必ず完成した提案文を出力する。「確認してから書きます」などの作業予告や、情報提供のお願いだけの返答はしない。
- 情報が足りない点は断定せず、提案文の中で確認事項として触れる。"""
WEB_OFF = ("Webの閲覧はできない。依頼内容のURLと【過去のプロジェクト】のページのうち、システムが取得できたものは内容が入力に含まれている。"
           "取得できなかったページや、入力にないページの中身は推測しない。")
WEB_ON = ("依頼内容のURLと【過去のプロジェクト】のページのうち、システムが取得できたものは内容が入力に含まれている。"
          "加えてWeb検索の結果が与えられる。依頼内容やクライアントのサイトの理解に役立つ範囲で使い、確認できないことは断定しない。"
          "出典・注釈・検索結果へのリンクは本文に書かない。")


# Grok in a browser (Grok Drafter) can look things up on the web by itself
WEB_GROK = ("依頼内容のURLと【過去のプロジェクト】のページのうち、システムが取得できたものは内容が入力に含まれている。"
            "必要ならWebで確認してよいが、確認できないことは断定しない。出典・注釈・リンクは本文に書かない。")


def rules(web_search=False):
    """web_search: True (OpenRouter / OpenAI search), False, or "grok" (Grok's own browsing)"""
    return RULES.replace("{web}", WEB_GROK if web_search == "grok" else WEB_ON if web_search else WEB_OFF)


def check_prompt(prompt):
    if not prompt or PLACEHOLDER in prompt:
        raise ProposalError("プロンプトの「私について」がまだ例文のままです（Settings → Your profile）")


def grok_message(prompt, job, info, context=""):
    """Everything in one chat message, the way it is pasted into Grok by hand: prompt, rules, the job"""
    check_prompt(prompt)
    return f"{prompt.strip()}{rules('grok')}\n\n{project_block(job, info)}" + (f"\n\n{context}" if context else "")


def test_input(prompt, details, web_search=False):
    """The Test tab: the prompt with the output rules (as for real proposals), and the pasted project details"""
    if not prompt.strip() or not details.strip():
        raise ProposalError("enter both the project details and a prompt")
    return prompt.strip() + rules(web_search), details.strip()


def finish(text):
    """A model's answer as the proposal: wrapping and citation links removed, cut to MAX_CHARS"""
    text = clean(text)
    if not text:
        raise ProposalError("the model returned an empty answer")
    return fit(text)


class ProposalError(Exception):
    pass


def project_block(job, info):
    """The Project Information handed to the model"""
    budget = (info.get("budget") or {}).get("display") or job.get("price") or "-"
    lines = [
        "【依頼内容】",
        f"依頼タイトル: {info.get('title') or job.get('title')}",
        f"クライアント名: {info.get('client_name') or job.get('client_name') or '-'}",
        f"依頼方式: {info.get('work_type') or job.get('work_type') or '-'}",
        f"源泉徴収: {info.get('withholding_tax') or '-'}",
        f"依頼予算: {budget}",
        f"募集開始日時: {(info.get('recruit_start_at') or '-')[:16].replace('T', ' ')}",
        f"募集締切日時: {(info.get('recruit_end_at') or '-')[:16].replace('T', ' ')}",
        f"希望納期（完了予定日として入力されます）: {info.get('desired_delivery') or '指定なし'}",
    ]
    for label, value in (info.get("other") or {}).items():
        lines.append(f"{label}: {value}")
    lines += ["", "【詳細】", (info.get("description") or job.get("description") or "")[:8000]]
    return "\n".join(lines)


def clean(text):
    text = (text or "").strip()
    # Models sometimes wrap the answer in a code block or label it
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    text = re.sub(r"^(提案文|本文)\s*[:：]\s*\n?", "", text).strip()
    # Web search answers can carry citation links: keep the words, drop the link
    text = re.sub(r"\s*\(\[[^\]\n]+\]\(https?://[^)\s]+\)\)", "", text)       # "([site](url))" after a sentence
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1", text)
    return text


def fit(text):
    """Cut at the last paragraph (or sentence) that ends within MAX_CHARS"""
    if len(text) <= MAX_CHARS:
        return text
    head = text[:MAX_CHARS]
    cut = max(head.rfind("\n\n"), head.rfind("。"))
    return head[:cut + 1].strip() if cut > MAX_CHARS // 2 else head


# Room for the proposal itself (3000 Japanese characters is roughly 3000–4000 tokens)
ANSWER_TOKENS = 4000
# Share of max_tokens OpenRouter gives reasoning at each effort (budget-based providers); the cap is sized
# so the answer still gets ANSWER_TOKENS. Only the tokens actually used are billed.
REASONING_SHARE = {"max": 0.95, "xhigh": 0.95, "high": 0.8, "medium": 0.5, "low": 0.2, "minimal": 0.1, "none": 0.0}
DEFAULT_MAX_TOKENS = 16000     # model's own default effort (often "high")


def max_tokens_for(effort, model_limit=None):
    tokens = DEFAULT_MAX_TOKENS if effort is None else int(ANSWER_TOKENS / (1 - REASONING_SHARE[effort])) if REASONING_SHARE[effort] < 0.9 else 80000
    tokens = max(tokens, 8000)
    return min(tokens, model_limit) if model_limit else tokens


def request_body(model, messages, effort=None, speed=None, model_limit=None, web_search=False):
    body = {"model": f"{model}:nitro" if speed == "fast" else model, "messages": messages,
            "temperature": 0.7, "max_tokens": max_tokens_for(effort, model_limit)}
    if effort:
        # exclude: the reasoning itself is not sent back (it is still billed)
        body["reasoning"] = {"effort": effort, "exclude": True}
    if speed == "latency":
        body["provider"] = {"sort": "latency"}
    if web_search:
        # OpenRouter's web plugin: the provider's own search where it has one (xAI, OpenAI, Anthropic), else Exa
        body["plugins"] = [{"id": "web"}]
    return body


def provider_of(api_key):
    """Whose key: OpenRouter keys start sk-or-, OpenAI's sk- (sk-proj-, sk-svcacct- …)"""
    return "openrouter" if (api_key or "").startswith("sk-or-") else "openai"


def openai_model(model):
    """The dropdown lists OpenRouter's ids (openai/gpt-5.5); OpenAI calls the same model gpt-5.5"""
    return model.split("/", 1)[1] if model.startswith("openai/") else model


def openai_body(model, system, user, effort=None, speed=None, model_limit=None, web_search=False, reasoning_model=True):
    """OpenAI's Responses API: reasoning effort, web search as a tool, the priority tier for Fast"""
    body = {"model": openai_model(model), "instructions": system, "input": user,
            "max_output_tokens": max_tokens_for(effort, model_limit), "store": False}
    if effort:
        body["reasoning"] = {"effort": effort}
    if not reasoning_model:
        body["temperature"] = 0.7      # reasoning models take only their default
    if speed == "fast":
        body["service_tier"] = "priority"
    if web_search:
        body["tools"] = [{"type": "web_search"}]
    return body


def openai_error(resp):
    try:
        err = resp.json().get("error", {}).get("message")
    except ValueError:
        err = None
    return f"OpenAI {resp.status_code}: {err or resp.text[:200]}"


def openai_chat(api_key, body, timeout=300):
    resp = requests.post(f"{OPENAI_URL}/responses", json=body, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code != 200:
        raise ProposalError(openai_error(resp))
    data = resp.json()
    text = "".join(c.get("text") or "" for item in data.get("output") or [] if item.get("type") == "message"
                   for c in item.get("content") or [] if c.get("type") == "output_text")
    if not text.strip() and (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
        raise ProposalError("reasoning used up the whole token budget before the answer — choose a lower reasoning effort")
    return text, f"openai/{data.get('model') or openai_model(body['model'])}", (data.get("usage") or {}).get("total_tokens")


def check_openai_model(api_key, model):
    """Raise ProposalError unless this OpenAI key can use the model"""
    resp = requests.get(f"{OPENAI_URL}/models/{openai_model(model)}", headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
    if resp.status_code == 404:
        raise ProposalError(f"{openai_model(model)} is not available with this OpenAI key — pick another OpenAI model")
    if resp.status_code != 200:
        raise ProposalError(openai_error(resp))


def chat(api_key, model, messages, effort=None, speed=None, model_limit=None, web_search=False, timeout=300):
    resp = requests.post(f"{OPENROUTER_URL}/chat/completions", timeout=timeout,
                         json=request_body(model, messages, effort, speed, model_limit, web_search),
                         headers={"Authorization": f"Bearer {api_key}", "X-Title": "Lancers proposal bot"})
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {}).get("message")
        except ValueError:
            err = None
        raise ProposalError(f"OpenRouter {resp.status_code}: {err or resp.text[:200]}")
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content")
    if not (text or "").strip() and choice.get("finish_reason") == "length":
        raise ProposalError("reasoning used up the whole token budget before the answer — choose a lower reasoning effort")
    return text, data.get("model") or model, (data.get("usage") or {}).get("total_tokens")


def generate(api_key, model, prompt, job, info, effort=None, speed=None, listed=None, web_search=False, context=""):
    """Returns (message, model, tokens); raises ProposalError. listed: the model's entry on the model
    list (models.info: token limit, reasoning levels); context: more input after the Project
    Information (the job's linked pages and the user's portfolio, see webpages.py)"""
    check_prompt(prompt)
    return generate_text(api_key, model, prompt.strip() + rules(web_search),
                         project_block(job, info) + (f"\n\n{context}" if context else ""), effort, speed, listed, web_search)


def generate_text(api_key, model, system, user, effort=None, speed=None, listed=None, web_search=False):
    """One answer for a system prompt + a user message (a real proposal, or the Test tab's)"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    model = model or DEFAULT_MODEL
    listed = listed or {}
    model_limit = listed.get("max_output")
    last_error = None
    for _ in range(2):   # one retry: an empty or failed answer is usually transient
        try:
            if provider_of(api_key) == "openai":
                body = openai_body(model, messages[0]["content"], messages[1]["content"], effort, speed, model_limit, web_search,
                                   reasoning_model=bool(listed.get("efforts")) or not listed)
                text, used, tokens = openai_chat(api_key, body)
            else:
                text, used, tokens = chat(api_key, model, messages, effort, speed, model_limit, web_search)
        except (requests.RequestException, ProposalError) as e:
            last_error = e
            continue
        text = clean(text)
        if text:
            return fit(text), used, tokens
        last_error = ProposalError("the model returned an empty answer")
    raise ProposalError(str(last_error))


def test_key(api_key):
    """Check an OpenRouter or OpenAI key without spending tokens; returns a short description"""
    if provider_of(api_key) == "openai":
        resp = requests.get(f"{OPENAI_URL}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
        if resp.status_code != 200:
            raise ProposalError(f"OpenAI rejected the key ({resp.status_code})")
        return "OpenAI key OK · billed by OpenAI (OpenAI models only)"
    resp = requests.get(f"{OPENROUTER_URL}/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
    if resp.status_code == 404:
        resp = requests.get(f"{OPENROUTER_URL}/auth/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
    if resp.status_code != 200:
        raise ProposalError(f"OpenRouter rejected the key ({resp.status_code})")
    data = resp.json().get("data") or {}
    limit = data.get("limit_remaining", data.get("limit"))
    return f"key OK{f' · {limit} credits left' if limit is not None else ''}"

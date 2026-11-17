"""Which jobs a user bids on. Each rule returns a reason (Japanese, shown in the extension) or None.

To add a criterion: write a rule(job, info, filters) and append it to RULES; give it a default
in DEFAULT_FILTERS if it is configurable per user.
"""

import re
import time
import unicodedata
from datetime import datetime

# Per-user settings (profiles.filters); missing keys fall back to these
DEFAULT_FILTERS = {
    "exclude_competition": True,   # コンペ = 選定方式: the client picks a winner from submitted designs
    "min_budget": 0,               # yen; jobs whose upper budget is below this are skipped (0 = off)
    "exclude_keywords": [],        # skipped when the title, tags or description contains any of these
    "exclude_clients": [],         # skipped when the client's name (or Lancers id) contains any of these
}
# Typed around a keyword (as in the title's 【初心者大歓迎】) but not part of what is matched
KEYWORD_BRACKETS = "[]【】「」『』()（）<>＜＞〈〉《》〔〕{}［］"


def merged_filters(filters):
    return {**DEFAULT_FILTERS, **(filters or {})}


def work_type(job, info):
    return (info or {}).get("work_type") or job.get("work_type")


def rule_competition(job, info, filters):
    if filters["exclude_competition"] and work_type(job, info) == "コンペ":
        return "コンペ（選定方式）の依頼は対象外"


def rule_closed(job, info, filters):
    end = (info or {}).get("recruit_end_at")
    if end and datetime.fromisoformat(end).timestamp() < time.time():
        return "募集期間が終了しています"


def rule_min_budget(job, info, filters):
    budget = ((info or {}).get("budget") or {}).get("max")
    if filters["min_budget"] and budget is not None and budget < filters["min_budget"]:
        return f"予算が下限（{filters['min_budget']:,}円）未満"


def normalize(text):
    """Full-width / half-width and letter case don't matter (ＰＨＰ = php)"""
    return unicodedata.normalize("NFKC", text or "").casefold()


def keyword(word):
    return normalize(word).strip().strip(KEYWORD_BRACKETS).strip()


def rule_keywords(job, info, filters):
    # The search card's description starts with the job's tags (初心者OK, 急募 …); the proposal page has the full text
    text = normalize("\n".join([job.get("title") or "", (info or {}).get("title") or "", job.get("description") or "",
                                 (info or {}).get("description") or ""]))
    for word in filters["exclude_keywords"]:
        if keyword(word) and keyword(word) in text:
            return f"除外キーワード「{word}」を含む"


def rule_clients(job, info, filters):
    # The proposal page's クライアント名, the search card's name, and the id in /client/<id>
    client_id = re.search(r"/client/([^/?#]+)", job.get("client_url") or "")
    text = normalize("\n".join([(info or {}).get("client_name") or "", job.get("client_name") or "",
                                 client_id.group(1) if client_id else ""]))
    for name in filters["exclude_clients"]:
        if keyword(name) and keyword(name) in text:
            return f"除外クライアント「{name}」"


RULES = [rule_competition, rule_closed, rule_min_budget, rule_keywords, rule_clients]


def evaluate(job, info, filters, blocked):
    """The bid decision for one user: {"allowed", "blocked", "reasons"}"""
    filters = merged_filters(filters)
    reasons = []
    if blocked:
        reasons.append("ブロックリストのクライアント")
    for rule in RULES:
        reason = rule(job, info, filters)
        if reason:
            reasons.append(reason)
    return {"allowed": not reasons, "blocked": bool(blocked), "reasons": reasons}

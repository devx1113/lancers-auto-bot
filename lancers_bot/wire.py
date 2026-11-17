"""Stored jobs -> the job objects the Lancers Auto-Proposal extension expects in its events (one per user)"""

import json
import re
import time
from datetime import datetime

from .details import propose_url

CATEGORIES = [
    {"key": "system", "label": "システム開発・運用"},
    {"key": "web", "label": "Web制作・Webデザイン"},
]
CATEGORY_LABELS = {c["key"]: c["label"] for c in CATEGORIES}


def client_id_of(url):
    """Lancers client id: the user name in /client/<name>"""
    m = re.search(r"/client/([^/?#]+)", url or "")
    return m.group(1) if m else None


def epoch(iso):
    return int(datetime.fromisoformat(iso).timestamp()) if iso else None


def bid_state(details, proposal):
    """job.bid for the extension: may this user bid, and why not"""
    if details and details.get("closed"):
        return {"allowed": False, "blocked": False, "reasons": [details["closed"]]}
    if proposal is None:
        if details and details["status"] == "skipped":
            return {"allowed": False, "blocked": False, "reasons": ["フィルターの対象外"]}
        # The pipeline hasn't reached this user yet
        return {"allowed": False, "blocked": False, "pending": True, "reasons": ["依頼内容と提案文を準備中…"]}
    if proposal["status"] == "ready":
        return {"allowed": True, "blocked": False, "reasons": []}
    return {"allowed": False, "blocked": bool(proposal["blocked"]), "reasons": proposal["reasons"]}


def to_wire(job_id, data, first_seen_ts, details=None, proposal=None, submitted=None):
    job = json.loads(data)
    info = (details or {}).get("info") or {}
    category = job.get('category')
    budget = info.get("budget") or {}
    end_ts = epoch(info.get("recruit_end_at"))
    wire = {
        "site": "lancers",
        "id": job_id,
        "key": f"lancers:{job_id}",
        "url": job['url'],
        "apply_url": propose_url(job_id),
        "title": info.get("title") or job.get('title'),
        "channel": category,
        "category": {"parent_name": CATEGORY_LABELS.get(category, category), "child_name": job.get('job_category')},
        "work_type": info.get("work_type") or job.get('work_type'),
        "industry": job.get('industry'),
        "budget": {"display": budget.get("display") or job.get('price'), "min": budget.get("min"), "max": budget.get("max")},
        "client": {
            "id": client_id_of(job.get('client_url')),
            "name": info.get("client_name") or job.get('client_name'),
            "url": job.get('client_url'),
            "avatar": job.get('client_avatar'),
        },
        "content": info.get("description") or job.get('description') or "",
        # 募集開始日時 when the proposal page was read, else when the scraper first saw the job
        "created_ts": epoch(info.get("recruit_start_at")) or first_seen_ts,
        "deadline_ts": end_ts,
        "deadline_at": info.get("recruit_end_at"),
        # 希望納期: the extension enters it as 完了予定日
        "delivery_at": f"{info['desired_delivery']}T00:00:00+09:00" if info.get("desired_delivery") else None,
        "detail_fetched": bool(info),
        "extra": {
            "withholding_tax": info.get("withholding_tax"),
            "recruit_start_at": info.get("recruit_start_at"),
            "recruit_end_at": info.get("recruit_end_at"),
            "nda_required": info.get("nda_required"),
            "proposals_left": info.get("proposals_left"),
            **(info.get("other") or {}),
        },
        "status": {"open": not (details or {}).get("closed") and (not end_ts or end_ts > time.time()),
                   "closed_reason": (details or {}).get("closed")},
        # Proposals for this job are written by the server; the extension never drafts them itself
        "proposal_source": "server",
        "bid": bid_state(details, proposal),
        "proposal": None,
        # This user already submitted a proposal for this job ({at, step}): never offered again
        "submitted": submitted,
    }
    if proposal and proposal["status"] == "ready":
        wire["proposal"] = {
            "id": proposal["id"],
            # Changes whenever the proposal is rewritten (the row id stays the same)
            "version": f"{proposal['id']}-{proposal['created_ts']}",
            "message": proposal["message"],
            "delivery_date": proposal["delivery_date"],
            "auto_submit": bool(proposal["auto_submit"]),
            "hold_reasons": proposal["reasons"],
            "model": proposal["model"],
            "created_ts": proposal["created_ts"],
        }
    return wire

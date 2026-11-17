"""Project Information from the logged-in proposal page (/work/propose_start/<id>).

The page's 依頼内容 box is one <dl> of label/value pairs (依頼タイトル, クライアント名, 源泉徴収,
依頼予算, 募集開始日時, 募集締切日時, 希望納期 for projects, 詳細). Logged-out visitors are
redirected to /user/login, so the page is fetched through a user's browser (the extension relay).
"""

import re

from bs4 import BeautifulSoup


def propose_url(job_id):
    return f"https://www.lancers.jp/work/propose_start/{job_id}?proposeReferer="


# <dt> label -> ProjectInfo field
LABELS = {
    "依頼タイトル": "title",
    "クライアント名": "client_name",
    "源泉徴収": "withholding_tax",
    "依頼予算": "budget",
    "募集開始日時": "recruit_start_at",
    "募集締切日時": "recruit_end_at",
    "希望納期": "desired_delivery",
    "納品希望日": "desired_delivery",
    "詳細": "description",
}
# Line break marker swapped in for <br> before parsing (\s matches it, so norm() turns it into a space)
BR = "\x1e"
# Without these the proposal can't be written (or bid) sensibly
REQUIRED = ("title", "budget", "recruit_end_at", "description")


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip()


# What Lancers shows instead of the form when nobody can propose any more (the client left, the job was
# withdrawn …): 提案できません and its reason
CANNOT_PROPOSE = re.compile(r"提案できません\s*([^。]{0,120}。)?")


def cannot_propose(text):
    """"提案できません: <Lancers' reason>" if the text says so, else None"""
    m = CANNOT_PROPOSE.search(norm(text))
    return f"提案できません: {m.group(1)}" if m and m.group(1) else ("提案できません" if m else None)


def jst_datetime(text):
    """"2026年10月30日 11:06" -> "2026-10-30T11:06:00+09:00" (None when there is no date)"""
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?", text or "")
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}T{int(h or 0):02d}:{int(mi or 0):02d}:00+09:00"


def jst_date(text):
    """"2027年12月31日" -> "2027-12-31" """
    dt = jst_datetime(text)
    return dt[:10] if dt else None


def parse_budget(text):
    """"300,000 円 ~ 500,000 円" -> display, min, max (yen)"""
    display = re.sub(r"\s+円", "円", norm(text))
    amounts = [int(a.replace(",", "")) for a in re.findall(r"\d[\d,]*(?=\s*円)", text or "")]
    return {
        "display": display or None,
        "min": amounts[0] if amounts else None,
        "max": amounts[-1] if amounts else None,
    }


def description_text(dd):
    """The full 詳細 text: Lancers renders it twice (a preview and the whole), with </br> line breaks"""
    whole = dd.select_one("#workDesctiptionWhole, #workDescriptionWhole") or dd
    for button in whole.select("button"):
        button.decompose()
    # Only the <br>s are line breaks; the source's own newlines are indentation
    lines = [norm(line) for line in whole.get_text().split(BR)]
    text = "\n".join(lines)
    # The source escapes a few <span> tags into the text itself
    text = re.sub(r"</?span>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_propose_page(html, final_url=""):
    """Parse the proposal page. Returns {"status": ok|login|unavailable, "info": {...}, "error": str|None}"""
    if "/user/login" in (final_url or ""):
        return {"status": "login", "info": None, "error": "the page redirected to the login form"}
    # Lancers writes </br> where it means a line break
    soup = BeautifulSoup(re.sub(r"</?br\s*/?>", BR, html), "html.parser")
    box = soup.select_one(".p-work-propose__work-content dl")
    if not box:
        if soup.select_one('form[action*="/user/login"], #UserLoginForm'):
            return {"status": "login", "info": None, "error": "the page shows the login form"}
        text = soup.select_one("main, body").get_text(" ") if soup.select_one("main, body") else ""
        if cannot_propose(text):
            return {"status": "unavailable", "info": None, "error": cannot_propose(text)}
        return {"status": "unavailable", "info": None, "error": f"no 依頼内容 on the page: {norm(text)[:160]}"}

    info = {}
    for dt in box.find_all("dt", recursive=False):
        dd = dt.find_next_sibling("dd")
        label = norm(dt.get_text())
        field = LABELS.get(label)
        if not dd:
            continue
        if field == "description":
            info["description"] = description_text(dd)
        elif field == "budget":
            info["budget"] = parse_budget(dd.get_text(" "))
        elif field in ("recruit_start_at", "recruit_end_at"):
            info[field] = jst_datetime(dd.get_text())
        elif field == "desired_delivery":
            info[field] = jst_date(dd.get_text())
        elif field == "withholding_tax":
            text = norm(dd.get_text())
            info["withholding_tax"] = text
            info["withholding"] = False if "しない" in text else True if "する" in text else None
        elif field:
            info[field] = norm(dd.get_text())
        else:
            # Anything else in the box (検収期間, 業務委託に関する法律への準拠, ...) is kept as is
            info.setdefault("other", {})[label] = norm(dd.get_text())

    intro = norm(soup.select_one(".p-work-propose > main > p.c-text, main p.c-text").get_text()) if soup.select_one("main p.c-text") else ""
    info["work_type"] = ("コンペ" if "コンペ方式" in intro else "タスク" if "タスク方式" in intro
                         else "プロジェクト" if "プロジェクト方式" in intro or "計画・期間・金額" in intro else None)

    form = soup.select_one("#ProposalProposeForm") or soup
    agree = form.select_one("#ProposalIsAgreement, .js-work-contract-agree")
    info["nda_required"] = bool(agree) or "秘密保持契約書" in form.get_text()
    # コンペ: 添付ファイル 必須 — a design has to be attached, which can't be automated
    info["attachments_required"] = any("添付ファイル" in t.get_text() and "必須" in t.get_text()
                                       for t in form.select("h3.c-form__title"))
    remain = re.search(r"あと\s*(\d+)\s*回まで提案できます", norm(form.get_text(" ")))
    info["proposals_left"] = int(remain.group(1)) if remain else None
    info["missing"] = [f for f in REQUIRED if not info.get(f)]
    return {"status": "ok", "info": info, "error": None}

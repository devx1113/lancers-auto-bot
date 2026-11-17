"""End-to-end run of the server with a fake extension and a mock OpenRouter, on a throwaway database.

    python tests/e2e_server.py <scratch dir>

Nothing reaches Lancers, OpenRouter or Slack: the fake extension answers the relay's page fetches
with the saved fixtures, and OpenRouter is a local HTTP server.
"""

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
SCRATCH = sys.argv[1]
DB = os.path.join(SCRATCH, "e2e.db")
SERVER_PORT, MOCK_PORT = 8799, 8798

for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
# The extension folder the server takes the current version (and the update download) from
EXT_DIR = os.path.join(SCRATCH, "e2e-extension-src")
EXT_VERSION = "1.5.0"
os.makedirs(os.path.join(EXT_DIR, "icons"), exist_ok=True)
for name, text in (("manifest.json", json.dumps({"name": "test", "version": EXT_VERSION})), ("background.js", "// test\n"),
                   ("icons/icon16.png", "png"), (".hidden", "not shipped")):
    with open(os.path.join(EXT_DIR, name), "w") as f:
        f.write(text)
os.environ.update({"LANCERS_DB": DB, "SERVER_PORT": str(SERVER_PORT), "OPENROUTER_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
                   # keep the test away from the real channels
                   "SLACK_WEBHOOK_BIDS": "", "SLACK_WEBHOOK_ALERTS": "",
                   # the portfolio pages below are served from 127.0.0.1
                   "PAGE_FETCH_ALLOW_PRIVATE": "1",
                   "EXTENSION_DIR": EXT_DIR,
                   # users with an OpenAI key: OpenAI itself is mocked under /openai
                   "OPENAI_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}/openai",
                   # a throwaway key for the API keys saved during the test (not the server's .env)
                   "PROFILE_SECRET": base64.urlsafe_b64encode(os.urandom(32)).decode(),
                   })
sys.path.insert(0, ROOT)

import websockets  # noqa: E402

from lancers_bot.store import Store  # noqa: E402

PASSWORD = "e2e-Owner-Passw0rd!"
NEW_PASSWORD = "e2e-Changed-Passw0rd?"
PROMPT = "あなたはWeb系エンジニアです。【私について】Python/Django歴5年。丁寧に提案してください。"
PROPOSAL = "はじめまして。要件定義の経験を活かしてご提案します。\n\n" + "業務フローを整理し、決めるべき点を早めに洗い出します。" * 12
SHORT_ANSWER = "公開サイトを確認してから提案文を書きます。"


class MockOpenRouter(BaseHTTPRequestHandler):
    """OpenRouter, and OpenAI's own API under /openai"""
    calls = []
    openai_calls = []

    def reply_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/openai/responses":
            MockOpenRouter.openai_calls.append(body)
            return self.reply_json(200, {"model": body["model"], "status": "completed", "usage": {"total_tokens": 456}, "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": PROPOSAL + " ([example.com](https://example.com/))"}]}]})
        MockOpenRouter.calls.append(body)
        # The job without 希望納期 gets a non-answer, like a model asking for more information
        content = SHORT_ANSWER if "指定なし" in body["messages"][1]["content"] else f"```\n{PROPOSAL}\n```"
        reply = {"model": body["model"], "usage": {"total_tokens": 321}, "choices": [{"message": {"content": content}}]}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(reply, ensure_ascii=False).encode())

    def do_GET(self):
        if self.path.startswith("/openai/"):           # OpenAI: the key check and the models this key can use
            if self.headers.get("Authorization") != "Bearer sk-proj-owner0123456789abcdef":
                return self.reply_json(401, {"error": {"message": "Incorrect API key provided"}})
            if self.path == "/openai/models":
                return self.reply_json(200, {"data": [{"id": "gpt-5.4-mini"}]})
            found = self.path == "/openai/models/gpt-5.4-mini"
            return self.reply_json(200 if found else 404, {"id": "gpt-5.4-mini"} if found else {"error": {"message": "The model does not exist"}})
        if self.path == "/client/site":                # the site a client links in a job
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<html><head><title>現行の受発注システム</title></head><body><p>Access製の受発注管理画面</p></body></html>".encode())
            return
        if self.path.startswith("/portfolio/"):        # a user's past-project site
            found = self.path == "/portfolio/shop"
            self.send_response(200 if found else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if found:
                self.wfile.write("<html><head><title>ECショップ構築事例</title></head><body><script>x()</script>"
                                 "<p>Stripe決済と在庫連携を実装したECサイト</p></body></html>".encode())
            return
        self.send_response(200)
        self.end_headers()
        if self.path.endswith("/models"):
            text = {"output_modalities": ["text"]}
            models = [{"id": "openai/gpt-4.1-mini", "name": "OpenAI: GPT-4.1 Mini", "created": 2, "architecture": text, "pricing": {"prompt": "0.0000004", "completion": "0.0000016"}},
                      {"id": "openai/gpt-5.4-mini", "name": "OpenAI: GPT-5.4 Mini", "created": 6, "architecture": text,
                       "reasoning": {"mandatory": False, "supported_efforts": ["xhigh", "high", "medium", "low", "none"], "default_effort": "medium"},
                       "top_provider": {"max_completion_tokens": 128000}},
                      {"id": "anthropic/claude-haiku-5.5", "name": "Anthropic: Claude Haiku 5.5", "created": 7, "architecture": text,
                       "reasoning": {"mandatory": False, "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"], "default_effort": "medium"}},
                      {"id": "openai/gpt-4o-2024-11-20", "name": "OpenAI: GPT-4o (2024-11-20)", "created": 1, "architecture": text},
                      {"id": "anthropic/claude-opus-5.5", "name": "Anthropic: Claude Opus 5.5", "created": 3, "architecture": text, "pricing": {"prompt": "0.000004", "completion": "0.00002"}},
                      {"id": "anthropic/claude-opus-5.5:batch", "name": "batch", "created": 3, "architecture": text},
                      {"id": "x-ai/grok-4.7", "name": "xAI: Grok 4.7", "created": 1, "architecture": text,
                       "reasoning": {"mandatory": True, "supported_efforts": ["xhigh", "high", "medium", "low"], "default_effort": "high"}},
                      {"id": "openai/gpt-5.4-image-2", "name": "image", "created": 4, "architecture": {"output_modalities": ["image", "text"]}},
                      {"id": "google/gemini-3", "name": "Gemini", "created": 5, "architecture": text}]
            self.wfile.write(json.dumps({"data": models}).encode())
        else:
            self.wfile.write(b'{"data": {"limit_remaining": 4.2}}')

    def log_message(self, *args):
        pass


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# The saved page's dates moved relative to today, so the job is always still open when the test runs
_deadline = datetime.now() + timedelta(days=30)
DELIVERY = (datetime.now() + timedelta(days=400)).strftime("%Y-%m-%d")
DEADLINE_ISO = _deadline.strftime("%Y-%m-%dT15:04:00+09:00")


def project_page():
    y, m, d = DELIVERY.split("-")
    return (fixture("propose_start_project_nda.html")
            .replace("2026年11月06日 15:04", _deadline.strftime("%Y年%m月%d日 15:04"))
            .replace("2027年12月31日", f"{y}年{m}月{d}日"))


PAGES = {
    # The client links their current site in the 依頼内容
    5614153: project_page().replace("【概要】", f"【概要】現行サイト: http://127.0.0.1:{MOCK_PORT}/client/site を参照"),
    5622772: fixture("propose_start_competition.html"),
    # A project without 希望納期 -> proposal written but held for manual submission
    5600001: project_page().replace("<dt class=\"c-definition-list__term\">希望納期</dt>", "")
                           .replace(f"<dd class=\"c-definition-list__description\">{DELIVERY[:4]}年{DELIVERY[5:7]}月{DELIVERY[8:]}日</dd>", ""),
}
CARDS = [
    {"id": 5614153, "work_type": "プロジェクト", "client_url": "https://www.lancers.jp/client/vcvcvc", "client_name": "vcvcvc"},
    {"id": 5622772, "work_type": "コンペ", "client_url": "https://www.lancers.jp/client/isupport", "client_name": "isupport"},
    {"id": 5600001, "work_type": "プロジェクト", "client_url": "https://www.lancers.jp/client/other", "client_name": "other"},
]


def card(c):
    return {"url": f"https://www.lancers.jp/work/detail/{c['id']}", "title": f"card {c['id']}", "category": "system",
            "price": "1円", "pr_job": None, "description": "card text", **c}


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


async def extension(token, caps, inbox, ready):
    """A fake extension: answers relay fetches with the fixtures, collects everything else"""
    url = f"ws://127.0.0.1:{SERVER_PORT}/ws?token={token}&since=0&client=fake&caps={caps}&ver={EXT_VERSION}"
    async with websockets.connect(url) as ws:
        ready.set_result(ws)
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "fetch":
                job_id = int(msg["url"].split("/propose_start/")[1].split("?")[0])
                inbox.append({"type": "fetched", "url": msg["url"], "credentials": msg["credentials"]})
                await ws.send(json.dumps({"type": "fetch-result", "id": msg["id"], "ok": True, "status": 200,
                                          "url": msg["url"], "html": PAGES[job_id]}))
            else:
                inbox.append(msg)


def ws_url(password, extra=""):
    return f"ws://127.0.0.1:{SERVER_PORT}/ws?token={urllib.parse.quote(password)}&since=0&client=probe&ver={EXT_VERSION}{extra}"


async def refused(url, address=None):
    """The close frame when the server refuses the connection, or None when it lets it in"""
    headers = {"X-Forwarded-For": address} if address else None
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            await asyncio.wait_for(ws.recv(), 10)
        return None
    except websockets.ConnectionClosed as e:
        return e.rcvd


async def wait_closed(ws, timeout=12):
    end = time.time() + timeout
    while ws.close_code is None and time.time() < end:
        await asyncio.sleep(0.2)
    return ws.close_code, ws.close_reason


async def wait_for(inbox, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        for m in inbox:
            if pred(m):
                return m
        await asyncio.sleep(0.1)
    return None


def job_event(job_id, pred=lambda job: True):
    return lambda m: m["type"] == "event" and m["job"]["id"] == job_id and pred(m["job"])


async def main():
    store = Store(DB)

    server = subprocess.Popen([sys.executable, os.path.join(ROOT, "run_server.py")], cwd=ROOT,
                              stdout=open(os.path.join(SCRATCH, "e2e-server.log"), "w"), stderr=subprocess.STDOUT)
    try:
        await asyncio.sleep(2.5)
        # ---- Password: the only way in
        closed = await refused(ws_url("anything at all"))
        check(closed and closed.code == 4401 and "no password" in closed.reason, "before set-password every connection is refused")
        for weak in ("short1!", "aaaaaaaaaaaaaaaa"):
            try:
                store.set_password(weak)
                check(False, f"a weak password is refused ({weak})")
            except ValueError as e:
                check(True, f"a weak password is refused ({e})")
        check(store.set_password(PASSWORD) == "owner" and store.set_grok_free(True), "the password is set; the owner is created")
        check(PASSWORD not in json.dumps([dict(r) for r in store._all("SELECT * FROM settings")]), "only a hash of the password is stored")
        for wrong, what in (("", "no password"), ("e2e-owner-passw0rd!", "a wrong password (case differs)")):
            closed = await refused(ws_url(wrong))
            check(closed and closed.code == 4401 and closed.reason == "wrong password", f"{what} is refused with 4401")
        check(await refused(ws_url("", f"&password={urllib.parse.quote(PASSWORD)}")) is None, "?password= works as well as ?token=")
        attacker = "198.51.100.7"
        for _ in range(5):
            await refused(ws_url("guess-guess-guess"), attacker)
        closed = await refused(ws_url(PASSWORD), attacker)
        check(closed and closed.code == 4401 and "too many wrong passwords" in closed.reason,
              "after 5 wrong passwords an address is locked out, even with the right one")
        check(await refused(ws_url(PASSWORD), "203.0.113.50") is None, "...while other addresses still get in")

        alice = PASSWORD
        a_inbox = []
        a_ready = asyncio.get_running_loop().create_future()
        tasks = [asyncio.create_task(extension(alice, "fetch", a_inbox, a_ready))]
        a_ws = await a_ready
        hello = await wait_for(a_inbox, lambda m: m["type"] == "hello")
        check(hello and "server-proposals" in hello["features"] and hello["profile"]["not_ready"], "hello carries features and an unset profile")

        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "r1",
                                    "profile": {"prompt": PROMPT, "api_key": "Bearer sk-or-v1-0123456789abcdef", "model": "openai/gpt-5.4-mini"}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "r1")
        check(reply and reply["ok"] and not reply["profile"]["not_ready"] and reply["profile"]["key_hint"] == "sk-or-…cdef",
              "profile saved (key stored encrypted, only a hint comes back)")
        check("0123456789abcdef" not in str(Store(DB).get_profile(1)["api_key_enc"]), "the key is not stored in plain text")
        await a_ws.send(json.dumps({"type": "profile.test-key", "req_id": "r2"}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "r2")
        check(reply and reply["ok"] and "4.2" in reply["info"], "test-key uses the stored key")

        await a_ws.send(json.dumps({"type": "models.list", "req_id": "m1"}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "m1")
        ids = [m["id"] for m in (reply or {}).get("models", [])]
        check(reply and reply["ok"] and ids == ["anthropic/claude-haiku-5.5", "anthropic/claude-opus-5.5", "x-ai/grok-4.7", "openai/gpt-5.4-mini", "openai/gpt-4o-2024-11-20"],
              f"models.list: Claude / Grok / OpenAI text models, newest version per series, no :batch ({ids})")
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "m2", "profile": {"model": "google/gemini-3"}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "m2")
        check(reply and not reply["ok"] and "Claude" in reply["error"], "a model from another provider is refused")
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "m3", "profile": {"model": "anthropic/claude-opus-9"}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "m3")
        check(reply and not reply["ok"] and "not on the current model list" in reply["error"], "a model OpenRouter doesn't list is refused")
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "m4", "profile": {"model": "x-ai/grok-4.7", "reasoning_effort": "max"}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "m4")
        check(reply and not reply["ok"] and "does not take reasoning effort 'max'" in reply["error"], "an effort the model doesn't take is refused")
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "m5", "profile": {"model": "openai/gpt-5.4-mini", "reasoning_effort": "high", "speed": "latency"}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "m5")
        check(reply and reply["ok"] and reply["profile"]["reasoning_effort"] == "high" and reply["profile"]["speed"] == "latency",
              "reasoning effort and speed saved with the model")

        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "p1", "profile": {"portfolio": "説明だけ"}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "p1")
        check(reply and not reply["ok"] and "URL" in reply["error"], "a portfolio line without a URL is refused")
        site = f"http://127.0.0.1:{MOCK_PORT}/portfolio"
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "p2", "profile": {
            "web_search": True, "portfolio": f"{site}/shop  ECサイトの決済連携を担当\n予約システム {site}/missing"}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "p2")
        check(reply and reply["ok"] and reply["profile"]["web_search"]
              and [(p["note"], p["status"]) for p in reply["profile"]["portfolio"]] == [("ECサイトの決済連携を担当", "reading"), ("予約システム", "reading")],
              "portfolio (URL + note per line) and web search saved")
        pushed = await wait_for(a_inbox, lambda m: m["type"] == "profile" and not m.get("req_id")
                                and all(p["status"] != "reading" for p in m["profile"]["portfolio"]))
        pages = (pushed or {}).get("profile", {}).get("portfolio", [])
        check([p["status"] for p in pages] == ["ok", "error"] and pages[0]["title"] == "ECショップ構築事例" and "404" in pages[1]["error"],
              "the server reads the portfolio pages and reports each one to the extension")

        store.add_jobs([card(c) for c in CARDS])

        new = await wait_for(a_inbox, job_event(5614153, lambda j: j["bid"].get("pending")))
        check(bool(new), "the new job arrives at once, marked pending")
        ready = await wait_for(a_inbox, job_event(5614153, lambda j: j["proposal"]))
        check(bool(ready), "the proposal follows as an update")
        job = ready["job"]
        check(job["bid"]["allowed"] and job["proposal"]["auto_submit"] and job["proposal"]["delivery_date"] == DELIVERY,
              f"project with 希望納期: allowed, auto-submit, 完了予定日 = {DELIVERY}")
        preps = [m for m in a_inbox if m.get("type") == "bid-prepare" and m.get("key") == "lancers:5614153"]
        check(bool(preps) and preps[0].get("delivery_date") == DELIVERY and "propose_start" in (preps[0].get("apply_url") or "")
              and a_inbox.index(preps[0]) < a_inbox.index(ready),
              "the proposal form is opened (date known) before the text is ready")
        check(not any(m.get("type") == "bid-prepare" and str(m.get("key", "")).endswith("5600001") for m in a_inbox),
              "no early form when the job has no 希望納期")
        check(job["proposal"]["message"] == PROPOSAL, "code fences stripped from the model's answer")
        check(job["title"].startswith("要件定義") and job["client"]["name"] == "vcvcvc / vc" and job["extra"]["withholding_tax"] == "源泉徴収をしない"
              and job["deadline_at"] == DEADLINE_ISO and job["created_ts"] > 0,
              "Project Information (title, client, 源泉徴収, 募集開始/締切) in the job")
        fetched = [m for m in a_inbox if m["type"] == "fetched"]
        check(all(m["credentials"] == "include" for m in fetched) and all("propose_start" in m["url"] for m in fetched),
              "relay fetches the proposal page with the browser's cookies")
        check(not any(m["url"].endswith("5622772?proposeReferer=") for m in fetched), "the コンペ job is never fetched")

        held = await wait_for(a_inbox, job_event(5600001, lambda j: j["proposal"]))
        check(held and held["job"]["bid"]["allowed"] and not held["job"]["proposal"]["auto_submit"]
              and "希望納期" in held["job"]["proposal"]["hold_reasons"][0], "no 希望納期: proposal written but held (pending queue)")
        check(held and any("短すぎる" in r for r in held["job"]["proposal"]["hold_reasons"]),
              "a too-short answer (not a real proposal) is never auto-submitted")
        comp = await wait_for(a_inbox, job_event(5622772, lambda j: not j["bid"].get("pending")))
        check(comp and not comp["job"]["bid"]["allowed"] and "コンペ" in comp["job"]["bid"]["reasons"][0], "コンペ excluded")

        check(len(MockOpenRouter.calls) == 2, f"OpenRouter called once per proposal ({len(MockOpenRouter.calls)})")
        system = MockOpenRouter.calls[0]["messages"][0]["content"]
        user_msg = MockOpenRouter.calls[0]["messages"][1]["content"]
        call = MockOpenRouter.calls[0]
        check(call["model"] == "openai/gpt-5.4-mini" and call["reasoning"] == {"effort": "high", "exclude": True}
              and call["provider"] == {"sort": "latency"} and call["max_tokens"] == 20000,
              f"OpenRouter gets the effort, the routing and a budget sized for it ({call['model']}, {call.get('reasoning')}, {call.get('provider')}, {call['max_tokens']})")
        check(PROMPT in system and "3000文字以内" in system and f"希望納期（完了予定日として入力されます）: {DELIVERY}" in user_msg
              and "源泉徴収: 源泉徴収をしない" in user_msg, "prompt + rules + Project Information sent to the model")
        check("本人による説明: ECサイトの決済連携を担当" in user_msg and "Stripe決済と在庫連携を実装したECサイト" in user_msg
              and "x()" not in user_msg and "（ページを取得できませんでした" in user_msg,
              "the portfolio pages' text goes to the model with each proposal")
        check(f"■ http://127.0.0.1:{MOCK_PORT}/client/site\nページのタイトル: 現行の受発注システム" in user_msg
              and "Access製の受発注管理画面" in user_msg, "the page the client linked in the 依頼内容 is read and given to the model")
        check(call.get("plugins") == [{"id": "web"}] and "Web検索の結果が与えられる" in system, "web search on: OpenRouter's web plugin is used")

        a_inbox.clear()
        await a_ws.send(json.dumps({"type": "block-client", "key": "lancers:5614153"}))
        result = await wait_for(a_inbox, lambda m: m["type"] == "block-client-result")
        check(result and result["ok"] and result["added"] and result["updated_jobs"] == 1, "block-client answers like the old server")
        blocked = await wait_for(a_inbox, job_event(5614153, lambda j: j["bid"]["blocked"]))
        check(blocked and not blocked["job"]["bid"]["allowed"], "the blocked client's open job is re-sent as blocked")

        await a_ws.send(json.dumps({"type": "bid-result", "key": "lancers:5600001", "status": "submitted", "step": "done", "message": "ok"}))
        await asyncio.sleep(1)
        bids = Store(DB)._all("SELECT status FROM bids")
        check([b["status"] for b in bids] == ["submitted"], "bid results are recorded")

        # A bid that found Lancers' 提案できません page: the job is closed
        a_inbox.clear()
        await a_ws.send(json.dumps({"type": "bid-result", "key": "lancers:5622772", "status": "failed", "step": "open", "message":
                                    'Could not recognise this page as the proposal form. The page says: "For ランサー 仕事を探す 提案できません '
                                    '依頼者が退会しているため、この依頼に提案できません。 パッケージを探す"'}))
        closed = await wait_for(a_inbox, job_event(5622772, lambda j: j["status"]["open"] is False))
        check(closed and closed["job"]["bid"]["reasons"] == ["提案できません: 依頼者が退会しているため、この依頼に提案できません。"]
              and closed["job"]["status"]["closed_reason"].startswith("提案できません") and not closed["job"]["bid"]["allowed"],
              "Lancers says 提案できません on a bid: the job is closed, with Lancers' reason")

        # ---- Activity log
        now_ms = int(time.time() * 1000)
        await a_ws.send(json.dumps({"type": "activity", "req_id": "act1", "events": [
            {"ts": now_ms, "kind": "bid.result", "job_id": 5614153, "data": {"status": "submitted", "message": "提案文"}},
            {"kind": 5}, {"ts": now_ms, "kind": "big", "data": {"x": "あ" * 30000}}]}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "act1")
        rows = Store(DB).list_activity()
        check(reply and reply["saved"] == 2 and rows[0]["kind"] == "bid.result" and rows[0]["job_id"] == 5614153 and rows[0]["user"] == "owner"
              and rows[0]["ext_version"] == EXT_VERSION and rows[0]["ts"] == now_ms // 1000 and rows[1]["data"].get("truncated"),
              "activity events are stored per user (malformed ones dropped, oversized data cut)")

        # ---- New filters apply to jobs already decided (but not to ones already submitted)
        a_inbox.clear()
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "f1", "profile": {"filters": {"exclude_keywords": "【要件定義】"}}}))
        dropped = await wait_for(a_inbox, job_event(5614153, lambda j: any("除外キーワード" in r for r in j["bid"]["reasons"])))
        store2 = Store(DB)
        check(dropped and store2.get_proposal(1, 5614153)["status"] == "skipped" and store2.get_proposal(1, 5600001)["status"] == "ready",
              "an excluded keyword added later withdraws the open proposal; a submitted one is left alone")
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "f2", "profile": {"filters": {"exclude_clients": "vcvcvc / vc、  株式会社Ａ  "}}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "f2")
        check(reply and reply["ok"] and reply["profile"]["filters"]["exclude_clients"] == ["vcvcvc / vc", "株式会社Ａ"]
              and reply["profile"]["filters"]["exclude_keywords"] == ["【要件定義】"],
              "excluded client names are saved on their own, and the keywords stay")

        # ---- Test tab: a prompt and a model on pasted project text, nothing saved
        calls = len(MockOpenRouter.calls)
        saved = Store(DB)._one("SELECT COUNT(*) AS n, MAX(created_ts) AS last FROM proposals")
        await a_ws.send(json.dumps({"type": "test.generate", "req_id": "x1", "details": "依頼：ECサイトの改修。予算5万円。", "prompt": "丁寧に書いて。",
                                    "model": "openai/gpt-5.4-mini", "reasoning_effort": "low", "speed": "fast"}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "x1", timeout=20)
        call = MockOpenRouter.calls[-1] if len(MockOpenRouter.calls) > calls else {}
        check(reply and reply["ok"] and reply["text"] == PROPOSAL and reply["model"] == "openai/gpt-5.4-mini:nitro"
              and call["messages"][0]["content"].startswith("丁寧に書いて。") and "3000文字以内" in call["messages"][0]["content"]
              and call["messages"][1]["content"] == "依頼：ECサイトの改修。予算5万円。"
              and call.get("reasoning") == {"effort": "low", "exclude": True} and call.get("model") == "openai/gpt-5.4-mini:nitro"
              and tuple(Store(DB)._one("SELECT COUNT(*) AS n, MAX(created_ts) AS last FROM proposals")) == tuple(saved),
              "Test tab: the pasted details + prompt (with the output rules), reasoning and speed go to the chosen model with the user's key; nothing is saved")
        await a_ws.send(json.dumps({"type": "test.generate", "req_id": "x1b", "details": "依頼", "prompt": "丁寧に。",
                                    "model": "openai/gpt-5.4-mini", "reasoning_effort": "max"}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "x1b")
        check(reply and not reply["ok"] and "does not take" in reply["error"], "Test tab: an effort the model doesn't take is refused")
        await a_ws.send(json.dumps({"type": "test.generate", "req_id": "x2", "details": "", "prompt": "p", "model": "openai/gpt-5.4-mini"}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "x2")
        check(reply and not reply["ok"] and "project details" in reply["error"], "Test tab: empty project details are refused")
        # ---- Grok Free off (manage.py grok-free off): no Grok Drafter, no Grok Free model or setting
        store.set_grok_free(False)
        drafter_url = lambda token: f"ws://127.0.0.1:{SERVER_PORT}/ws?token={urllib.parse.quote(token)}&client=grok-test&kind=grok-drafter&ver=0.1.0"
        for req_id, msg in (("o1", {"type": "test.generate", "details": "d", "prompt": "p", "model": "grok-free"}),
                            ("o2", {"type": "profile.update", "profile": {"use_grok_free": True}}),
                            ("o3", {"type": "profile.update", "profile": {"model": "grok-free"}})):
            await a_ws.send(json.dumps({**msg, "req_id": req_id}))
            reply = await wait_for(a_inbox, lambda m: m.get("req_id") == req_id)
            check(reply and not reply["ok"] and "not enabled" in reply["error"], f"Grok Free off: {msg['type']} with Grok Free is refused")
        closed = await refused(drafter_url(alice))
        check(closed and closed.code == 4403 and "not enabled" in closed.reason, f"Grok Free off: the Grok Drafter is refused ({closed and closed.reason})")
        store.set_grok_free(True)

        # ---- Grok Free: a setting for users on the Grok Free list; the drafter connects from anywhere (wss through nginx)
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "g0", "profile": {"filters": {"exclude_keywords": "", "exclude_clients": ""}}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "g0")
        check(reply["profile"]["grok_free_allowed"] and not reply["profile"]["use_grok_free"] and not reply["profile"]["grok_drafter_connected"],
              "Grok Free on; the setting is off, no drafter yet")
        async with websockets.connect(drafter_url(alice), additional_headers={"X-Forwarded-For": "203.0.113.9"}) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        check(hello.get("kind") == "grok-drafter", "a Grok Drafter coming in from outside (through nginx) is accepted")
        await asyncio.sleep(0.3)
        async with websockets.connect(drafter_url(alice)) as g_ws:
            hello = json.loads(await g_ws.recv())
            health = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/health").read())
            check(hello == {"type": "hello", "kind": "grok-drafter", "user": "owner"} and health["grok_drafters"] == 1 and health["clients"] == 1,
                  f"the Grok Drafter connects (v0.1.0: no update check), gets no jobs ({health})")
            pushed = await wait_for(a_inbox, lambda m: m["type"] == "profile" and not m.get("req_id") and m["profile"]["grok_drafter_connected"])
            check(bool(pushed), "her Lancers extension is told the drafter is connected")

            # Setting off: ↻ uses the API although the drafter is connected
            a_inbox.clear()
            await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5600001"}))
            api = await wait_for(a_inbox, job_event(5600001, lambda j: (j["proposal"] or {}).get("model") == "openai/gpt-5.4-mini"))
            try:
                stray = json.loads(await asyncio.wait_for(g_ws.recv(), 1))
            except asyncio.TimeoutError:
                stray = {}
            check(api and stray.get("type") != "grok.draft", "Grok Free off: ↻ is written through the API, nothing goes to Grok")

            # Test tab with Grok Free: the drafter writes it
            await a_ws.send(json.dumps({"type": "test.generate", "req_id": "x3", "details": "テスト案件", "prompt": "私は斎藤です。", "model": "grok-free"}))
            while (req := json.loads(await asyncio.wait_for(g_ws.recv(), 20)))["type"] != "grok.draft":
                pass
            await g_ws.send(json.dumps({"type": "grok.result", "id": req["id"], "ok": True, "text": PROPOSAL}))
            reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "x3")
            check(reply and reply["ok"] and reply["model"] == "grok-web" and req["text"].startswith("私は斎藤です。") and req["text"].endswith("テスト案件"),
                  "Test tab with Grok Free: prompt + rules + details go to the Grok Drafter, its answer comes back")

            # Model "Grok Free", checkbox off: a proposal (what Auto-Bid would submit) is written by the drafter
            await a_ws.send(json.dumps({"type": "profile.update", "req_id": "gm", "profile": {"model": "grok-free"}}))
            reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "gm")
            check(reply and reply["ok"] and reply["profile"]["model"] == "grok-free" and not reply["profile"]["use_grok_free"]
                  and not reply["profile"]["not_ready"], "Grok Free can be saved as the model, with no extra setting")
            a_inbox.clear()
            await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5600001"}))
            while (req := json.loads(await asyncio.wait_for(g_ws.recv(), 20)))["type"] != "grok.draft":
                pass
            await g_ws.send(json.dumps({"type": "grok.result", "id": req["id"], "ok": True, "text": PROPOSAL}))
            done = await wait_for(a_inbox, job_event(5600001, lambda j: (j["proposal"] or {}).get("model") == "grok-web"))
            check(bool(done), "with Grok Free selected, a proposal is written by the drafter even though ↻ Grok Free is off")
            await a_ws.send(json.dumps({"type": "profile.update", "req_id": "gm2", "profile": {"model": "openai/gpt-5.4-mini"}}))
            reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "gm2")
            check(reply and reply["ok"] and reply["profile"]["model"] == "openai/gpt-5.4-mini", "the API model is restored")
            await a_ws.send(json.dumps({"type": "profile.update", "req_id": "g1", "profile": {"use_grok_free": True}}))
            reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "g1")
            check(reply and reply["ok"] and reply["profile"]["use_grok_free"] and reply["profile"]["grok_drafter_connected"], "Grok Free switched on")
            a_inbox.clear()
            await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5600001"}))
            while True:
                req = json.loads(await asyncio.wait_for(g_ws.recv(), 20))
                if req["type"] == "grok.draft":
                    break
            check(PROMPT in req["text"] and "【依頼内容】" in req["text"] and "必要ならWebで確認してよい" in req["text"],
                  "↻ Redraft by a Grok Drafter user: the whole chat message (prompt, rules, job) goes to the drafter")
            await g_ws.send(json.dumps({"type": "grok.result", "id": req["id"], "ok": True, "seconds": 12,
                                        "text": f"```\n{PROPOSAL}\n```\n詳しくは([example.com](https://example.com/))"}))
            drafted = await wait_for(a_inbox, job_event(5600001, lambda j: (j["proposal"] or {}).get("model") == "grok-web"))
            check(drafted and drafted["job"]["proposal"]["message"].startswith(PROPOSAL), "Grok's answer becomes the proposal (wrapping and citation links removed)")
            # ↻ on a job this user's filters / block list exclude: nothing goes to Grok, and the card is told why
            a_inbox.clear()
            await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5614153"}))
            answered = await wait_for(a_inbox, job_event(5614153, lambda j: not j["bid"]["allowed"] and not j["bid"].get("pending")))
            try:
                stray = json.loads(await asyncio.wait_for(g_ws.recv(), 1.5))
            except asyncio.TimeoutError:
                stray = None
            check(answered and "ブロックリスト" in answered["job"]["bid"]["reasons"][0] and (stray or {}).get("type") != "grok.draft",
                  "↻ on an excluded job: no Grok chat, and the card gets the reason instead of waiting forever")
        # The drafter goes away in the middle of a draft: it fails at once, not after the timeout
        async with websockets.connect(drafter_url(alice)) as g_ws:
            await g_ws.recv()
            a_inbox.clear()
            await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5600001"}))
            while json.loads(await asyncio.wait_for(g_ws.recv(), 20))["type"] != "grok.draft":
                pass
        started = time.time()
        failed = await wait_for(a_inbox, job_event(5600001, lambda j: any("disconnected during the draft" in r for r in j["bid"]["reasons"])), timeout=10)
        check(failed and time.time() - started < 5, f"drafter disconnected mid-draft: the draft fails at once ({time.time() - started:.1f} s)")
        a_inbox.clear()
        await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5600001"}))
        redrawn = await wait_for(a_inbox, job_event(5600001, lambda j: (j["proposal"] or {}).get("model") == "openai/gpt-5.4-mini"))
        check(bool(redrawn), "with the drafter disconnected, ↻ uses the API again")

        # ---- Several Grok Free devices: the one chosen in the settings writes the draft
        device_url = lambda cid, name: (f"ws://127.0.0.1:{SERVER_PORT}/ws?token={urllib.parse.quote(alice)}&client={cid}&kind=grok-drafter&ver=0.6.0"
                                        f"&device={urllib.parse.quote(name)}")
        async with websockets.connect(device_url("grok-vps", "VPS Chrome")) as vps, websockets.connect(device_url("grok-home", "Home PC")) as home:
            await vps.recv()
            await home.recv()
            await a_ws.send(json.dumps({"type": "profile.update", "req_id": "d1", "profile": {"grok_device": "grok-home"}}))
            reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "d1")
            devices = {d["name"]: d["connected"] for d in reply["profile"]["grok_devices"]}
            check(reply["ok"] and reply["profile"]["grok_device"] == "grok-home" and devices.get("VPS Chrome") and devices.get("Home PC")
                  and devices.get("grok-test") is False,
                  f"her devices are listed by name, connected or not, and one can be chosen ({devices})")
            await a_ws.send(json.dumps({"type": "profile.update", "req_id": "d2", "profile": {"grok_device": "someone-elses"}}))
            reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "d2")
            check(not reply["ok"] and "not one of yours" in reply["error"], "a device that isn't hers can't be chosen")
            a_inbox.clear()
            await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5600001"}))
            while (req := json.loads(await asyncio.wait_for(home.recv(), 20)))["type"] != "grok.draft":
                pass
            try:
                stray = json.loads(await asyncio.wait_for(vps.recv(), 1))
            except asyncio.TimeoutError:
                stray = {}
            await home.send(json.dumps({"type": "grok.result", "id": req["id"], "ok": True, "text": PROPOSAL}))
            done = await wait_for(a_inbox, job_event(5600001, lambda j: (j["proposal"] or {}).get("model") == "grok-web"))
            check(done and stray.get("type") != "grok.draft", "↻ goes to the chosen device (Home PC) only")
        await asyncio.sleep(0.3)
        a_inbox.clear()
        await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5600001"}))
        api = await wait_for(a_inbox, job_event(5600001, lambda j: (j["proposal"] or {}).get("model") == "openai/gpt-5.4-mini"))
        check(bool(api), "the chosen device offline: ↻ uses the API")
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "d3", "profile": {"grok_device": ""}}))
        await wait_for(a_inbox, lambda m: m.get("req_id") == "d3")

        # ---- Version check
        try:
            async with websockets.connect(f"ws://127.0.0.1:{SERVER_PORT}/ws?token={urllib.parse.quote(alice)}&since=0&client=old") as ws:
                await asyncio.wait_for(ws.recv(), 5)
            closed = None
        except websockets.ConnectionClosed as e:
            closed = e.rcvd
        check(closed and closed.code == 4426 and f"v{EXT_VERSION} is required" in closed.reason,
              "an extension too old to send its version is refused, with the reason it shows")
        async with websockets.connect(f"ws://127.0.0.1:{SERVER_PORT}/ws?token={urllib.parse.quote(alice)}&since=0&client=outdated&caps=fetch&ver=1.4.9", max_size=None) as ws:
            hello = json.loads(await ws.recv())
            check(hello["type"] == "hello" and hello["update"] == {"type": "update", "required": True, "latest": EXT_VERSION, "current": "1.4.9"}
                  and hello["replay"] == 0, "an older version is told to update in the hello, and gets no jobs")
            health = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/health").read())
            check(health["clients"] == 2 and health["relay_clients"] == 1, f"...nor relay fetches ({health})")
            await ws.send(json.dumps({"type": "refresh", "key": "lancers:5614153", "req_id": "o1"}))
            err = json.loads(await asyncio.wait_for(ws.recv(), 5))
            check(err["type"] == "error" and err["req_id"] == "o1" and "update the extension" in err["message"], "...and its other requests are refused")
            await ws.send(json.dumps({"type": "extension.download", "req_id": "d1"}))
            dl = json.loads(await asyncio.wait_for(ws.recv(), 10))
            names = zipfile.ZipFile(io.BytesIO(base64.b64decode(dl["zip_b64"]))).namelist()
            check(dl["ok"] and dl["version"] == EXT_VERSION and dl["filename"] == f"lancers-proposal-extension-{EXT_VERSION}.zip"
                  and sorted(names) == ["lancers-proposal-extension/background.js", "lancers-proposal-extension/icons/icon16.png",
                                        "lancers-proposal-extension/manifest.json"],
                  f"...but can download the current version as a zip ({names})")
        with open(os.path.join(EXT_DIR, "manifest.json"), "w") as f:
            f.write(json.dumps({"name": "test", "version": "1.5.1"}))
        update = await wait_for(a_inbox, lambda m: m["type"] == "update", timeout=5)
        check(update and update["required"] and update["latest"] == "1.5.1" and update["current"] == EXT_VERSION,
              "a new version on the server: connected extensions are told at once")
        a_inbox.clear()
        with open(os.path.join(EXT_DIR, "manifest.json"), "w") as f:
            f.write(json.dumps({"name": "test", "version": EXT_VERSION}))
        back = await wait_for(a_inbox, lambda m: m["type"] == "update", timeout=5)
        check(back and back["required"] is False, "the version taken back: they carry on")

        # ---- An OpenAI key: OpenAI is called directly, OpenAI models only
        a_inbox.clear()
        openai_key = "sk-proj-owner0123456789abcdef"
        for req_id, extra, want in (("c1", {"model": "x-ai/grok-4.7"}, "only OpenAI models"),
                                    ("c2", {"model": "openai/gpt-4o-2024-11-20", "speed": "normal"}, "not available with this OpenAI key"),
                                    ("c3", {"model": "openai/gpt-5.4-mini", "speed": "latency"}, "Low latency is an OpenRouter option")):
            await a_ws.send(json.dumps({"type": "profile.update", "req_id": req_id, "profile": {"prompt": PROMPT, "api_key": openai_key, **extra}}))
            reply = await wait_for(a_inbox, lambda m: m.get("req_id") == req_id)
            check(reply and not reply["ok"] and want in reply["error"], f"OpenAI key refused with {extra} ({(reply or {}).get('error')})")
        check(Store(DB).get_profile(1)["key_hint"] == "sk-or-…cdef", "...and nothing was saved (the OpenRouter key stays)")
        await a_ws.send(json.dumps({"type": "profile.update", "req_id": "c4", "profile": {
            "prompt": PROMPT, "api_key": openai_key, "model": "openai/gpt-5.4-mini", "reasoning_effort": "low", "speed": "fast", "web_search": True}}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "c4")
        check(reply and reply["ok"] and reply["profile"]["key_provider"] == "openai" and not reply["profile"]["not_ready"],
              f"an OpenAI key with an OpenAI model it can use is saved ({(reply or {}).get('error')})")
        await a_ws.send(json.dumps({"type": "profile.test-key", "req_id": "c5"}))
        reply = await wait_for(a_inbox, lambda m: m.get("req_id") == "c5")
        check(reply and reply["ok"] and "OpenAI key OK" in reply["info"], "test-key checks an OpenAI key with OpenAI")
        router_calls = len(MockOpenRouter.calls)
        await a_ws.send(json.dumps({"type": "refresh", "key": "lancers:5600001"}))
        ready = await wait_for(a_inbox, job_event(5600001, lambda j: j["proposal"]), timeout=20)
        body = MockOpenRouter.openai_calls[-1] if MockOpenRouter.openai_calls else {}
        check(ready and ready["job"]["proposal"]["message"] == PROPOSAL and ready["job"]["proposal"]["model"] == "openai/gpt-5.4-mini"
              and len(MockOpenRouter.calls) == router_calls,
              "the proposal is written by OpenAI itself (not OpenRouter), citation links removed")
        check(body.get("model") == "gpt-5.4-mini" and PROMPT in body["instructions"] and "【依頼内容】" in body["input"]
              and body["reasoning"] == {"effort": "low"} and body["service_tier"] == "priority" and body["tools"] == [{"type": "web_search"}],
              f"OpenAI gets the model, prompt, job, reasoning effort, priority tier and web search ({ {k: body.get(k) for k in ('model', 'reasoning', 'service_tier', 'tools')} })")

        # ---- Changing the password closes every connection that used the old one
        async with websockets.connect(f"ws://127.0.0.1:{SERVER_PORT}/ws?token={urllib.parse.quote(PASSWORD)}&client=grok-pw&kind=grok-drafter&ver=0.6.0") as g_ws:
            await g_ws.recv()
            Store(DB).set_password(NEW_PASSWORD)          # what manage.py set-password does
            ext_closed, drafter_closed = await wait_closed(a_ws), await wait_closed(g_ws)
        check(ext_closed == (4401, "the password was changed") and drafter_closed == (4401, "the password was changed"),
              f"after a password change the extension and the Grok Drafter are disconnected ({ext_closed}, {drafter_closed})")
        closed = await refused(ws_url(PASSWORD))
        check(closed and closed.code == 4401, "the old password no longer works")
        check(await refused(ws_url(NEW_PASSWORD)) is None, "the new one does")

        for t in tasks:
            t.cancel()
    finally:
        server.terminate()
        server.wait()
    log = open(os.path.join(SCRATCH, "e2e-server.log")).read()
    check(PASSWORD not in log and NEW_PASSWORD not in log and urllib.parse.quote(PASSWORD) not in log and "0123456789abcdef" not in log,
          "no password or key in the server log")
    print("all good")


if __name__ == "__main__":
    mock = HTTPServer(("127.0.0.1", MOCK_PORT), MockOpenRouter)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    asyncio.run(main())

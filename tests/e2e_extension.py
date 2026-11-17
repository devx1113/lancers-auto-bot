"""The Lancers Auto-Proposal extension (in headless Chromium) against the real server, offline.

    python tests/e2e_extension.py <scratch dir> [path/to/lancers-proposal-extension]     (CHROMIUM=<binary> to pick the browser)

A copy of the extension is pointed at a test server on a throwaway database; the Project Information
is pre-seeded from the fixtures and OpenRouter is a local mock, so nothing reaches Lancers, OpenRouter
or Slack. The relay is switched off and Auto-Bid waits a day before bidding, so no tab is ever opened
on lancers.jp. Driven over the Chrome DevTools protocol from an extension page (sidepanel.html).
"""

import asyncio
import base64
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = sys.argv[1]
EXT_SRC = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(ROOT), "lancers-proposal-extension")
DB = os.path.join(SCRATCH, "ext-e2e.db")
SERVER_PORT, MOCK_PORT, CDP_PORT = 8799, 8798, 9333
# Google-branded Chrome ignores --load-extension since 137; Chromium (e.g. Playwright's build) still loads it
CHROME = os.getenv("CHROMIUM") or os.path.expanduser("~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import websockets  # noqa: E402

from e2e_server import CARDS, PAGES, PROPOSAL, MockOpenRouter, card, check  # noqa: E402

# After the e2e_server import, which sets its own database. The server's extension folder (current
# version, update download) is a copy of the extension under test
EXT_SERVER = os.path.join(SCRATCH, "ext-server")
shutil.rmtree(EXT_SERVER, ignore_errors=True)
shutil.copytree(EXT_SRC, EXT_SERVER)
os.environ.update({"LANCERS_DB": DB, "SERVER_PORT": str(SERVER_PORT), "OPENROUTER_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
                   "SLACK_WEBHOOK_BIDS": "", "SLACK_WEBHOOK_ALERTS": "", "EXTENSION_DIR": EXT_SERVER})
from lancers_bot.details import parse_propose_page  # noqa: E402
from lancers_bot.store import Store  # noqa: E402

PROMPT = "あなたはWeb系エンジニアです。【私について】Python/Django歴5年。"
EXTRA = {"id": 5614154, "work_type": "プロジェクト", "client_url": "https://www.lancers.jp/client/later", "client_name": "later"}
PAGES[5614154] = PAGES[5614153]


class Page:
    """Runtime.evaluate in one DevTools target"""

    def __init__(self, ws):
        self.ws = ws
        self.ids = itertools.count(1)

    async def eval(self, expression):
        msg_id = next(self.ids)
        await self.ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate",
                                       "params": {"expression": expression, "awaitPromise": True, "returnByValue": True}}))
        while True:
            reply = json.loads(await self.ws.recv())
            if reply.get("id") == msg_id:
                result = reply["result"]
                if "exceptionDetails" in result:
                    raise RuntimeError(result["exceptionDetails"])
                return result["result"].get("value")


class Interceptor:
    """Answers the service worker's requests to Lancers and Slack over the DevTools Fetch domain:
    canned JSON for the Lancers header endpoints, and Slack posts captured instead of sent"""

    def __init__(self, ws):
        self.ws = ws
        self.ids = itertools.count(1000)
        self.lancers = {}          # path -> JSON answer
        self.lancers_calls = []
        self.slack = []            # captured webhook payloads

    async def start(self):
        await self.ws.send(json.dumps({"id": next(self.ids), "method": "Fetch.enable", "params": {"patterns": [
            {"urlPattern": "https://www.lancers.jp/*"}, {"urlPattern": "https://hooks.slack.com/*"}]}}))
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("method") != "Fetch.requestPaused":
                continue
            req = msg["params"]["request"]
            if req["url"].startswith("https://hooks.slack.com/"):
                self.slack.append(json.loads(req.get("postData") or "{}"))
                body, ctype = b"ok", "text/plain"
            else:
                path = req["url"].split("www.lancers.jp", 1)[1]
                self.lancers_calls.append(path)
                answer = self.lancers.get(path.split("?")[0].rstrip("/"), {})
                body, ctype = answer if isinstance(answer, tuple) else (json.dumps(answer).encode(), "application/json")
            await self.ws.send(json.dumps({"id": next(self.ids), "method": "Fetch.fulfillRequest", "params": {
                "requestId": msg["params"]["requestId"], "responseCode": 200,
                "responseHeaders": [{"name": "Content-Type", "value": ctype}], "body": base64.b64encode(body).decode()}}))


def cdp_json(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{CDP_PORT}{path}", method=method)
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


async def state(panel):
    return await panel.eval('chrome.runtime.sendMessage({type: "getState"})')


async def wait_state(panel, pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        s = await state(panel)
        if pred(s):
            return s
        await asyncio.sleep(0.5)
    return await state(panel)


async def main():
    store = Store(DB)
    token = "e2e-Extension-Passw0rd!"          # the owner password, entered in the extension's Token field
    store.set_password(token)
    # Project Information as if the relay had read the pages already
    for job_id, html in PAGES.items():
        parsed = parse_propose_page(html)
        store.save_details(job_id, parsed["status"], parsed["info"], parsed["error"])

    ext = os.path.join(SCRATCH, "ext")
    shutil.rmtree(ext, ignore_errors=True)
    shutil.copytree(EXT_SRC, ext)
    with open(os.path.join(ext, "defaults.js"), "w") as f:
        f.write(f'export const SERVER_DEFAULTS = {{ serverUrl: "ws://127.0.0.1:{SERVER_PORT}", token: "{token}" }};\n')

    server = subprocess.Popen([sys.executable, os.path.join(ROOT, "run_server.py")], cwd=ROOT,
                              stdout=open(os.path.join(SCRATCH, "ext-e2e-server.log"), "w"), stderr=subprocess.STDOUT)
    profile_dir = os.path.join(SCRATCH, "chrome-profile")
    shutil.rmtree(profile_dir, ignore_errors=True)
    chrome = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", f"--user-data-dir={profile_dir}",
                               f"--remote-debugging-port={CDP_PORT}", "--remote-allow-origins=*", f"--load-extension={ext}",
                               f"--disable-extensions-except={ext}",
                               # Safety net: anything not intercepted below fails instead of reaching the real sites
                               "--host-resolver-rules=MAP www.lancers.jp 127.0.0.1:9, MAP hooks.slack.com 127.0.0.1:9", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(4)
        ext_id = None
        # Chromium runs component extensions too: ours is the one named Lancers Auto-Proposal
        for t in cdp_json("/json/list"):
            if t["type"] != "service_worker" or not t["url"].endswith("/background.js"):
                continue
            async with websockets.connect(t["webSocketDebuggerUrl"]) as sw:
                if "Lancers Auto-Proposal" in (await Page(sw).eval("chrome.runtime.getManifest().name") or ""):
                    ext_id, sw_url = t["url"].split("/")[2], t["webSocketDebuggerUrl"]
        check(bool(ext_id), "extension loaded (service worker running)")
        target = cdp_json("/json/new?about:blank", method="PUT")
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws, \
                   websockets.connect(sw_url, max_size=None) as sw_ws:
            net = Interceptor(sw_ws)
            await net.start()
            panel = Page(ws)
            # An extension page, so chrome.runtime.sendMessage reaches the service worker like the side panel does
            await ws.send(json.dumps({"id": 0, "method": "Page.navigate", "params": {"url": f"chrome-extension://{ext_id}/sidepanel.html"}}))
            await asyncio.sleep(2)
            check(await panel.eval("typeof chrome.runtime?.sendMessage") == "function", f"side panel page open ({await panel.eval('location.href')})")
            # Offline test: no relay, and an automatic bid waits ~10 years after 募集開始 (the fixture's date is
            # fixed, so it must count as just posted whenever this runs)
            await panel.eval('chrome.runtime.sendMessage({type: "saveSettings", settings: {relay: false, notify: false, bidding: {minAgeSeconds: 315360000}}})')
            s = await wait_state(panel, lambda s: s["status"].get("state") == "connected" and s.get("server", {}).get("profile"))
            check(s["status"]["state"] == "connected" and s["server"]["user"] == "owner", "connects with the built-in server URL and the password")
            check(bool(s["server"]["profile"]["not_ready"]), "hello brings the (still empty) server profile")

            r = await panel.eval(f'chrome.runtime.sendMessage({{type: "serverProfileSave", profile: {{prompt: {json.dumps(PROMPT)}, api_key: "sk-or-v1-feedfacecafebeef", model: "openai/gpt-5.4-mini"}}}})')
            check(r["ok"] and not r["profile"]["not_ready"] and r["profile"]["key_hint"] == "sk-or-…beef", "Settings → Save to server stores the profile")
            r = await panel.eval('chrome.runtime.sendMessage({type: "serverProfileTest", apiKey: ""})')
            check(r["ok"], "Settings → test checks the saved key on the server")

            await panel.eval('document.querySelector(\'.tab[data-tab="settings"]\').click()')
            await asyncio.sleep(1.5)
            groups = await panel.eval('Array.from(document.querySelectorAll("#spModel optgroup")).map(g => g.label + ": " + g.children.length)')
            chosen = await panel.eval('document.getElementById("spModel").value')
            check(groups == ["Claude (Anthropic): 2", "Grok (xAI): 1", "OpenAI: 2"] and chosen == "openai/gpt-5.4-mini",
                  f"model dropdown grouped by provider, saved model selected ({groups}, {chosen})")
            efforts = await panel.eval('Array.from(document.querySelectorAll("#spEffort option")).map(o => o.value || o.textContent)')
            check(efforts == ["Model default (Medium)", "xhigh", "high", "medium", "low", "none"], f"Reasoning offers the chosen model's levels ({efforts})")
            await panel.eval('(() => { const s = document.getElementById("spModel"); s.value = "x-ai/grok-4.7"; s.dispatchEvent(new Event("change")); })()')
            efforts = await panel.eval('Array.from(document.querySelectorAll("#spEffort option")).map(o => o.value || o.textContent)')
            check(efforts == ["Model default (High)", "xhigh", "high", "medium", "low"], f"switching to Grok 4.7 offers its levels ({efforts})")
            await panel.eval('document.getElementById("spEffort").value = "low"; document.getElementById("spSpeed").value = "fast"')
            site = f"http://127.0.0.1:{MOCK_PORT}/portfolio"
            await panel.eval(f'document.getElementById("spPortfolio").value = {json.dumps(f"{site}/shop  決済連携を担当\n{site}/missing")};'
                             'document.getElementById("spWebSearch").checked = true')
            await panel.eval('document.getElementById("spSave").click()')
            s = await wait_state(panel, lambda s: (s["server"]["profile"] or {}).get("model") == "x-ai/grok-4.7")
            p = s["server"]["profile"]
            check(p["model"] == "x-ai/grok-4.7" and p["reasoning_effort"] == "low" and p["speed"] == "fast", f"Save to server stores model, effort and speed ({p['model']}, {p['reasoning_effort']}, {p['speed']})")
            s = await wait_state(panel, lambda s: all(e["status"] != "reading" for e in s["server"]["profile"]["portfolio"]))
            rows = await panel.eval('Array.from(document.querySelectorAll("#spPortfolioStatus li")).map(e => e.innerText.replace(/\\s+/g, " "))')
            check(s["server"]["profile"]["web_search"] and len(rows) == 2 and rows[0].startswith("✓") and "ECショップ構築事例" in rows[0]
                  and rows[1].startswith("✗") and "404" in rows[1], f"portfolio + web search saved; each page's read status shown ({rows})")
            text = await panel.eval('document.getElementById("spPortfolio").value')
            check(text == f"{site}/shop  決済連携を担当\n{site}/missing", "the portfolio box shows what the server stored")
            hidden = await panel.eval('document.getElementById("spGrokFreeRow").hidden && ![...document.querySelectorAll("#spModel option")].some(o => o.value === "grok-free")')
            check(hidden and not s["server"]["profile"]["grok_free_allowed"],
                  "the Grok Free setting and model are hidden from someone not on the Grok Free list")

            # Test tab: paste project details, a prompt, pick a model, Generate
            await panel.eval('document.querySelector(\'.tab[data-tab="test"]\').click()')
            await asyncio.sleep(1)
            options = await panel.eval('Array.from(document.querySelectorAll("#tModel option")).map(o => o.value)')
            picked = await panel.eval("""(() => {
              document.getElementById("tDetails").value = "依頼：予約サイトの改修。予算10万円。";
              document.getElementById("tPrompt").value = "あなたはWebエンジニアです。";
              const s = document.getElementById("tModel");
              s.value = "openai/gpt-5.4-mini"; s.dispatchEvent(new Event("change"));
              const efforts = Array.from(document.querySelectorAll("#tEffort option")).map(o => o.value || o.textContent);
              document.getElementById("tEffort").value = "low";
              document.getElementById("tSpeed").value = "fast";
              document.getElementById("tRun").click();
              return { efforts, speed: document.getElementById("tSpeed").value }; })()""")
            check(picked["efforts"] == ["Model default (Medium)", "xhigh", "high", "medium", "low", "none"] and picked["speed"] == "fast",
                  f"Test tab: reasoning offers the chosen model's levels, and speed can be set ({picked})")
            end = time.time() + 20
            while time.time() < end and not await panel.eval('document.getElementById("tOutput").value'):
                await asyncio.sleep(0.5)
            out = await panel.eval('({ text: document.getElementById("tOutput").value, info: document.getElementById("tInfo").textContent })')
            check(out["text"] == PROPOSAL and "openai/gpt-5.4-mini" in out["info"] and "grok-free" not in options and len(options) > 3,
                  f"Test tab: Generate shows the model's output for the pasted details and prompt ({out['info']})")
            await panel.eval('document.querySelector(\'.tab[data-tab="settings"]\').click()')
            await asyncio.sleep(0.5)

            # On the Grok Free list: the setting appears, with a device to choose (none has connected yet)
            Store(DB).set_grok_free("tester", True)
            await panel.eval('chrome.runtime.sendMessage({type: "serverProfileSave", profile: {}})')
            await asyncio.sleep(1)
            row = await panel.eval("""({ hidden: document.getElementById("spGrokFreeRow").hidden,
              options: Array.from(document.querySelectorAll("#spGrokDevice option")).map(o => o.textContent),
              info: document.getElementById("spGrokFreeInfo").textContent })""")
            model_opts = await panel.eval('[...document.querySelectorAll("#spModel option")].map(o => o.value)')
            check(not row["hidden"] and row["options"] == ["Any connected device"] and "No Grok Drafter has connected yet" in row["info"]
                  and "grok-free" in model_opts,
                  f"on the Grok Free list: the setting shows, with its device choice, and Grok Free is a model ({row}, {model_opts})")
            Store(DB).set_grok_free("tester", False)
            await panel.eval('chrome.runtime.sendMessage({type: "serverProfileSave", profile: {}})')

            # An OpenAI key typed in: only OpenAI models (and no OpenRouter routing) can go with it
            ui_state = await panel.eval("""(() => {
              const ids = (sel) => Array.from(document.querySelectorAll(sel + " option")).map(o => o.value).filter(Boolean);
              const key = document.getElementById("spKey");
              key.value = "sk-proj-typedbutnotsaved0000"; key.dispatchEvent(new Event("input"));
              const out = { models: ids("#spModel"), testModels: ids("#tModel"), model: document.getElementById("spModel").value,
                            latency: document.getElementById("spSpeedLatency").disabled, testLatency: document.getElementById("tSpeedLatency").disabled,
                            info: document.getElementById("spKeyInfo").textContent };
              key.value = ""; key.dispatchEvent(new Event("input"));
              out.after = ids("#spModel");
              return out; })()""")
            api = lambda ids: [i for i in ids if i != "grok-free"]
            check(api(ui_state["models"]) and all(i.startswith("openai/") for i in api(ui_state["models"]))
                  and all(i.startswith("openai/") for i in api(ui_state["testModels"]))
                  and ui_state["model"].startswith("openai/") and ui_state["latency"] and ui_state["testLatency"]
                  and "OpenAI models only" in ui_state["info"]
                  and any(i.startswith("anthropic/") for i in ui_state["after"]) and any(i.startswith("x-ai/") for i in ui_state["after"]),
                  f"an OpenAI key in the box: only OpenAI models are listed (Settings and Test), Low latency off ({ui_state})")
            await panel.eval('document.querySelector(\'.tab[data-tab="jobs"]\').click()')

            store.add_jobs([card(c) for c in CARDS])
            s = await wait_state(panel, lambda s: all(s["jobs"].get(f"lancers:{c['id']}", {}).get("_bid", {}).get("status") in ("pending", "skipped")
                                                       for c in CARDS))
            jobs = s["jobs"]
            a, b, c = jobs["lancers:5614153"], jobs["lancers:5600001"], jobs["lancers:5622772"]
            check(a["_draft"]["version"] and a["_draft"]["deliveryDate"] == "2027-12-31" and a["_draft"]["autoSubmit"],
                  "server proposal becomes the draft, 完了予定日 = 希望納期")
            check(a["_bid"]["status"] == "pending" and "Auto-Bid is off" in a["_bid"]["message"], "Auto-Bid off: the ready proposal waits in Pending")
            check(b["_bid"]["status"] == "pending" and "希望納期" in b["_bid"]["message"] and b["_draft"]["deliveryDate"] == "",
                  "no 希望納期: Pending, and no date is made up")
            check(c["_bid"]["status"] == "skipped" and "コンペ" in c["_bid"]["message"], "コンペ: No bid")
            check(a["title"].startswith("要件定義") and a["extra"]["withholding_tax"] == "源泉徴収をしない", "Project Information reaches the panel")

            await panel.eval('document.querySelector(\'.tab[data-tab="pending"]\').click()')
            await asyncio.sleep(1)
            rows = await panel.eval('Array.from(document.querySelectorAll("#pendingList .pending-row")).map(e => e.innerText.split("\\n")[0])')
            count = await panel.eval('document.getElementById("pendingCount").textContent')
            check(len(rows) == 2 and count == "2", f"Pending tab lists the 2 unsent proposals ({rows})")

            await panel.eval('chrome.runtime.sendMessage({type: "setAutoBid", value: true})')
            store.add_jobs([card(EXTRA)])
            s = await wait_state(panel, lambda s: s["jobs"].get("lancers:5614154", {}).get("_bid", {}).get("status") == "queued")
            d = s["jobs"].get("lancers:5614154", {})
            check(d.get("_bid", {}).get("status") == "queued" and "Bidding in" in d["_bid"]["message"],
                  "Auto-Bid on: a fresh auto-submittable proposal is queued (waits minAge from 募集開始)")
            check(s["jobs"]["lancers:5600001"]["_bid"]["status"] == "pending", "the held proposal is still not bid on")
            await panel.eval('chrome.runtime.sendMessage({type: "setAutoBid", value: false})')
            s = await wait_state(panel, lambda s: not s["queued"])
            check(not s["queued"], "Auto-Bid off empties the queue")
            tabs = [t["url"] for t in cdp_json("/json/list") if t["type"] == "page"]
            check(not any("lancers.jp" in u for u in tabs), "no Lancers tab was opened")

            # ---- Auto-Bid with Grok Free selected as the model (a fake Grok Drafter connected)
            Store(DB).set_grok_free("tester", True)
            await panel.eval('chrome.runtime.sendMessage({type: "serverProfileSave", profile: {model: "grok-free"}})')
            grok_job = {"id": 5614156, "work_type": "プロジェクト", "client_url": "https://www.lancers.jp/client/grokjob", "client_name": "grokjob"}
            parsed = parse_propose_page(PAGES[5614153])
            store.save_details(grok_job["id"], parsed["status"], parsed["info"], parsed["error"])
            async with websockets.connect(f"ws://127.0.0.1:{SERVER_PORT}/ws?token={token}&client=grok-fake&kind=grok-drafter&ver=0.6.1&device=Fake") as drafter:
                await drafter.recv()
                s = await wait_state(panel, lambda s: (s["server"]["profile"] or {}).get("model") == "grok-free"
                                     and (s["server"]["profile"] or {}).get("grok_drafter_connected"))
                check(s["server"]["profile"]["model"] == "grok-free" and s["server"]["profile"]["grok_drafter_connected"],
                      "Grok Free is the saved model and its drafter is connected")
                await panel.eval('chrome.runtime.sendMessage({type: "setAutoBid", value: true})')
                store.add_jobs([card(grok_job)])
                while (req := json.loads(await asyncio.wait_for(drafter.recv(), 30)))["type"] != "grok.draft":
                    pass
                check("【依頼内容】" in req["text"], "the new job is sent to the Grok Drafter, not the API")
                await drafter.send(json.dumps({"type": "grok.result", "id": req["id"], "ok": True, "text": PROPOSAL}))
                s = await wait_state(panel, lambda s: ((s["jobs"].get("lancers:5614156") or {}).get("_draft") or {}).get("model") == "grok-web"
                                     and ((s["jobs"].get("lancers:5614156") or {}).get("_bid") or {}).get("status") == "queued", timeout=30)
                job = s["jobs"].get("lancers:5614156", {})
                check(job.get("_draft", {}).get("message") == PROPOSAL and job.get("_draft", {}).get("autoSubmit")
                      and job.get("_bid", {}).get("status") == "queued",
                      f"Auto-Bid queues the Grok Free proposal ({job.get('_bid', {}).get('status')}: {(job.get('_bid', {}).get('message') or '')[:60]})")
            await panel.eval('chrome.runtime.sendMessage({type: "setAutoBid", value: false})')
            Store(DB).set_grok_free("tester", False)
            await panel.eval('chrome.runtime.sendMessage({type: "serverProfileSave", profile: {model: "openai/gpt-5.4-mini"}})')

            # ---- Slack: webhook settings, bid results, Lancers notifications
            hook = "https://hooks.slack.com/services/T000/B000/test"
            net.lancers.update({"/notification/count": {"important": 0, "message": 0, "search": 0}, "/todo_list/count": {"count": 1, "opened": True}})
            r = await panel.eval('chrome.runtime.sendMessage({type: "saveSettings", settings: {slack: {bidWebhook: "https://example.com/hook"}}})')
            check(not r["ok"] and "hooks.slack.com" in r["error"], "a webhook that is not Slack's is refused")
            r = await panel.eval(f'chrome.runtime.sendMessage({{type: "saveSettings", settings: {{slack: {{bidWebhook: "{hook}", forwardNotifications: true, notifyWebhook: ""}}}}}})')
            check(r["ok"], "webhook and notification forwarding saved")
            await asyncio.sleep(1.5)
            check(not net.slack and set(net.lancers_calls) == {"/notification/count", "/todo_list/count"},
                  f"switching on only takes a baseline of the unread counts ({net.lancers_calls})")
            r = await panel.eval(f'chrome.runtime.sendMessage({{type: "testSlack", kind: "bid", webhook: "{hook}"}})')
            check(r["ok"] and len(net.slack) == 1 and "bid results" in net.slack[0]["text"], "test button posts to the webhook")

            net.slack.clear(); net.lancers_calls.clear()
            net.lancers.update({
                "/notification/count": {"important": 2, "message": 1, "search": 0}, "/todo_list/count": {"count": 2, "opened": False},
                "/notification/header_important_list": {"notifications": [
                    {"id": 92, "content": "<b>提案</b>が選ばれました&amp;おめでとうございます", "link": "/work/detail/5614153", "categoryName": "仕事", "modifiedText": "1分前"},
                    {"id": 91, "content": "メンテナンスのお知らせ", "link": "https://www.lancers.jp/news/1", "categoryName": "運営", "modifiedText": "5分前"},
                    {"id": 80, "content": "古いお知らせ", "link": "/old", "categoryName": "運営", "modifiedText": "3日前"}]},
                "/notification/lists/message": {"results": [{"content": "<p>山田様: 見積もりの件でご相談です<br>よろしくお願いします</p>", "link": "/mypage/message/777", "time": 1793350000, "timestamp": "2分前"}]},
                "/todo_list/list": {"todos": [{"id": 5, "html_content": "<p>納品の確認をしてください</p>", "link": "/todo/5", "expiredText": "あと2日", "startedText": "今日"},
                                              {"id": 4, "html_content": "<p>前からあるTODO</p>", "link": "/todo/4", "expiredText": "", "startedText": "昨日"}]},
            })
            r = await panel.eval('chrome.runtime.sendMessage({type: "checkNotifications"})')
            texts = "\n---\n".join(p["text"] for p in net.slack)
            check(r["ok"] and len(net.slack) == 3, f"one Slack post per type whose count rose ({len(net.slack)})")
            check("提案が選ばれました&amp;おめでとうございます" in texts and "メンテナンスのお知らせ" in texts and "古いお知らせ" not in texts,
                  "お知らせ: the 2 new ones (count rose by 2), HTML stripped, not the older one")
            check("山田様: 見積もりの件でご相談です\nよろしくお願いします" in texts and "<https://www.lancers.jp/mypage/message/777|開く>" in texts,
                  "メッセージ: text and an absolute link")
            check("納品の確認をしてください" in texts and "前からあるTODO" not in texts and "あと2日" in texts, "やることリスト: only the new item, with its deadline")
            check(all(p.get("unfurl_links") is False for p in net.slack), "posts don't unfurl links")

            net.slack.clear(); net.lancers_calls.clear()
            r = await panel.eval('chrome.runtime.sendMessage({type: "checkNotifications"})')
            check(not net.slack and set(net.lancers_calls) == {"/notification/count", "/todo_list/count"}, "unchanged counts: nothing re-sent, no list read")
            net.lancers["/notification/count"] = {"important": 3, "message": 1, "search": 0}
            net.lancers["/notification/header_important_list"]["notifications"].insert(0, {"id": 93, "content": "新しい評価が届きました", "link": "/x", "categoryName": "評価", "modifiedText": "今"})
            r = await panel.eval('chrome.runtime.sendMessage({type: "checkNotifications"})')
            check(len(net.slack) == 1 and "新しい評価が届きました" in net.slack[0]["text"] and "メンテナンス" not in net.slack[0]["text"],
                  "a later お知らせ: only that one is posted")

            # A manual bid whose page can't load (www.lancers.jp is unreachable here) -> ❌ posted to the bid webhook
            net.slack.clear()
            r = await panel.eval('chrome.runtime.sendMessage({type: "bid", key: "lancers:5614153"})')
            s = await wait_state(panel, lambda s: s["jobs"]["lancers:5614153"].get("_bid", {}).get("status") == "failed" and net.slack, timeout=30)
            check(len(net.slack) == 1 and net.slack[0]["text"].startswith("❌ *提案に失敗しました*") and "完了予定日 2027-12-31" in net.slack[0]["text"],
                  f"bid result posted to the bid webhook ({net.slack[0]['text'][:80] if net.slack else 'nothing'}…)")

            # ---- Piggyback: a Lancers tab's own header requests feed the extension
            page_html = (b"<!doctype html><html><head><meta charset='utf-8'></head><body><header>Lancers</header><script>"
                         b"window.get = (p) => new Promise((r) => { const x = new XMLHttpRequest(); x.open('GET', p); x.onload = () => r(x.status); x.send(); });"
                         b"</script></body></html>")
            lancers_tab = cdp_json("/json/new?about:blank", method="PUT")
            async with websockets.connect(lancers_tab["webSocketDebuggerUrl"], max_size=None) as tab_net_ws, \
                       websockets.connect(lancers_tab["webSocketDebuggerUrl"], max_size=None) as tab_ws:
                tab_net = Interceptor(tab_net_ws)
                tab_net.lancers.update({"/mypage": (page_html, "text/html; charset=utf-8"),
                                        "/notification/count": {"important": 4, "message": 1, "search": 0}, "/todo_list/count": {"count": 2, "opened": True}})
                await tab_net.start()
                await asyncio.sleep(0.3)
                tab = Page(tab_ws)
                await tab_ws.send(json.dumps({"id": 0, "method": "Page.navigate", "params": {"url": "https://www.lancers.jp/mypage"}}))
                await asyncio.sleep(2)
                check(await tab.eval("window.__lancersAutoProposalHook === true && typeof window.get"), "the hook is in Lancers pages (page context)")
                check(await tab.eval("document.visibilityState") == "visible", "the Lancers tab is visible")

                net.slack.clear(); net.lancers_calls.clear()
                net.lancers["/notification/count"] = {"important": 4, "message": 1, "search": 0}
                net.lancers["/notification/header_important_list"]["notifications"].insert(0, {"id": 94, "content": "プロジェクトが開始されました", "link": "/y", "categoryName": "仕事", "modifiedText": "今"})
                await tab.eval("window.get('/notification/count')")          # the header's 30-second poll
                s = await wait_state(panel, lambda s: s["notify"].get("lastSource") == "Lancers tab" and net.slack, timeout=10)
                check(len(net.slack) == 1 and "プロジェクトが開始されました" in net.slack[0]["text"],
                      "a count from the Lancers tab's own poll -> the new お知らせ is forwarded")
                check("/notification/count" not in net.lancers_calls and net.lancers_calls == ["/notification/header_important_list"],
                      f"the extension didn't ask for the counts itself, only read the list that rose ({net.lancers_calls})")
                check(s["notify"]["lastSource"] == "Lancers tab", "status shows the update came from the open Lancers tab")

                net.lancers_calls.clear()
                r = await panel.eval('chrome.runtime.sendMessage({type: "checkNotifications", tick: true})')
                check(net.lancers_calls == [], "the once-a-minute check skips itself while a Lancers tab reports")

                # The user opens メッセージ in the header: remembered, not posted (the page is not trusted for content)
                net.slack.clear()
                tab_net.lancers["/notification/lists/message"] = {"results": [{"content": "ページが報告したメッセージ", "link": "/mypage/message/888", "time": 1793360000}]}
                await tab.eval("window.get('/notification/lists/message')")
                await tab.eval("window.postMessage({source: 'lancers-auto-proposal', path: '/notification/header_important_list', data: {notifications: [{id: 999, content: '偽物', link: '/z'}]}}, location.origin)")
                await asyncio.sleep(1.5)
                s = await state(panel)
                check(not net.slack and "/mypage/message/888#1793360000" in s["notify"]["seen"]["message"] and "n999" in s["notify"]["seen"]["important"],
                      "lists reported by the page are only remembered, never posted")
                tab_net.task.cancel()

            net.lancers_calls.clear()
            r = await panel.eval('chrome.runtime.sendMessage({type: "checkNotifications"})')
            check(set(net.lancers_calls) == {"/notification/count", "/todo_list/count"}, "Check now still asks Lancers directly")

            # ---- A proposal the user edits and submits by hand on Lancers: form -> confirmation -> completion page
            s = await state(panel)
            check(s["jobs"]["lancers:5614153"]["_bid"]["status"] == "failed", "the job is in Pending after its failed bid")
            html = lambda body: (f"<!doctype html><html><head><meta charset='utf-8'></head><body>{body}</body></html>".encode(), "text/html; charset=utf-8")
            net.slack.clear()
            form_tab = cdp_json("/json/new?about:blank", method="PUT")
            async with websockets.connect(form_tab["webSocketDebuggerUrl"], max_size=None) as f_net_ws, \
                       websockets.connect(form_tab["webSocketDebuggerUrl"], max_size=None) as f_ws:
                f_net = Interceptor(f_net_ws)
                f_net.lancers.update({
                    "/work/propose_start/5614153": html("<textarea name='description'>手直しした提案文です。</textarea><input name='end' value='2027年12月31日'>"
                                                        "<button id='go' onclick=\"location.href='/work/propose_confirm/5614153'\">内容を確認する</button>"),
                    "/work/propose_confirm/5614153": html("<p>手直しした提案文です。</p><button id='go' onclick=\"location.href='/work/propose_finish/88001'\">利用規約に同意して提案する</button>"),
                    "/work/propose_finish/88001": html("<h1>提案が完了しました</h1>"),
                    "/notification/count": {"important": 4, "message": 1, "search": 0}, "/todo_list/count": {"count": 2, "opened": True}})
                await f_net.start()
                await asyncio.sleep(0.3)
                f_tab = Page(f_ws)
                for path in ("/work/propose_start/5614153", "/work/propose_confirm/5614153"):
                    if path.endswith("start/5614153"):
                        await f_ws.send(json.dumps({"id": 0, "method": "Page.navigate", "params": {"url": f"https://www.lancers.jp{path}"}}))
                    await asyncio.sleep(1.5)
                    await f_tab.eval("document.getElementById('go').click()")
                s = await wait_state(panel, lambda s: s["jobs"]["lancers:5614153"]["_bid"]["status"] == "submitted", timeout=10)
                job = s["jobs"]["lancers:5614153"]
                check(job["_bid"]["status"] == "submitted" and job["_bid"]["step"] == "manual" and "lancers:5614153" not in [k for k in s["order"]
                      if s["jobs"][k].get("_draft") and s["jobs"][k]["_bid"].get("status") not in ("submitted", "running", "queued", "waiting")],
                      "submitted by hand: the completion page marks the job submitted (job found via the referrer), so it leaves Pending")
                await asyncio.sleep(1)
                bids = Store(DB)._all("SELECT status, step FROM bids WHERE job_id = 5614153 ORDER BY id")
                check([tuple(b) for b in bids][-1] == ("submitted", "manual"), f"...the server records it ({[tuple(b) for b in bids]})")
                check(any(p["text"].startswith("✅ *提案を送信しました*") for p in net.slack), "...and it is posted to Slack like any submitted bid")
                await f_ws.send(json.dumps({"id": 0, "method": "Page.reload", "params": {}}))
                await asyncio.sleep(1.5)
                check(len([p for p in net.slack if p["text"].startswith("✅")]) == 1, "reloading the completion page doesn't count it twice")
                f_net.task.cancel()

            # ---- Activity log: what the user did reaches the server, without secrets
            want = {"panel.open", "ui.setAutoBid", "ui.saveSettings", "bid.start", "bid.result", "notification.forwarded",
                    "proposal.form", "proposal.submitted", "connected", "ui.serverProfileSave"}
            end = time.time() + 15
            while time.time() < end and not want <= {r["kind"] for r in Store(DB).list_activity()}:
                await asyncio.sleep(0.5)
            rows = Store(DB).list_activity()
            kinds = {r["kind"] for r in rows}
            check(want <= kinds, f"user activity is logged on the server (missing: {sorted(want - kinds)})")
            dump = json.dumps([r["data"] for r in rows], ensure_ascii=False)
            check(hook not in dump and token not in dump and "feedfacecafebeef" not in dump
                  and any(r["kind"] == "ui.saveSettings" and r["data"].get("slack.bidWebhook") == "(changed)" for r in rows),
                  "no token, webhook URL or API key in the log (only that they changed)")
            submitted = [r for r in rows if r["kind"] == "proposal.submitted"][-1]
            form = [r for r in rows if r["kind"] == "proposal.form"][-1]
            check(submitted["job_id"] == 5614153 and submitted["data"]["message"] == "手直しした提案文です。" and submitted["data"]["matches_generated"] is False
                  and form["data"]["delivery_date"] == "2027年12月31日",
                  "the text the user actually submitted is logged, compared with the generated one")

            # ---- A too-short draft is never submitted, even by hand
            r = await panel.eval('chrome.runtime.sendMessage({type: "bid", key: "lancers:5600001"})')
            check(not r["ok"] and "not a real proposal" in r["error"], f"Bid on a non-proposal (a refusal / question back) is refused ({r.get('error')})")

            # ---- A new version on the server pauses the extension until it is updated
            with open(os.path.join(EXT_SERVER, "manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
            current = manifest["version"]
            manifest["version"] = current + ".1"
            with open(os.path.join(EXT_SERVER, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f)
            s = await wait_state(panel, lambda s: s.get("update"), timeout=10)
            check(s.get("update") == {"latest": current + ".1", "current": current}, f"the server's new version reaches the extension ({s.get('update')})")
            await asyncio.sleep(0.5)
            banner = await panel.eval('!document.getElementById("updateBanner").hidden && document.getElementById("updateText").textContent')
            check(bool(banner) and current + ".1" in banner, f"the side panel shows the update notice ({banner})")
            r = await panel.eval('chrome.runtime.sendMessage({type: "openPending", key: "lancers:5600001"})')
            r2 = await panel.eval('chrome.runtime.sendMessage({type: "setAutoBid", value: true})')
            check(not r["ok"] and "Update the extension" in r["error"] and not r2["ok"], "bidding and Auto-Bid are paused")
            r = await panel.eval('chrome.runtime.sendMessage({type: "downloadUpdate"})')
            z = zipfile.ZipFile(io.BytesIO(base64.b64decode(r.get("zip_b64") or "")))
            check(r["ok"] and r["version"] == current + ".1" and json.loads(z.read("lancers-proposal-extension/manifest.json"))["version"] == current + ".1"
                  and token not in z.read("lancers-proposal-extension/defaults.js").decode(),
                  "Download the update: the new version's zip, with no server token in it")

    finally:
        chrome.terminate()
        server.terminate()
        chrome.wait()
        server.wait()
    print("all good")


if __name__ == "__main__":
    mock = HTTPServer(("127.0.0.1", MOCK_PORT), MockOpenRouter)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    asyncio.run(main())

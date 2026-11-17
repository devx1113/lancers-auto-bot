"""New job -> Project Information -> each user's filters -> each user's proposal -> that user's extension.

The proposal page needs a login, so it is fetched through a connected extension (the fetch relay,
with the browser's own cookies) rather than from the server. One page fetch per job serves every
user; filters and proposals are per user. Proposals are written through the API, or by Grok in the
user's browser (GrokDrafters) when their saved model is Grok Free — new jobs included, so Auto-Bid
submits that text. A Redraft can also go to Grok while that checkbox is on.
"""

import asyncio
import itertools
import time
import uuid
from datetime import datetime

from . import filters, models, profiles, proposals, slack, webpages
from .details import parse_propose_page, propose_url
from .wire import client_id_of

# How long a job waits for a browser to connect before its Project Information is given up on
RELAY_WAIT_SECONDS = 600
RELAY_TIMEOUT_SECONDS = 45
# Pause between proposal-page fetches through one person's browser
FETCH_SPACING_SECONDS = 2
# Jobs first seen this recently are (re)processed when the server starts
STARTUP_LOOKBACK_SECONDS = 2 * 3600
# Proposals generated at the same time (OpenRouter calls)
GENERATE_CONCURRENCY = 3
# Alert after this many proposal pages in a row without the 依頼内容 box (Lancers changed the page?)
PARSE_ALERT_AFTER = 3
# How long Grok (in the Grok Drafter's browser) may take for one proposal
GROK_TIMEOUT_SECONDS = 240


def log(*args):
    print("[pipeline]", *args, flush=True)


class RelayError(Exception):
    pass


class Relay:
    """Asks a connected extension to fetch a page with its cookies and waits for the fetch-result"""

    def __init__(self, hub):
        self.hub = hub
        self.pending = {}
        self.turn = itertools.count()

    def candidates(self):
        # An extension that must update is not used until it has
        return [c for c in self.hub.clients if "fetch" in c.caps and not c.outdated]

    async def wait_for_candidate(self, timeout):
        end = time.monotonic() + timeout
        while not self.candidates():
            if time.monotonic() > end:
                return False
            await asyncio.sleep(5)
        return True

    async def fetch(self, url):
        clients = self.candidates()
        if not clients:
            raise RelayError("no extension with the fetch relay is connected")
        start = next(self.turn)
        errors = []
        # Round robin over the connected browsers; a failure moves on to the next one
        for i in range(min(3, len(clients))):
            client = clients[(start + i) % len(clients)]
            request_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            client.send({"type": "fetch", "id": request_id, "url": url, "credentials": "include",
                         "timeout_ms": (RELAY_TIMEOUT_SECONDS - 5) * 1000})
            try:
                result = await asyncio.wait_for(future, RELAY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                errors.append(f"{client.user['name']}: timed out")
                continue
            finally:
                self.pending.pop(request_id, None)
            if result.get("ok"):
                return result
            errors.append(f"{client.user['name']}: {result.get('error')}")
        raise RelayError("; ".join(errors))

    def resolve(self, msg):
        future = self.pending.get(msg.get("id"))
        if future and not future.done():
            future.set_result(msg)


class DraftError(Exception):
    pass


class GrokDrafters:
    """The Grok Drafter extensions connected for each user (in browsers they keep logged in to grok.com;
    a user may have several devices): each draft is one new Grok conversation, started only when the
    user asks (↻, or the Test tab), on the device they chose (or any connected one)"""

    def __init__(self):
        self.conns = {}          # user id -> {device id (the drafter's client id): connection}
        self.pending = {}        # request id -> (future, connection it was sent to)
        self.locks = {}          # connection -> lock: one conversation at a time in that browser

    def count(self):
        return sum(len(devices) for devices in self.conns.values())

    def all_conns(self):
        return [conn for devices in self.conns.values() for conn in devices.values()]

    def connected_ids(self, user_id):
        return set(self.conns.get(user_id, {}))

    def pick(self, user_id, device=None):
        """The chosen device's connection ("" / None: the one connected most recently), or None"""
        devices = self.conns.get(user_id, {})
        if device:
            return devices.get(device)
        return list(devices.values())[-1] if devices else None

    def connected(self, user_id, device=None):
        return self.pick(user_id, device) is not None

    def add(self, user_id, device, conn):
        devices = self.conns.setdefault(user_id, {})
        previous = devices.pop(device, None)       # the same device reconnecting
        devices[device] = conn
        return previous

    def remove(self, user_id, device, conn):
        devices = self.conns.get(user_id, {})
        if devices.get(device) is conn:
            del devices[device]
            if not devices:
                self.conns.pop(user_id, None)
        self.locks.pop(conn, None)

    async def draft(self, user_id, text, device=None, timeout=GROK_TIMEOUT_SECONDS):
        """Grok's answer to one chat message, on the chosen device; raises DraftError"""
        conn = self.pick(user_id, device)
        if not conn:
            raise DraftError("the chosen Grok Free device is not connected" if device
                             else "no Grok Drafter is connected (a browser logged in to grok.com)")
        async with self.locks.setdefault(conn, asyncio.Lock()):
            request_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = (future, conn)
            conn.send({"type": "grok.draft", "id": request_id, "text": text, "timeout_ms": (timeout - 10) * 1000})
            try:
                result = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise DraftError(f"Grok did not answer within {timeout} s")
            finally:
                self.pending.pop(request_id, None)
        if not result.get("ok"):
            raise DraftError(result.get("error") or "Grok Drafter failed")
        return result.get("text") or ""

    def resolve(self, msg):
        future, _ = self.pending.get(msg.get("id"), (None, None))
        if future and not future.done():
            future.set_result(msg)

    def disconnected(self, conn):
        """A drafter went away: its unfinished drafts fail now instead of at their timeout"""
        for future, sent_to in list(self.pending.values()):
            if sent_to is conn and not future.done():
                future.set_result({"ok": False, "error": "Grok Drafter disconnected during the draft (the extension was reloaded or the browser closed)"})


class Pipeline:
    def __init__(self, store, hub, relay, grok=None):
        self.store = store
        self.hub = hub
        self.relay = relay
        self.grok = grok or GrokDrafters()
        self.queue = asyncio.Queue()
        self.fetch_lock = asyncio.Lock()
        self.generate_slots = asyncio.Semaphore(GENERATE_CONCURRENCY)
        self.running = set()          # job ids being processed (a job is never processed twice at once)
        self.parse_failures = 0
        self.last_login_alert = 0

    def submit(self, job_id, users=None, refetch=False, regenerate=False, source=None):
        """source "grok": the proposal is written by the user's Grok Drafter"""
        self.queue.put_nowait((job_id, users, refetch, regenerate, source))

    async def run(self):
        for job_id in self.store.recent_jobs_without_details(int(time.time()) - STARTUP_LOOKBACK_SECONDS):
            self.submit(job_id)
        while True:
            item = await self.queue.get()
            if item[0] in self.running:
                # Already in progress: try again once it has finished
                asyncio.get_running_loop().call_later(3, self.queue.put_nowait, item)
                continue
            asyncio.create_task(self.process(*item))

    async def process(self, job_id, users=None, refetch=False, regenerate=False, source=None):
        self.running.add(job_id)
        try:
            job, _ = self.store.get_job(job_id)
            if not job:
                return
            users = users or self.store.active_users()
            if not users:
                return    # nobody to read or write for yet
            details = self.store.get_details(job_id)
            if refetch or not details or details["status"] not in ("ok", "skipped"):
                # Fetch only for someone the card alone doesn't already rule out (コンペ, blocked client)
                if any(self.card_decision(user, job)["allowed"] for user in users):
                    details = await self.fetch_details(job_id)
                elif not details:
                    self.store.save_details(job_id, "skipped", error="excluded by every user's filters")
                    details = self.store.get_details(job_id)
            await asyncio.gather(*(self.decide(user, job, details, regenerate, source) for user in users))
        except Exception as e:
            log(f"job {job_id} failed:", repr(e))
        finally:
            self.running.discard(job_id)

    def card_decision(self, user, job):
        profile = self.store.get_profile(user["id"])
        blocked = self.store.is_blocked(user["id"], client_id_of(job.get("client_url")), job.get("client_name"))
        return filters.evaluate(job, None, profile["filters"], blocked)

    async def fetch_details(self, job_id):
        async with self.fetch_lock:
            if not await self.relay.wait_for_candidate(RELAY_WAIT_SECONDS):
                self.store.save_details(job_id, "error", error="no browser connected to read the proposal page")
                return self.store.get_details(job_id)
            try:
                result = await self.relay.fetch(propose_url(job_id))
                parsed = parse_propose_page(result.get("html") or "", result.get("url") or "")
            except RelayError as e:
                log(f"job {job_id}: relay failed: {e}")
                self.store.save_details(job_id, "error", error=f"relay: {e}")
                return self.store.get_details(job_id)
            finally:
                await asyncio.sleep(FETCH_SPACING_SECONDS)
        self.store.save_details(job_id, parsed["status"], parsed["info"], parsed["error"])
        log(f"job {job_id}: proposal page {parsed['status']}{' — ' + parsed['error'] if parsed['error'] else ''}")
        if (parsed["error"] or "").startswith("提案できません"):
            self.store.close_job(job_id, parsed["error"])
        await self.watch_parsing(job_id, parsed)
        return self.store.get_details(job_id)

    async def watch_parsing(self, job_id, parsed):
        if parsed["status"] == "login":
            if time.time() - self.last_login_alert > 3600:
                self.last_login_alert = time.time()
                await asyncio.to_thread(slack.alert, "The browser fetching Lancers proposal pages is logged out — log in to Lancers in that browser.")
        elif parsed["status"] == "ok" and not parsed["info"]["missing"]:
            self.parse_failures = 0
        else:
            self.parse_failures += 1
            if self.parse_failures == PARSE_ALERT_AFTER:
                why = parsed["error"] or f"missing {', '.join(parsed['info']['missing'])}"
                await asyncio.to_thread(slack.alert, f"{PARSE_ALERT_AFTER} Lancers proposal pages in a row could not be read "
                                                     f"(last: job {job_id}, {why}). The page layout may have changed.")

    async def decide(self, user, job, details, regenerate=False, source=None):
        """Filters, then the proposal, for one user; ends with an "updated" event for that user"""
        user_id = user["id"]
        profile = self.store.get_profile(user_id)
        info = details["info"] if details and details["status"] == "ok" else None
        decision = self.evaluate(user_id, profile, job, info)
        existing = self.store.get_proposal(user_id, job["id"])

        if decision["allowed"] and info is None:
            why = (details or {}).get("error") or (details or {}).get("status") or "not fetched"
            decision = {"allowed": False, "blocked": False,
                        "reasons": [why if why.startswith("提案できません") else f"依頼内容を取得できませんでした（{why}）"]}
        if not decision["allowed"]:
            if regenerate:
                log(f"job {job['id']} / {user['name']}: no Redraft — {decision['reasons'][0]}")
            elif existing and existing["status"] == "skipped" and existing["reasons"] == decision["reasons"]:
                return    # re-checked (filters changed), still skipped for the same reasons
            # A Redraft always answers: the card then shows why there is no proposal
            self.store.save_proposal(user_id, job["id"], "skipped", decision)
        elif existing and existing["status"] == "ready" and not regenerate:
            return        # nothing changed for this user
        elif profiles.setup_problem(profile):
            self.store.save_proposal(user_id, job["id"], "no_profile", decision, reasons=[profiles.setup_problem(profile)])
        elif self.store.proposals_today(user_id) >= profiles.daily_limit(profile):
            self.store.save_proposal(user_id, job["id"], "error", decision,
                                     reasons=[f"本日の提案文生成の上限（{profiles.daily_limit(profile)}件）に達しました"])
        else:
            # A first proposal (not a ↻) can open the form now: 完了予定日 and the contract do not
            # need the text, so the page load overlaps the writing.
            if source is None:
                self.preload_form(user, job, info)
            await self.write_proposal(user, profile, job, info, decision, source)
        self.store.add_event(job["id"], "updated", user_id)

    def evaluate(self, user_id, profile, job, info):
        blocked = self.store.is_blocked(user_id, client_id_of(job.get("client_url")),
                                        (info or {}).get("client_name") or job.get("client_name"))
        return filters.evaluate(job, info, profile["filters"], blocked)

    def preload_form(self, user, job, info):
        """Tell this user's extension to open the proposal form before the text exists.

        Only when a bid could still be automatic: a 希望納期 that is not in the past, and no
        required attachment. The extension submits only after the text arrives. A short answer or
        a later hold closes that tab and does not count as a bid.
        """
        delivery = info.get("desired_delivery")
        if not delivery or info.get("attachments_required") or delivery < datetime.now().strftime("%Y-%m-%d"):
            return
        msg = {"type": "bid-prepare", "key": f"lancers:{job['id']}", "delivery_date": delivery,
               "apply_url": propose_url(job["id"])}
        sent = 0
        for client in list(self.hub.clients):
            if client.user["id"] == user["id"] and not client.outdated:
                client.send(msg)
                sent += 1
        if sent:
            log(f"job {job['id']} / {user['name']}: opening the proposal form while the text is written ({delivery})")

    async def write_proposal(self, user, profile, job, info, decision, source=None):
        # The saved model "grok-free" writes every proposal (new jobs included, so Auto-Bid submits
        # Grok's text). source "grok" is a ↻ while that checkbox is on and the device is connected.
        via_grok = source == "grok" or profile.get("model") == profiles.GROK_FREE_MODEL
        try:
            # The links in the 依頼内容, and the user's portfolio (cached; a stale page is read again first)
            links = webpages.job_links(info)
            linked = await asyncio.to_thread(webpages.read_all, self.store, links, max_age=webpages.JOB_REFRESH_SECONDS)
            pages = await asyncio.to_thread(webpages.read_all, self.store, [e["url"] for e in profile["portfolio"]])
            context = "\n\n".join(filter(None, [webpages.job_links_block(links, linked),
                                                webpages.portfolio_block(profile["portfolio"], pages)]))
            if via_grok:
                started = time.monotonic()
                text = await self.grok.draft(user["id"], proposals.grok_message(profile["prompt"], job, info, context),
                                             profile.get("grok_device"))
                message, model, tokens = proposals.finish(text), "grok-web", None
                log(f"job {job['id']} / {user['name']}: Grok answered in {time.monotonic() - started:.0f} s")
            else:
                api_key = profiles.decrypt_key(profile["api_key_enc"])
                model = profile["model"] or proposals.DEFAULT_MODEL
                listed = models.info(model) or {}
                async with self.generate_slots:
                    message, model, tokens = await asyncio.to_thread(
                        proposals.generate, api_key, model, profile["prompt"], job, info,
                        profile.get("reasoning_effort"), profile.get("speed"), listed,
                        bool(profile.get("web_search")), context)
        except (profiles.ProfileError, proposals.ProposalError, DraftError) as e:
            log(f"job {job['id']} / {user['name']}: proposal failed: {e}")
            where = "Grokでの" if via_grok else ""
            self.store.save_proposal(user["id"], job["id"], "error", decision, reasons=[f"{where}提案文の生成に失敗: {e}"])
            return
        # The filters (or block list) may have changed while the proposal was being written
        latest = self.evaluate(user["id"], self.store.get_profile(user["id"]), job, info)
        if not latest["allowed"]:
            log(f"job {job['id']} / {user['name']}: proposal dropped — now excluded ({latest['reasons'][0]})")
            self.store.save_proposal(user["id"], job["id"], "skipped", latest)
            return
        delivery = info.get("desired_delivery")
        hold = []
        if not delivery:
            hold.append("希望納期の指定がないため自動送信しません（完了予定日を入力して送信してください）")
        elif delivery < datetime.now().strftime("%Y-%m-%d"):
            hold.append(f"希望納期（{delivery}）が過ぎているため自動送信しません")
            delivery = None
        if info.get("attachments_required"):
            hold.append("添付ファイルが必須のため自動送信しません")
        if len(message) < proposals.MIN_CHARS:
            hold.append(f"提案文が短すぎるため（{len(message)}文字）自動送信しません。内容を確認して書き直すか再生成してください")
        self.store.save_proposal(user["id"], job["id"], "ready", decision, message=message, delivery_date=delivery,
                                 auto_submit=not hold, reasons=hold, model=model, tokens=tokens)
        read = f", links read {sum(p['status'] == 'ok' for p in linked.values())}/{len(links)}" if links else ""
        log(f"job {job['id']} / {user['name']}: proposal ready ({len(message)} chars, {model}{read}){' — held: ' + hold[0] if hold else ''}")

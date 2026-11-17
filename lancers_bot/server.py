"""WebSocket server for the Lancers Auto-Proposal extension (../lancers-proposal-extension).

Single user: every connection must carry the owner's password (manage.py set-password). The extension
sends it as ?token= (its Token setting holds the password); ?password= is accepted too. A wrong one is
refused with 4401, an address with too many wrong ones is locked out (auth.Lockout), and connections
opened with a password that has since been changed are closed.

Speaks the protocol the extension already uses: connect to /ws?token&since&client&caps,
receive hello + the missed events, then live events; ping every 20 s, the client acks seqs.
The scraper writes jobs and "new" events to SQLite; one hub task polls for new events, starts the
pipeline (Project Information -> filters -> proposals) for new jobs and fans events out per user.

Extension -> server messages: pong, ack, fetch-result (relay), block-client, refresh, bid-result,
profile.get, profile.update, profile.test-key, models.list, activity, extension.download (these
answer with the same req_id).

Extensions send their version (?ver=); one older than EXTENSION_DIR's manifest is told to update
(hello.update / an "update" message) and gets nothing else until it has: no jobs, no relay fetches.

The Grok Drafter extension (?kind=grok-drafter, ../grok-drafter-extension) is a different client: only
for users on the Grok Free list (manage.py grok-free), from this machine (ws://127.0.0.1) or through
nginx (wss://), outside the version check. It gets no jobs; it is sent grok.draft (one chat message)
when that user's saved model is Grok Free (every proposal, including new jobs), when they Redraft
with "Grok Free" on, or when they test Grok Free in the Test tab, and answers grok.result.
"""

import asyncio
import base64
import json
import time

import requests
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from . import auth, extension, models, profiles, proposals, slack, webpages
from .details import cannot_propose
from .pipeline import DraftError, GrokDrafters, Pipeline, Relay
from .store import Store
from .wire import CATEGORIES, client_id_of, to_wire

POLL_SECONDS = 0.5
PING_SECONDS = 20
# An extension connecting fresh (since=0) or after a long gap gets at most this many past events
REPLAY_LIMIT = 300
# Blocking a client re-checks that client's jobs from this far back
BLOCK_LOOKBACK_SECONDS = 14 * 86400
# Changing filters re-checks the jobs of this long ago (skipped ones only from the shorter window,
# since one that now passes gets a proposal written)
FILTER_RECHECK_SECONDS = 2 * 86400
FILTER_RECHECK_SKIPPED_SECONDS = 3 * 3600
# What one activity message may carry
ACTIVITY_BATCH = 200
ACTIVITY_DATA_BYTES = 20000
# Messages an extension that must update may still send
OUTDATED_ALLOWED = {"pong", "ack", "activity", "extension.download"}

store = Store()
lockout = auth.Lockout()
# How often the hub looks for a password change (to close connections that used the old one)
PASSWORD_CHECK_SECONDS = 5


def log(*args):
    print("[server]", *args, flush=True)


def job_id_of_key(key):
    try:
        site, job_id = str(key).split(":", 1)
        return int(job_id) if site == "lancers" else None
    except ValueError:
        return None


def event_message(row, user_id):
    """One event as one user sees it: with their decision and proposal"""
    job_id = row["id"]
    return {"type": "event", "seq": row["seq"], "event": row["event"],
            "job": to_wire(job_id, row["data"], row["first_seen_ts"], store.get_details(job_id), store.get_proposal(user_id, job_id),
                           store.submitted_bid(user_id, job_id))}


class Client:
    def __init__(self, websocket, user, client_id, caps, version, password_version=None):
        self.websocket = websocket
        self.user = user
        self.client_id = client_id
        self.caps = set(filter(None, caps.split(",")))
        self.version = version
        self.outdated = extension.is_outdated(version)
        self.queue = asyncio.Queue()
        # The password version this connection logged in with (store.password_version)
        self.password_version = password_version

    def send(self, msg):
        self.queue.put_nowait(msg)


class Hub:
    """Polls the events table, starts the pipeline for new jobs and pushes each event to the clients it is for"""

    def __init__(self):
        self.clients = set()
        self.last_seq = store.latest_seq()
        self.version = extension.latest_version()

    def check_version(self):
        """The extension version on the server changed: every older one connected is told to update
        (and one that is current again, after a version was taken back, carries on)"""
        version = extension.latest_version()
        if version == self.version:
            return
        self.version = version
        log(f"extension version is now {version}")
        for client in self.clients:
            outdated = extension.is_outdated(client.version)
            if outdated != client.outdated:
                client.outdated = outdated
                client.send(update_message(client))

    def check_password(self):
        """The password was changed (manage.py set-password): close every connection that used the old one"""
        current = store.password_version()
        stale = [c for c in list(self.clients) + grok.all_conns() if c.password_version != current]
        for conn in stale:
            conn.password_version = current        # closed once
            asyncio.create_task(close_quietly(conn.websocket, 4401, "the password was changed"))
        if stale:
            log(f"password changed: closed {len(stale)} connection(s) that used the old one")

    async def run(self):
        polls = 0
        while True:
            await asyncio.sleep(POLL_SECONDS)
            polls += 1
            try:
                if polls % int(PASSWORD_CHECK_SECONDS / POLL_SECONDS) == 0:
                    self.check_password()
                self.check_version()
                latest = store.latest_seq()
                if latest <= self.last_seq:
                    continue
                rows = store.events_between(self.last_seq, latest)
                self.last_seq = latest
                for row in rows:
                    if row["event"] == "new" and row["user_id"] is None:
                        pipeline.submit(row["id"])
                    for client in self.clients:
                        if not client.outdated and row["user_id"] in (None, client.user["id"]):
                            client.send(event_message(row, client.user["id"]))
            except Exception as e:
                log("hub poll failed:", repr(e))


hub = Hub()
relay = Relay(hub)
grok = GrokDrafters()
pipeline = Pipeline(store, hub, relay, grok)


@asynccontextmanager
async def lifespan(app):
    store.prune_activity()
    tasks = [asyncio.create_task(hub.run()), asyncio.create_task(pipeline.run()),
             # Load OpenRouter's model list once, so profile saves can be checked against it
             asyncio.create_task(asyncio.to_thread(models.list_models))]
    log(f"ready; latest seq {hub.last_seq}")
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "latest_seq": hub.last_seq, "clients": len(hub.clients),
            "relay_clients": len(relay.candidates()), "pipeline_queue": pipeline.queue.qsize(), "grok_drafters": grok.count()}


def update_message(conn):
    return {"type": "update", "required": conn.outdated, "latest": extension.latest_version(), "current": conn.version}


async def close_quietly(websocket, code, reason):
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass      # already gone


async def authenticate(websocket, password):
    """The owner if the password is right; otherwise closes the connection (4401) and returns None.

    The extension stops reconnecting on 4401 and shows the reason as "server rejected the token"."""
    address = websocket.client.host if websocket.client else "?"
    if lockout.is_locked(address):
        await close_quietly(websocket, 4401, "too many wrong passwords from this address — try again later")
        return None
    version = store.password_version()
    if not version:
        log(f"refused {address}: no password is set yet (python manage.py set-password)")
        await close_quietly(websocket, 4401, "the server has no password yet")
        return None
    # scrypt takes ~0.1 s: off the event loop
    user = await asyncio.to_thread(store.check_password, password)
    if user:
        lockout.succeeded(address)
        return user, version
    locked_now = lockout.failed(address)
    log(f"wrong password from {address}{' — locked out for ' + str(auth.LOCKOUT_SECONDS // 60) + ' min' if locked_now else ''}")
    if locked_now:
        await asyncio.to_thread(slack.alert, f"{auth.FAIL_LIMIT} wrong passwords in a row from {address}: that address is locked "
                                             f"out for {auth.LOCKOUT_SECONDS // 60} minutes. If it wasn't you, someone is guessing.")
    await asyncio.sleep(auth.FAIL_DELAY_SECONDS)
    await close_quietly(websocket, 4401, "wrong password")
    return None


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = "", password: str = "", since: int = 0, client: str = "", caps: str = "",
                      ver: str = "", kind: str = "", device: str = ""):
    await websocket.accept()
    # The extension's Token setting holds the owner's password
    login = await authenticate(websocket, password or token)
    if not login:
        return
    user, password_version = login
    if kind == "grok-drafter":
        await drafter_session(websocket, user, client, ver, device, password_version)
        return
    if not ver and extension.latest_version():
        # Older than 1.5.0, which can't show an update notice: it shows this reason as its status instead
        await websocket.close(code=4426, reason=f"update the extension: v{extension.latest_version()} is required")
        log(f"refused {user['name']} ({client or '?'}): extension too old to say its version")
        return

    # Snapshot the cursor and subscribe with no await in between, so no event is missed or sent twice
    conn = Client(websocket, user, client, caps, ver, password_version)
    upto = hub.last_seq
    hub.clients.add(conn)
    who = f"{user['name']} ({client or '?'}, v{ver or '?'})"
    log(f"connected: {who}, since {since}, caps {caps or '-'}{' — must update to v' + str(extension.latest_version()) if conn.outdated else ''}")
    try:
        if since > upto:
            await websocket.send_json({"type": "error", "message": "the server's event history was reset; use Reset cursor in the extension"})
        replay = store.events_between(since, upto, limit=REPLAY_LIMIT, user_id=user["id"]) if since < upto and not conn.outdated else []
        await websocket.send_json({"type": "hello", "categories": CATEGORIES, "sites": ["lancers"],
                                   "replay": len(replay), "latest_seq": upto, "relay_wanted": True,
                                   "features": ["server-proposals", "profile", "bid-result", "activity", "update"],
                                   "user": user["name"], "profile": public_profile(user["id"]),
                                   "version": {"latest": extension.latest_version()},
                                   "update": update_message(conn) if conn.outdated else None})
        for row in replay:
            await websocket.send_json(event_message(row, user["id"]))

        sender = asyncio.create_task(send_loop(conn))
        try:
            await receive_loop(conn)
        finally:
            sender.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log(f"connection error ({who}):", repr(e))
    finally:
        hub.clients.discard(conn)
        log(f"disconnected: {who}")


async def drafter_session(websocket, user, client_id, version, device_name, password_version=None):
    """A Grok Drafter (one of the owner's devices): sent grok.draft when asked, answers grok.result"""
    device_name = device_name.strip()[:60] or client_id
    who = f"{user['name']} ({device_name}, Grok Drafter v{version or '?'})"
    if not user["grok_free"]:
        log(f"refused Grok Drafter for {who}: Grok Free is off (manage.py grok-free on)")
        await websocket.close(code=4403, reason="Grok Drafter is not enabled for this user")
        return
    conn = Client(websocket, user, client_id, "", version, password_version)
    conn.outdated = False
    if not client_id:
        await websocket.close(code=4400, reason="no client id")
        return
    store.save_grok_device(user["id"], client_id, device_name)
    previous = grok.add(user["id"], client_id, conn)
    log(f"connected: {who}{' (replaces the previous one)' if previous else ''}")
    push_profile(user)      # their Lancers extension shows the drafter as connected
    try:
        await websocket.send_json({"type": "hello", "kind": "grok-drafter", "user": user["name"]})
        sender = asyncio.create_task(send_loop(conn))
        try:
            while True:
                try:
                    msg = json.loads(await websocket.receive_text())
                except (ValueError, TypeError):
                    continue
                if msg.get("type") == "grok.result":
                    grok.resolve(msg)
                    outcome = "ok" if msg.get("ok") else f"failed — {str(msg.get('error'))[:200]}"
                    took = f" ({msg['seconds']} s{', ' + msg['via'] if msg.get('via') else ''})" if msg.get("seconds") else ""
                    log(f"{who}: draft {outcome}{took}")
        finally:
            sender.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log(f"connection error ({who}):", repr(e))
    finally:
        grok.remove(user["id"], client_id, conn)
        store.save_grok_device(user["id"], client_id, device_name)       # last seen
        grok.disconnected(conn)
        push_profile(user)
        log(f"disconnected: {who}")


async def send_loop(conn):
    while True:
        try:
            msg = await asyncio.wait_for(conn.queue.get(), timeout=PING_SECONDS)
        except asyncio.TimeoutError:
            msg = {"type": "ping"}
        await conn.websocket.send_json(msg)


async def receive_loop(conn):
    while True:
        try:
            msg = json.loads(await conn.websocket.receive_text())
        except (ValueError, TypeError):
            continue
        kind = msg.get("type")
        if conn.outdated and kind not in OUTDATED_ALLOWED:
            conn.send({"type": "error", "req_id": msg.get("req_id"), "ok": False,
                       "message": f"update the extension to v{extension.latest_version()} first"})
            continue
        try:
            if kind in ("pong", "ack"):
                # The extension resends `since` on every reconnect, so acks need no bookkeeping
                continue
            if kind == "fetch-result":
                relay.resolve(msg)
            elif kind == "block-client":
                await block_client(conn, msg)
            elif kind == "refresh":
                await refresh_job(conn, msg)
            elif kind == "bid-result":
                await bid_result(conn, msg)
            elif kind in ("profile.get", "profile.update", "profile.test-key"):
                await profile_message(conn, msg)
            elif kind == "models.list":
                result = await asyncio.to_thread(models.list_models, bool(msg.get("force")))
                conn.send({"type": "models", "req_id": msg.get("req_id"), "ok": bool(result["models"]), **result})
            elif kind == "test.generate":
                # Can take a minute or more: answered from its own task, so other messages aren't held up
                asyncio.create_task(test_generate(conn, msg))
            elif kind == "activity":
                saved = save_activity(conn, msg.get("events"))
                conn.send({"type": "activity", "req_id": msg.get("req_id"), "ok": True, "saved": saved})
            elif kind == "extension.download":
                version, data = await asyncio.to_thread(extension.build_zip)
                conn.send({"type": "extension", "req_id": msg.get("req_id"), "ok": bool(version), "version": version,
                           "filename": f"lancers-proposal-extension-{version}.zip", "zip_b64": base64.b64encode(data).decode()})
                log(f"{conn.user['name']} downloaded extension v{version}")
            else:
                log(f"unknown message from {conn.user['name']}: {kind}")
        except Exception as e:
            log(f"{kind} from {conn.user['name']} failed:", repr(e))
            conn.send({"type": "error", "message": f"{kind} failed: {e}"})


def save_activity(conn, events):
    """What the extension logged (it keeps its queue until this answers)"""
    rows = []
    for e in (events or [])[:ACTIVITY_BATCH]:
        if not isinstance(e, dict) or not isinstance(e.get("kind"), str) or not isinstance(e.get("ts"), (int, float)):
            continue
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if len(json.dumps(data, ensure_ascii=False)) > ACTIVITY_DATA_BYTES:
            data = {"truncated": True, "keys": sorted(data)[:50]}
        job_id = e.get("job_id")
        rows.append({"ts": int(e["ts"] / 1000) if e["ts"] > 1e11 else int(e["ts"]), "kind": e["kind"][:60],
                     "job_id": job_id if isinstance(job_id, int) else None, "data": data})
    if rows:
        store.add_activity(conn.user["id"], conn.client_id, conn.version, rows)
    return len(rows)


def recheck_after_filter_change(user):
    """New filters apply to the jobs already decided: proposals that are now excluded leave Pending,
    and recently skipped jobs that now pass get a proposal"""
    now = int(time.time())
    submitted = store.submitted_job_ids(user["id"])
    jobs = [j for status in ("ready", "no_profile", "error")
            for j in store.proposals_with_status(user["id"], status, now - FILTER_RECHECK_SECONDS)]
    jobs += store.proposals_with_status(user["id"], "skipped", now - FILTER_RECHECK_SKIPPED_SECONDS)
    for job_id in dict.fromkeys(jobs):
        if job_id not in submitted:
            details = store.get_details(job_id)
            pipeline.submit(job_id, users=[user], refetch=bool(details and details["status"] == "skipped"))
    return len(jobs)


async def block_client(conn, msg):
    """⛔ on a card: this user never bids on the client again; their open jobs are re-decided"""
    reply = {"type": "block-client-result", "key": msg.get("key"), "site": "lancers"}
    job_id = job_id_of_key(msg.get("key"))
    job, _ = store.get_job(job_id) if job_id else (None, None)
    if not job:
        return conn.send({**reply, "ok": False, "error": "unknown job"})
    client_id = client_id_of(job.get("client_url"))
    details = store.get_details(job_id) or {}
    client_name = ((details.get("info") or {}).get("client_name")) or job.get("client_name")
    if not client_id and not client_name:
        return conn.send({**reply, "ok": False, "error": "this job carries no client"})
    added = store.add_block(conn.user["id"], client_id, None if client_id else client_name, note=f"from job {job_id}")
    affected = []
    for other_id in store.recent_job_ids(int(time.time()) - BLOCK_LOOKBACK_SECONDS):
        other, _ = store.get_job(other_id)
        if other and ((client_id and client_id_of(other.get("client_url")) == client_id)
                      or (not client_id and other.get("client_name") == client_name)):
            affected.append(other_id)
            pipeline.submit(other_id, users=[conn.user])
    log(f"{conn.user['name']} blocked client {client_id or client_name} ({len(affected)} job(s) re-checked)")
    conn.send({**reply, "ok": True, "added": added, "client_id": client_id or client_name, "updated_jobs": len(affected)})


def uses_grok_free(user):
    """Grok Free on, and the chosen device (or any, if none chosen) connected"""
    current = store.user_by_id(user["id"])
    profile = store.get_profile(user["id"])
    return bool(current and current["grok_free"] and profile.get("use_grok_free")
                and grok.connected(user["id"], profile.get("grok_device")))


async def test_generate(conn, msg):
    """The Test tab: a proposal for pasted project details with a given prompt and model; nothing is saved"""
    reply = {"type": "test.result", "req_id": msg.get("req_id")}
    user_id, model = conn.user["id"], str(msg.get("model") or "")
    started = time.monotonic()
    try:
        prompt, details = str(msg.get("prompt") or "")[:20000], str(msg.get("details") or "")[:20000]
        if model == "grok-free":
            current = store.user_by_id(user_id)
            if not (current and current["grok_free"]):
                raise profiles.ProfileError("Grok Free is not enabled for this user")
            system, user_text = proposals.test_input(prompt, details, "grok")
            device = store.get_profile(user_id).get("grok_device")
            text, used, tokens = proposals.finish(await grok.draft(user_id, f"{system}\n\n{user_text}", device)), "grok-web", None
        else:
            api_key = profiles.decrypt_key(store.get_profile(user_id)["api_key_enc"])
            if not api_key:
                raise profiles.ProfileError("save your API key in Settings → Your profile first")
            if not models.allowed_id(model):
                raise profiles.ProfileError("pick a Claude, Grok or OpenAI model")
            if proposals.provider_of(api_key) == "openai" and not model.startswith("openai/"):
                raise profiles.ProfileError("with an OpenAI key only OpenAI models can be used")
            effort = str(msg.get("reasoning_effort") or "") or None
            speed = str(msg.get("speed") or "normal")
            if speed not in models.SPEEDS:
                raise profiles.ProfileError(f"speed must be one of {', '.join(models.SPEEDS)}")
            if proposals.provider_of(api_key) == "openai" and speed == "latency":
                raise profiles.ProfileError("Low latency is an OpenRouter option — with an OpenAI key choose Normal or Fast (priority)")
            listed = models.info(model)
            if effort and effort not in models.EFFORTS:
                raise profiles.ProfileError(f"reasoning effort must be one of {', '.join(models.EFFORTS)}")
            if effort and listed is not None and effort not in listed["efforts"]:
                raise profiles.ProfileError(f"{model} does not take reasoning effort '{effort}'"
                                           + (f" (it takes: {', '.join(listed['efforts'])})" if listed["efforts"] else " (no reasoning setting)"))
            system, user_text = proposals.test_input(prompt, details)
            async with pipeline.generate_slots:
                text, used, tokens = await asyncio.to_thread(
                    proposals.generate_text, api_key, model, system, user_text, effort,
                    None if speed == "normal" else speed, listed or {})
        seconds = round(time.monotonic() - started, 1)
        conn.send({**reply, "ok": True, "text": text, "model": used, "tokens": tokens, "seconds": seconds})
        log(f"{conn.user['name']}: test with {model} — {len(text)} chars in {seconds} s")
    except (profiles.ProfileError, proposals.ProposalError, DraftError) as e:
        conn.send({**reply, "ok": False, "error": str(e)})
        log(f"{conn.user['name']}: test with {model} failed: {e}")


async def refresh_job(conn, msg):
    """↻ Re-check / Redraft: read the proposal page again and write a fresh proposal for this user.
    Grok writes it when the saved model is Grok Free, or when ↻ Redraft with Grok Free is on and that
    device is connected. With a req_id, the request is answered (refresh-result / error)."""
    job_id = job_id_of_key(msg.get("key"))
    fail = lambda error: conn.send({"type": "error", "req_id": msg.get("req_id"), "message": error})
    if not job_id or not store.get_job(job_id)[0]:
        return fail("re-check: unknown job")
    user_id = conn.user["id"]
    profile = store.get_profile(user_id)
    if profile.get("model") == profiles.GROK_FREE_MODEL:
        source = "grok"
    else:
        # Checkbox on, and the chosen device connected: Grok writes this redraft
        source = "grok" if uses_grok_free(conn.user) else None
        if not source and profile.get("use_grok_free"):
            log(f"job {job_id} / {conn.user['name']}: Grok Free is on but its device is not connected — the API writes it")
    pipeline.submit(job_id, users=[conn.user], refetch=True, regenerate=True, source=source)
    if source:
        log(f"job {job_id} / {conn.user['name']}: {'Grok Free' if profile.get('model') == profiles.GROK_FREE_MODEL else 'Redraft with Grok'}")
    if msg.get("req_id"):
        conn.send({"type": "refresh-result", "req_id": msg["req_id"], "ok": True, "source": source or "api"})


async def bid_result(conn, msg):
    job_id = job_id_of_key(msg.get("key"))
    if not job_id:
        return
    status, step, message = str(msg.get("status") or ""), msg.get("step"), str(msg.get("message") or "")[:500]
    store.save_bid(conn.user["id"], job_id, status, step, message)
    # Lancers showed 提案できません (the client left, the job was withdrawn): closed for every user
    reason = cannot_propose(message) if status != "submitted" else None
    if reason and store.close_job(job_id, reason):
        store.add_event(job_id, "updated")
        log(f"job {job_id}: closed — {reason} (seen by {conn.user['name']})")
        return
    job, _ = store.get_job(job_id)
    if job and status in slack.BID_ICONS:
        await asyncio.to_thread(slack.bid_result, conn.user["name"], job.get("title"), job.get("url"), status,
                                None if status == "submitted" else message)


async def read_portfolio(user, force):
    """Read the user's portfolio pages now (new or failed ones; all with force), then show the result"""
    entries = store.get_profile(user["id"])["portfolio"]
    if not force:
        cached = store.get_pages(e["url"] for e in entries)
        entries = [e for e in entries if cached.get(e["url"], {}).get("status") != "ok"]
    if not entries:
        return
    try:
        pages = await asyncio.to_thread(webpages.read_all, store, [e["url"] for e in entries], force)
    except Exception as e:
        log(f"{user['name']}: reading the portfolio failed: {e!r}")
        return
    ok = sum(1 for p in pages.values() if p["status"] == "ok")
    log(f"{user['name']}: portfolio read, {ok}/{len(pages)} pages ok")
    push_profile(user)


def public_profile(user_id):
    """The profile as its extension sees it, with the user's Grok Free devices (connected or not)"""
    profile = profiles.public_for(store, user_id)
    online = grok.connected_ids(user_id)
    devices = [{"id": d["device_id"], "name": d["name"] or d["device_id"], "connected": d["device_id"] in online,
                "last_seen": d["last_seen_ts"]} for d in store.grok_devices(user_id)] if profile["grok_free_allowed"] else []
    return {**profile, "grok_devices": devices,
            "grok_drafter_connected": grok.connected(user_id, profile["grok_device"] or None)}


def push_profile(user):
    profile = public_profile(user["id"])
    for client in hub.clients:
        if client.user["id"] == user["id"]:
            client.send({"type": "profile", "ok": True, "profile": profile})


async def profile_message(conn, msg):
    kind, user_id = msg["type"], conn.user["id"]
    reply = {"type": "profile", "req_id": msg.get("req_id")}
    try:
        if kind == "profile.get":
            conn.send({**reply, "ok": True, "profile": public_profile(user_id)})
        elif kind == "profile.update":
            before = profiles.setup_problem(store.get_profile(user_id))
            # In a thread: an OpenAI key is checked against OpenAI
            await asyncio.to_thread(profiles.update, store, user_id, msg.get("profile") or {})
            profile = public_profile(user_id)
            conn.send({**reply, "ok": True, "profile": profile})
            log(f"{conn.user['name']} updated their profile")
            payload = msg.get("profile") or {}
            if "filters" in payload:
                n = recheck_after_filter_change(conn.user)
                if n:
                    log(f"{conn.user['name']}: filters changed, re-checking {n} recent job(s)")
            if "portfolio" in payload or payload.get("refresh_portfolio"):
                asyncio.create_task(read_portfolio(conn.user, bool(payload.get("refresh_portfolio"))))
            if before and not profile["not_ready"]:
                # Now able to write proposals: catch up on the last few hours' jobs that were waiting for it
                for job_id in store.proposals_with_status(user_id, "no_profile", int(time.time()) - 3 * 3600):
                    pipeline.submit(job_id, users=[conn.user])
        else:
            payload_key = profiles.clean_key((msg.get("profile") or {}).get("api_key"))
            api_key = payload_key or profiles.decrypt_key(store.get_profile(user_id)["api_key_enc"])
            if not api_key:
                raise profiles.ProfileError("no API key to test")
            info = await asyncio.to_thread(proposals.test_key, api_key)
            conn.send({**reply, "ok": True, "info": info, "profile": public_profile(user_id)})
    except (profiles.ProfileError, proposals.ProposalError, ValueError, requests.RequestException) as e:
        # The reasons never contain the key itself
        log(f"{conn.user['name']}: {kind} refused: {e}")
        conn.send({**reply, "ok": False, "error": str(e)})

"""SQLite store shared by the scraper (writes jobs and "new" events) and the server (everything else)"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

from .config import ACTIVITY_RETENTION_DAYS, DB_PATH, EVENT_RETENTION_DAYS

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,   -- Lancers work id
    url           TEXT NOT NULL,
    category      TEXT,
    data          TEXT,                  -- scraped card as JSON; NULL for ids imported from seen_jobs.json
    first_seen_ts INTEGER NOT NULL,
    updated_ts    INTEGER NOT NULL
);
-- The extension's feed: it reconnects with ?since=<last seq> and gets everything after it.
-- user_id NULL: for everyone (the scraper's "new"); otherwise one user's update (their proposal)
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    event      TEXT NOT NULL,            -- new / updated / closed
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_created ON events(created_ts);
-- Single-user bot: only the owner's row (settings.owner_id) is used. Rows from the old multi-user
-- version are kept (their proposals reference them) but disabled by manage.py set-password.
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,     -- unused (tokens were replaced by the owner's password); random filler
    created_ts INTEGER NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0,
    grok_free  INTEGER NOT NULL DEFAULT 0  -- may draft with a Grok Drafter (manage.py grok-free on|off)
);
-- Server settings: owner_id, password_hash (auth.hash_password), password_set_ts
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Project Information read from the logged-in proposal page
CREATE TABLE IF NOT EXISTS job_details (
    job_id     INTEGER PRIMARY KEY REFERENCES jobs(id),
    status     TEXT NOT NULL,            -- ok / login / unavailable / error / skipped
    info       TEXT,                     -- JSON (details.parse_propose_page)
    error      TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    fetched_ts INTEGER NOT NULL,
    closed     TEXT                      -- why nobody can propose any more (Lancers' 提案できません), or NULL
);
-- What each user's extension sends with proposals (prompt, OpenRouter key, filters)
CREATE TABLE IF NOT EXISTS profiles (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id),
    prompt      TEXT,
    api_key_enc TEXT,                    -- Fernet-encrypted with PROFILE_SECRET
    key_hint    TEXT,                    -- "sk-or-…abcd", shown in the extension
    model       TEXT,
    filters     TEXT,                    -- JSON, see filters.DEFAULT_FILTERS
    daily_limit INTEGER,                 -- proposals generated per day (JST)
    reasoning_effort TEXT,               -- OpenRouter reasoning effort (max … none); NULL = the model's default
    speed       TEXT,                    -- normal / fast (:nitro) / latency
    portfolio   TEXT,                    -- JSON [{url, note}], see webpages.py
    web_search  INTEGER,                 -- 1 = OpenRouter's web plugin (the model's own search)
    use_grok_free INTEGER,               -- 1 = ↻ Redraft goes to their Grok Drafter (Grok Free list only)
    grok_device TEXT,                    -- which of their Grok Drafters (device id); NULL = any connected one
    updated_ts  INTEGER NOT NULL
);
-- What each user did in the extension (sent by it), for improving the bot and the proposals
CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    client_id   TEXT,
    ext_version TEXT,
    ts          INTEGER NOT NULL,        -- when it happened (the extension's clock)
    kind        TEXT NOT NULL,           -- e.g. bid.start, bid.result, proposal.submitted, settings.save
    job_id      INTEGER,
    data        TEXT                     -- JSON
);
CREATE INDEX IF NOT EXISTS activity_user_ts ON activity (user_id, ts);
-- Each user's Grok Drafters (devices), as last seen: for choosing one in their settings
CREATE TABLE IF NOT EXISTS grok_devices (
    user_id      INTEGER NOT NULL REFERENCES users(id),
    device_id    TEXT NOT NULL,          -- the drafter's client id
    name         TEXT,                   -- set in the drafter's side panel
    last_seen_ts INTEGER NOT NULL,
    PRIMARY KEY (user_id, device_id)
);
-- Pages read by the server: job links + portfolios (shared cache; public sites only)
CREATE TABLE IF NOT EXISTS pages (
    url        TEXT PRIMARY KEY,
    status     TEXT NOT NULL,            -- ok / error
    title      TEXT,
    text       TEXT,
    error      TEXT,
    fetched_ts INTEGER NOT NULL
);
-- One decision + proposal per (user, job)
CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    job_id        INTEGER NOT NULL REFERENCES jobs(id),
    status        TEXT NOT NULL,         -- ready / skipped / no_profile / error
    allowed       INTEGER NOT NULL,
    blocked       INTEGER NOT NULL DEFAULT 0,
    reasons       TEXT,                  -- JSON list: why not bid / why not sent automatically
    message       TEXT,
    delivery_date TEXT,                  -- 希望納期 (YYYY-MM-DD) -> 完了予定日; NULL: no automatic submission
    auto_submit   INTEGER NOT NULL DEFAULT 0,
    model         TEXT,
    tokens        INTEGER,
    created_ts    INTEGER NOT NULL,
    UNIQUE (user_id, job_id)
);
-- Clients never bid on; user_id NULL = for every user
CREATE TABLE IF NOT EXISTS blacklist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id),
    client_id   TEXT,                    -- /client/<id>
    client_name TEXT,
    note        TEXT,
    created_ts  INTEGER NOT NULL
);
-- Bid outcomes reported by the extensions
CREATE TABLE IF NOT EXISTS bids (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    job_id     INTEGER NOT NULL,
    status     TEXT NOT NULL,            -- submitted / failed / manual / dryrun
    step       TEXT,
    message    TEXT,
    created_ts INTEGER NOT NULL
);
"""

JST = 9 * 3600


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def jst_day_start(ts=None):
    ts = int(ts if ts is not None else time.time())
    return ts - (ts + JST) % 86400


class Store:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Shared by the scraper's category threads and the server's worker threads; every access holds the lock
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            # WAL lets the server read while the scraper writes
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(SCHEMA)
            columns = {r["name"] for r in self.db.execute("PRAGMA table_info(events)")}
            if "user_id" not in columns:
                self.db.execute("ALTER TABLE events ADD COLUMN user_id INTEGER")
            if "grok_free" not in {r["name"] for r in self.db.execute("PRAGMA table_info(users)")}:
                self.db.execute("ALTER TABLE users ADD COLUMN grok_free INTEGER NOT NULL DEFAULT 0")
            if "closed" not in {r["name"] for r in self.db.execute("PRAGMA table_info(job_details)")}:
                self.db.execute("ALTER TABLE job_details ADD COLUMN closed TEXT")
            columns = {r["name"] for r in self.db.execute("PRAGMA table_info(profiles)")}
            for column, kind in (("reasoning_effort", "TEXT"), ("speed", "TEXT"), ("portfolio", "TEXT"), ("web_search", "INTEGER"),
                                 ("use_grok_free", "INTEGER"), ("grok_device", "TEXT")):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE profiles ADD COLUMN {column} {kind}")

    def _one(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def _run(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # ------------------------------------------------------------------ jobs
    def is_seen(self, job_id):
        return self._one("SELECT 1 FROM jobs WHERE id = ?", (job_id,)) is not None

    def add_jobs(self, jobs):
        """Store the jobs not seen before, with a "new" event each; returns those new jobs in order"""
        now = int(time.time())
        new_jobs, ids = [], set()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for job in jobs:
                    if job['id'] in ids:
                        continue
                    ids.add(job['id'])
                    cur = self.db.execute(
                        "INSERT OR IGNORE INTO jobs (id, url, category, data, first_seen_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?)",
                        (job['id'], job['url'], job['category'], json.dumps(job, ensure_ascii=False), now, now))
                    if cur.rowcount:
                        self.db.execute("INSERT INTO events (job_id, event, created_ts) VALUES (?, 'new', ?)", (job['id'], now))
                        new_jobs.append(job)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return new_jobs

    def get_job(self, job_id):
        """The scraped card (dict) and when it was first seen, or (None, None)"""
        row = self._one("SELECT data, first_seen_ts FROM jobs WHERE id = ? AND data IS NOT NULL", (job_id,))
        return (json.loads(row["data"]), row["first_seen_ts"]) if row else (None, None)

    def import_seen_file(self, path):
        """One-time migration: mark the URLs in the old seen_jobs.json as seen, without events"""
        if not os.path.exists(path):
            return 0
        with self.lock:
            if self.db.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
                return 0
            from .scraper import job_id_of
            now = int(time.time())
            with open(path, encoding="utf-8") as f:
                rows = [(job_id_of(url), url.strip(), now, now) for url in f if job_id_of(url)]
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR IGNORE INTO jobs (id, url, first_seen_ts, updated_ts) VALUES (?, ?, ?, ?)", rows)
            self.db.execute("COMMIT")
            return len(rows)

    def prune_events(self):
        cutoff = int(time.time()) - EVENT_RETENTION_DAYS * 86400
        return self._run("DELETE FROM events WHERE created_ts < ?", (cutoff,)).rowcount

    def recent_jobs_without_details(self, since_ts):
        """Jobs seen after since_ts whose Project Information was never fetched (e.g. the server was down)"""
        return [r["id"] for r in self._all(
            """SELECT j.id FROM jobs j LEFT JOIN job_details d ON d.job_id = j.id
               WHERE j.data IS NOT NULL AND j.first_seen_ts >= ? AND d.job_id IS NULL ORDER BY j.id""", (since_ts,))]

    def recent_job_ids(self, since_ts):
        return [r["id"] for r in self._all("SELECT id FROM jobs WHERE data IS NOT NULL AND first_seen_ts >= ?", (since_ts,))]

    # --------------------------------------------------------------- details
    def save_details(self, job_id, status, info=None, error=None):
        self._run("""INSERT INTO job_details (job_id, status, info, error, attempts, fetched_ts) VALUES (?, ?, ?, ?, 1, ?)
                     ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, info = COALESCE(excluded.info, info),
                     error = excluded.error, attempts = attempts + 1, fetched_ts = excluded.fetched_ts""",
                  (job_id, status, json.dumps(info, ensure_ascii=False) if info is not None else None, error, int(time.time())))

    def get_details(self, job_id):
        row = self._one("SELECT status, info, error, attempts, closed FROM job_details WHERE job_id = ?", (job_id,))
        if not row:
            return None
        return {"status": row["status"], "info": json.loads(row["info"]) if row["info"] else None,
                "error": row["error"], "attempts": row["attempts"], "closed": row["closed"]}

    def close_job(self, job_id, reason):
        """Nobody can propose to this job any more; returns False if it was already closed"""
        cur = self._run("UPDATE job_details SET closed = ? WHERE job_id = ? AND closed IS NULL", (reason, job_id))
        if cur.rowcount:
            return True
        if not self._one("SELECT 1 FROM job_details WHERE job_id = ?", (job_id,)):
            self._run("INSERT INTO job_details (job_id, status, error, fetched_ts, closed) VALUES (?, 'unavailable', ?, ?, ?)",
                      (job_id, reason, int(time.time()), reason))
            return True
        return False

    # ---------------------------------------------------------------- events
    def add_event(self, job_id, event, user_id=None):
        return self._run("INSERT INTO events (job_id, event, created_ts, user_id) VALUES (?, ?, ?, ?)",
                         (job_id, event, int(time.time()), user_id)).lastrowid

    def latest_seq(self):
        return self._one("SELECT COALESCE(MAX(seq), 0) FROM events")[0]

    def events_between(self, after, upto, limit=None, user_id=None):
        """Events with after < seq <= upto, oldest first (the newest `limit` of them when limited).
        With user_id: only that user's events and the ones for everyone."""
        sql = """SELECT e.seq, e.event, e.created_ts, e.user_id, j.id, j.data, j.first_seen_ts
                 FROM events e JOIN jobs j ON j.id = e.job_id
                 WHERE e.seq > ? AND e.seq <= ? AND j.data IS NOT NULL"""
        args = [after, upto]
        if user_id is not None:
            sql += " AND (e.user_id IS NULL OR e.user_id = ?)"
            args.append(user_id)
        sql += " ORDER BY e.seq DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return list(reversed(self._all(sql, args)))

    # -------------------------------------------------------------- settings
    def get_setting(self, key):
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def _set_setting(self, key, value):
        # Caller holds self.lock
        self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, None if value is None else str(value)))

    # ------------------------------------------------------- owner and password
    def owner(self):
        """The one user of this bot (id, name, grok_free), or None before manage.py set-password"""
        owner_id = self.get_setting("owner_id")
        if not owner_id:
            return None
        return self._one("SELECT id, name, grok_free FROM users WHERE id = ?", (int(owner_id),))

    def set_password(self, password):
        """Set (or change) the owner's password; returns the owner's name. Raises ValueError for a weak one.

        The first time, the owner becomes the oldest user of the old multi-user database (so their profile,
        proposals and history stay), or a new user "owner". Every other user is disabled."""
        from . import auth
        password_hash = auth.hash_password(password)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT value FROM settings WHERE key = 'owner_id'").fetchone()
                owner_id = int(row["value"]) if row else None
                if owner_id is None or not self.db.execute("SELECT 1 FROM users WHERE id = ?", (owner_id,)).fetchone():
                    first = self.db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
                    owner_id = first["id"] if first else self.db.execute(
                        "INSERT INTO users (name, token_hash, created_ts) VALUES ('owner', ?, ?)",
                        (hash_token(secrets.token_urlsafe(32)), int(time.time()))).lastrowid
                self.db.execute("UPDATE users SET disabled = (id != ?)", (owner_id,))
                self._set_setting("owner_id", owner_id)
                self._set_setting("password_hash", password_hash)
                # Connections opened with the previous password are closed when this changes (server.Hub)
                self._set_setting("password_set_ts", f"{time.time():.6f}")
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return self.owner()["name"]

    def check_password(self, password):
        """The owner when the password is right, else None (also None while no password is set)"""
        from . import auth
        if auth.verify_password(password, self.get_setting("password_hash")):
            return self.owner()
        return None

    def password_version(self):
        """Changes whenever the password is set; None while there is none"""
        return self.get_setting("password_set_ts")

    def active_users(self):
        """Whom the pipeline writes for: the owner only"""
        owner = self.owner()
        return [owner] if owner else []

    def user_by_id(self, user_id):
        return self._one("SELECT id, name, grok_free FROM users WHERE id = ?", (user_id,))

    def set_grok_free(self, on):
        """Allow (or stop) Grok Free for the owner; False before set-password"""
        owner = self.owner()
        if not owner:
            return False
        self._run("UPDATE users SET grok_free = ? WHERE id = ?", (int(bool(on)), owner["id"]))
        return True

    # -------------------------------------------------------------- profiles
    def get_profile(self, user_id):
        row = self._one("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
        if not row:
            return {"prompt": None, "api_key_enc": None, "key_hint": None, "model": None, "filters": {}, "daily_limit": None,
                    "reasoning_effort": None, "speed": None, "portfolio": [], "web_search": None, "use_grok_free": None,
                    "grok_device": None}
        profile = dict(row)
        profile["filters"] = json.loads(row["filters"]) if row["filters"] else {}
        profile["portfolio"] = json.loads(row["portfolio"]) if row["portfolio"] else []
        return profile

    def save_profile(self, user_id, **fields):
        """Update only the given fields (prompt, api_key_enc, key_hint, model, filters, daily_limit,
        reasoning_effort, speed, portfolio, web_search, use_grok_free, grok_device)"""
        current = self.get_profile(user_id)
        current.update(fields)
        self._run("""INSERT INTO profiles (user_id, prompt, api_key_enc, key_hint, model, filters, daily_limit,
                                          reasoning_effort, speed, portfolio, web_search, use_grok_free, grok_device, updated_ts)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(user_id) DO UPDATE SET prompt = excluded.prompt, api_key_enc = excluded.api_key_enc,
                     key_hint = excluded.key_hint, model = excluded.model, filters = excluded.filters,
                     daily_limit = excluded.daily_limit, reasoning_effort = excluded.reasoning_effort,
                     speed = excluded.speed, portfolio = excluded.portfolio, web_search = excluded.web_search,
                     use_grok_free = excluded.use_grok_free, grok_device = excluded.grok_device,
                     updated_ts = excluded.updated_ts""",
                  (user_id, current["prompt"], current["api_key_enc"], current["key_hint"], current["model"],
                   json.dumps(current["filters"] or {}, ensure_ascii=False), current["daily_limit"],
                   current.get("reasoning_effort"), current.get("speed"),
                   json.dumps(current.get("portfolio") or [], ensure_ascii=False), current.get("web_search"),
                   current.get("use_grok_free"), current.get("grok_device"), int(time.time())))

    # ---------------------------------------------------------- grok devices
    def save_grok_device(self, user_id, device_id, name):
        self._run("""INSERT INTO grok_devices (user_id, device_id, name, last_seen_ts) VALUES (?, ?, ?, ?)
                     ON CONFLICT(user_id, device_id) DO UPDATE SET name = excluded.name, last_seen_ts = excluded.last_seen_ts""",
                  (user_id, device_id, name, int(time.time())))

    def grok_devices(self, user_id):
        return [dict(r) for r in self._all("SELECT device_id, name, last_seen_ts FROM grok_devices WHERE user_id = ? ORDER BY last_seen_ts DESC",
                                           (user_id,))]

    # -------------------------------------------------------------- activity
    def add_activity(self, user_id, client_id, ext_version, events):
        rows = [(user_id, client_id, ext_version, int(e["ts"]), e["kind"], e.get("job_id"),
                 json.dumps(e.get("data") or {}, ensure_ascii=False)) for e in events]
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("""INSERT INTO activity (user_id, client_id, ext_version, ts, kind, job_id, data)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""", rows)
            self.db.execute("COMMIT")

    def list_activity(self, user_id=None, since_ts=0, kind=None, limit=1000):
        sql = """SELECT a.*, u.name AS user FROM activity a JOIN users u ON u.id = a.user_id WHERE a.ts >= ?"""
        args = [since_ts]
        if user_id:
            sql += " AND a.user_id = ?"
            args.append(user_id)
        if kind:
            sql += " AND a.kind LIKE ?"
            args.append(kind.replace("*", "%"))
        rows = self._all(sql + " ORDER BY a.ts, a.id LIMIT ?", (*args, limit))
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def prune_activity(self):
        self._run("DELETE FROM activity WHERE ts < ?", (int(time.time()) - ACTIVITY_RETENTION_DAYS * 86400,))

    # ----------------------------------------------------------------- pages
    def get_page(self, url):
        row = self._one("SELECT * FROM pages WHERE url = ?", (url,))
        return dict(row) if row else None

    def get_pages(self, urls):
        return {url: page for url in urls if (page := self.get_page(url))}

    def save_page(self, url, status, title=None, text=None, error=None):
        self._run("""INSERT INTO pages (url, status, title, text, error, fetched_ts) VALUES (?, ?, ?, ?, ?, ?)
                     ON CONFLICT(url) DO UPDATE SET status = excluded.status, title = excluded.title, text = excluded.text,
                     error = excluded.error, fetched_ts = excluded.fetched_ts""",
                  (url, status, title, text, error, int(time.time())))

    # ------------------------------------------------------------- proposals
    def save_proposal(self, user_id, job_id, status, decision, message=None, delivery_date=None,
                      auto_submit=False, reasons=None, model=None, tokens=None):
        cur = self._run("""INSERT INTO proposals (user_id, job_id, status, allowed, blocked, reasons, message, delivery_date,
                                                  auto_submit, model, tokens, created_ts)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(user_id, job_id) DO UPDATE SET status = excluded.status, allowed = excluded.allowed,
                           blocked = excluded.blocked, reasons = excluded.reasons, message = excluded.message,
                           delivery_date = excluded.delivery_date, auto_submit = excluded.auto_submit,
                           model = excluded.model, tokens = excluded.tokens, created_ts = excluded.created_ts""",
                        (user_id, job_id, status, int(decision["allowed"]), int(decision["blocked"]),
                         json.dumps(reasons if reasons is not None else decision["reasons"], ensure_ascii=False),
                         message, delivery_date, int(auto_submit), model, tokens, int(time.time())))
        return cur.lastrowid

    def get_proposal(self, user_id, job_id):
        row = self._one("SELECT * FROM proposals WHERE user_id = ? AND job_id = ?", (user_id, job_id))
        if not row:
            return None
        proposal = dict(row)
        proposal["reasons"] = json.loads(row["reasons"]) if row["reasons"] else []
        return proposal

    def proposals_with_status(self, user_id, status, since_ts):
        return [r["job_id"] for r in self._all("SELECT job_id FROM proposals WHERE user_id = ? AND status = ? AND created_ts >= ?",
                                               (user_id, status, since_ts))]

    def submitted_job_ids(self, user_id):
        return {r["job_id"] for r in self._all("SELECT DISTINCT job_id FROM bids WHERE user_id = ? AND status = 'submitted'", (user_id,))}

    def submitted_bid(self, user_id, job_id):
        row = self._one("SELECT step, created_ts FROM bids WHERE user_id = ? AND job_id = ? AND status = 'submitted' ORDER BY id LIMIT 1",
                        (user_id, job_id))
        return {"at": row["created_ts"], "step": row["step"]} if row else None

    def proposals_today(self, user_id):
        return self._one("SELECT COUNT(*) FROM proposals WHERE user_id = ? AND status = 'ready' AND model IS NOT NULL AND created_ts >= ?",
                         (user_id, jst_day_start()))[0]

    # ------------------------------------------------------------- blacklist
    def is_blocked(self, user_id, client_id, client_name):
        if not client_id and not client_name:
            return False
        return self._one("""SELECT 1 FROM blacklist WHERE (user_id IS NULL OR user_id = ?)
                            AND ((client_id IS NOT NULL AND client_id = ?) OR (client_name IS NOT NULL AND client_name = ?))""",
                         (user_id, client_id or "", client_name or "")) is not None

    def add_block(self, user_id, client_id, client_name, note=None):
        """Returns False when the client was already blocked for this user (or for everyone)"""
        if self.is_blocked(user_id, client_id, None if client_id else client_name):
            return False
        self._run("INSERT INTO blacklist (user_id, client_id, client_name, note, created_ts) VALUES (?, ?, ?, ?, ?)",
                  (user_id, client_id, client_name, note, int(time.time())))
        return True

    def remove_block(self, entry_id):
        return self._run("DELETE FROM blacklist WHERE id = ?", (entry_id,)).rowcount > 0

    def list_blocks(self):
        return self._all("""SELECT b.id, b.client_id, b.client_name, b.note, u.name AS user FROM blacklist b
                            LEFT JOIN users u ON u.id = b.user_id ORDER BY b.id""")

    # ------------------------------------------------------------------ bids
    def save_bid(self, user_id, job_id, status, step, message):
        self._run("INSERT INTO bids (user_id, job_id, status, step, message, created_ts) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, job_id, status, step, message, int(time.time())))

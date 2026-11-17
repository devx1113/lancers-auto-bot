# CrowdWorks Job Scraping Bot

A Python bot that detects new job postings from Lancers and automatically posts them to Discord channels by category.

## Features

- 🔍 Scrapes job postings from Lancers in real-time
- 📊 Categorizes jobs into relevant categories:
  - System
  - Web 

- 📡 **Discord Integration**: Automatically posts new jobs to Discord channels
- 🎨 Beautiful Discord embeds with job details(title, url, ...)
- 💾 Tracks seen jobs to avoid duplicates
- ⏰ Supports both one-time and continuous monitoring
- 🚀 **2-second monitoring** for real-time Discord notifications

## Installation

1. Install Python 3.9 or higher
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
   Or run the setup script:
   ```bash
   python setup.py
   ```

## Usage
####
### Run Once (Console Only)
```bash
python main.py
```

### Run Continuously (Console Only)
```bash
python main.py --continuous
```

### Discord Mode (2-second monitoring)
```bash
python main.py --discord
```

### Custom Check Interval
```bash
python main.py --continuous --interval=60  # Check every 10 minutes
```

## Discord Integration

The bot automatically posts new job postings to your Discord server channels:

- **System** → System channel
- **Web** → Web channel

Each Discord post includes:
- 🆕 Job title and description
- 💰 Price/budget information
- 🔗 Direct link to the job posting

## Output

The bot will display:
- Job title and category
- Price/budget information
- Job URL (if available)
- Description preview
- Posting timestamp
- Discord posting status

## Files

- `main.py` - Main bot script with Discord integration
- `requirements.txt` - Python dependencies
- `setup.py` - Installation script
- `lancers_bot/` - Scraper, Slack notifier, SQLite store and the extension server
- `run_server.py` - WebSocket server for the Lancers Auto-Proposal browser extension (`../lancers-proposal-extension`)
- `manage.py` - Sets the owner password, the client blacklist and the server secret
- `data/lancers.db` - Seen jobs and the extension's event feed (auto-generated; `seen_jobs.json` is imported into it once)
- `deploy/nginx/lancers-bot.conf` - nginx (TLS / `wss://`) in front of the server

## Extension Server

`main.py` records every new job in `data/lancers.db` (and posts it to Slack as before);
`run_server.py` serves the Lancers Auto-Proposal extension (`../lancers-proposal-extension`) over a WebSocket and runs the
proposal pipeline for every new job:

1. **Project Information** — the logged-in proposal page (`/work/propose_start/<id>`) is read
   through a connected extension (with that browser's Lancers login): 依頼タイトル, クライアント名,
   源泉徴収, 依頼予算, 募集開始日時, 募集締切日時, 希望納期, 詳細, NDA.
2. **Filters** per user (`lancers_bot/filters.py`): コンペ (選定方式) jobs, blacklisted clients,
   closed jobs, a minimum budget, excluded keywords and excluded client names. Add a rule there for new criteria.
3. **Proposal** per user with their prompt and OpenRouter key (stored on the server, the key
   encrypted with `PROFILE_SECRET`).
4. **Delivery** to that user's extension, which fills `propose_start/<id>?proposeReferer=`: the
   proposal text, 完了予定日 = 希望納期, 契約金額 left as prefilled, the NDA previewed then agreed,
   then 内容を確認する → 利用規約に同意して提案する. Without a 希望納期 nothing is sent: the
   proposal waits in the extension's **Pending** tab, which opens the form filled in.

```bash
pm2 start ecosystem.config.js          # lancers-bot (scraper) + lancers-server
python manage.py set-password          # set / change the owner password (asked twice, never shown)
python manage.py status                # password set or not, and your profile state
python manage.py block <client URL or クライアント名>   # blacklist
python manage.py list-blocks / unblock <id>
python manage.py activity [--days 7] [--kind 'bid.*'] [--json]   # what you did in the extension
python manage.py grok-free on|off                               # allow the Grok Drafter / Grok Free
python manage.py gen-secret            # PROFILE_SECRET for .env (once; keep it)
```

**Access: one user, one password.** The bot has a single user (its owner). Nothing works without the
password set by `manage.py set-password` (12+ characters; only a scrypt hash is stored). Enter it in the
extension's **Settings → Token** field (the extension sends it where it used to send a token). A wrong
password is refused; 5 wrong ones from one address lock that address out for 15 minutes (Slack alert);
changing the password disconnects everything that used the old one. Before a password is set the server
refuses every connection. Upgrading from the multi-user version: the first `set-password` keeps the
oldest user's profile and history as the owner and disables the other users.

Install the extension: `chrome://extensions` → Developer mode → **Load unpacked** →
`lancers-proposal-extension`, set the server URL and the password (Token field), then open
**Settings → Your profile**, enter your prompt and API key and press **Save to server**. The key is an OpenRouter key (`sk-or-…`: Claude, Grok and
OpenAI models) or an OpenAI key (`sk-…`: OpenAI models only, called on OpenAI's Responses API and
billed by OpenAI; Fast = OpenAI's priority tier, no Low latency).
Links a client puts in the 依頼内容 (up to 3, not Lancers' own pages) are read by the server and given to
the model with the job. Optionally users list their **Portfolio** (one past project per line: URL + what they did there; the
server reads each page itself, caches its text and gives it to the model with every proposal) and tick
**Web search** (OpenRouter's web plugin: the model's own search, extra cost per proposal).

**Updates.** Every extension must run the version in `../lancers-proposal-extension/manifest.json`
(`EXTENSION_DIR`). Bump that version after changing the extension: connected extensions that are
older are told at once and pause (no jobs, bids, relay fetches or notification forwarding); their
side panel offers **Download the update** (a zip of that folder, no token in it) and **Reload the
extension** after it is unzipped over the extension's folder ("Loaded from" on chrome://extensions).
Unpacked extensions can't update themselves; only a Chrome Web Store (or enterprise-policy) install
would auto-update.

**Grok Free** (`../grok-drafter-extension`, for users on the Grok Free list: `manage.py grok-free <name>
on|off`). The Grok Drafter is installed in a browser its user keeps logged in to grok.com; it connects
to `wss://<your domain>` (or `ws://127.0.0.1:8765` on this server; unencrypted ws:// to anywhere else is
refused), the server refuses it for anyone not on the list, and it has its own version, outside the
update check. A user can connect several (one per browser, each with a device name) and choose
which one drafts in their settings (or any connected one). **Grok Free** is a model in Settings →
Your profile (and in the Test tab), shown only to them. With it selected, every proposal — new jobs
included — is written by Grok: a new conversation in their open Grok tab with the prompt, the rules
and the job, its answer copied with Grok's copy button once that answer's own text has settled (a Stop
control elsewhere on the page does not keep it waiting, and the message just sent is never
taken as the answer). Auto-Bid submits those proposals the same way
as any other the server marks ready. No API key is required for that model. "↻ Redraft with Grok Free"
still sends a ↻ to the drafter when some other model is selected, and falls back to that model if the
drafter is offline; with Grok Free itself selected, an offline drafter fails the proposal instead.
A login page or a human check stops the draft with a message; it is never acted on.

For any model, a bid that can be automatic opens the Lancers form while the text is still being written
and fills 完了予定日 and the contract consent then. The text is pasted and submitted only after it
arrives (and the usual wait after 募集開始 has passed). A held proposal closes that tab and is not counted.

**Test tab.** Project details, a prompt, a model (or Grok Free), and that model's reasoning level and
speed → the output, with the user's own key and the server's output rules, as for a real proposal;
nothing is saved or submitted. An OpenAI key lists only OpenAI models there, as in Settings.

**Activity log.** The extension logs what its user does — panel actions, settings changes (secrets
only as "changed"), bids and their results, the text actually submitted next to the generated one
(also when a proposal is finished by hand on Lancers), forwarded notifications — and sends it to the
server's `activity` table (kept 365 days). `manage.py activity --json` exports it.

The server listens on `127.0.0.1:8765`. For remote extensions, put nginx in front
(`deploy/nginx/lancers-bot.conf`) and set the extension's Server URL to `wss://<your domain>`.
Without a domain of your own, a free DuckDNS name works (this server: `wss://lancers-bb.duckdns.org`).

Tests (offline: fixtures, a mock OpenRouter, no Slack):

```bash
python tests/test_details.py                       # proposal-page parser + filters
python tests/test_webpages.py                      # job links, portfolio pages, public-address check, web search
python tests/e2e_server.py <scratch dir>           # server, relay, pipeline, profiles, block list
python tests/e2e_extension.py <scratch dir>        # the extension in headless Chromium against the server
python tests/e2e_grok_drafter.py <scratch dir>     # Grok Drafter in headless Chromium against a stand-in grok.com
```

## Notes

- The bot respects Lancers' robots.txt and uses appropriate delays
- Jobs are tracked to avoid showing duplicates
- The bot handles network errors gracefully
- Discord webhooks are configured for your specific server
- Press Ctrl+C to stop continuous monitoring
- Discord mode automatically uses 2-second intervals for real-time updates

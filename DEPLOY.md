# Deploy the Lancers bot

Two processes run from the project directory. `.env` is read from the folder that contains `main.py`.

- `lancers-bot` polls Lancers and posts new jobs to Slack: `main.py --slack --interval=5`
- `lancers-server` serves the browser extension and writes proposals: `run_server.py` on `127.0.0.1:8765`

Python 3.9 or newer is required. The extension on another computer connects to `wss://<your domain>`. On the server machine itself, `ws://127.0.0.1:8765` is enough.

Do not commit `.env`, `.venv/`, `data/`, or `seen_jobs.json`.

## 1. Get the code

```bash
git clone git@github.com:sudo-enjoy/lancers-scraping-bot.git
cd lancers-scraping-bot
```

Use the `main` branch.

The version check and the extension's update zip read `../lancers-proposal-extension` (the folder next to this project). Clone that repository beside this one, or set `EXTENSION_DIR` in `.env` to its path. Without that folder the server still runs, and no extension is marked out of date.

## 2. Configure

Copy the example environment file and fill in real values.

Linux:

```bash
cp .env.example .env
```

Windows (Command Prompt):

```bat
copy .env.example .env
```

Generate `PROFILE_SECRET` once and paste it into `.env`. Keep it. Changing it makes every saved API key unreadable.

Linux:

```bash
.venv/bin/python manage.py gen-secret
```

Windows:

```bat
.venv\Scripts\python.exe manage.py gen-secret
```

Run that after the virtualenv in section 3 or 4 exists. You can also run `python manage.py gen-secret` with any Python that has `cryptography` installed.

| Variable | Required | Purpose |
| --- | --- | --- |
| `SLACK_WEBHOOK_SYSTEM` | yes | System channel |
| `SLACK_WEBHOOK_WEB` | yes | Web channel |
| `LANCERS_PROXY` | no | HTTP proxy for Lancers search pages only. Unset = direct. If it is unreachable the scraper goes direct by itself (rechecks every 10 min, one Slack alert). Example: `http://user:pass@host:port` |
| `SERVER_HOST` | no | Bind address. Leave `127.0.0.1`. The domain proxy is what the internet reaches |
| `SERVER_PORT` | no | Local port. Leave `8765` so it matches the nginx and Caddy configs below |
| `PROFILE_SECRET` | yes | Fernet key from `manage.py gen-secret`. Encrypts users' API keys |
| `SLACK_WEBHOOK_BIDS` | no | One Slack line per bid result |
| `SLACK_WEBHOOK_ALERTS` | no | Operator alerts (logged-out relay browser, page layout changed) |
| `DEFAULT_DAILY_LIMIT` | no | Proposals per user per day. Default `50` |
| `EXTENSION_DIR` | no | Extension folder used for the version gate. Default: `../lancers-proposal-extension` |

Set the owner password after `.env` is in place (at least 12 characters, asked twice, never shown).
Until it is set the server refuses every connection.

```bash
python manage.py set-password
```

The first poll has no seen-job history, so it can post every matching job on the first pages. Run `main.py` once in the foreground and watch Slack before leaving it under pm2. An existing `seen_jobs.json` is imported into `data/lancers.db` on the first start. Leave `data/` in place across restarts.

## 3. Linux

Install Python 3, Node.js, and pm2.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --slack --interval=5
```

Stop that foreground run with Ctrl+C after the first cycle looks right. Then:

```bash
pm2 start ecosystem.config.js
pm2 save
pm2 startup
```

`ecosystem.config.js` starts `lancers-bot` (`--slack --interval=5`) and `lancers-server`, both with `./.venv/bin/python`. `pm2 startup` prints a command. Run that command so both processes come back after a reboot, then run `pm2 save` again.

Confirm the local server before you add a domain:

```bash
curl -fsS http://127.0.0.1:8765/health
```

A JSON body with `"ok": true` means `lancers-server` is up. Do not open port 8765 on the firewall.

## 4. Windows

From the project directory in Command Prompt:

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
start.cmd
```

`start.cmd` changes to the project directory and runs only the scraper (`main.py --slack --interval=5`) with `.venv\Scripts\python.exe` when that interpreter exists. Close the window to stop it. This does not start `lancers-server`, and it does not restart after a crash or a reboot.

To keep both processes running with pm2, install Node.js, then in an elevated PowerShell:

```powershell
npm install -g pm2
pm2 start main.py --name lancers-bot --interpreter .venv\Scripts\python.exe -- --slack --interval=5
pm2 start run_server.py --name lancers-server --interpreter .venv\Scripts\python.exe
pm2 save
```

The `ecosystem.config.js` interpreter path is `./.venv/bin/python`. That path is for Linux. On Windows, start pm2 with the two commands above, or change both `interpreter` values to `.venv\\Scripts\\python.exe` before `pm2 start ecosystem.config.js`.

Confirm the local server:

```bat
curl -fsS http://127.0.0.1:8765/health
```

## 5. Domain

`lancers-server` binds to `127.0.0.1` only. A remote extension needs a hostname and TLS. The WebSocket URL carries the password in the query string, so enter it in the extension only after `https://` works.

On the machine that runs the server, the extension's Server URL can stay `ws://127.0.0.1:8765`. Anywhere else, set it to `wss://<your domain>` with no path. The extension adds `/ws` itself. The same URL is what Grok Drafter uses. Grok Drafter accepts `ws://` only for `127.0.0.1`. Any other host must be `wss://`.

This server's name is `lancers-bb.duckdns.org`. The steps below use that name. Another DuckDNS name, or a domain you already own, works the same way: put that name in the proxy config and in the extension.

### DuckDNS

1. Create the name at [https://www.duckdns.org](https://www.duckdns.org) and point it at this machine's public IP.
2. Keep the IP updated. `YOUR_TOKEN` is the token DuckDNS shows for your account. Do not commit it.

Linux crontab line, every five minutes:

```bash
*/5 * * * * curl -fsS "https://www.duckdns.org/update?domains=lancers-bb&token=YOUR_TOKEN&ip=" >/dev/null
```

Windows Task Scheduler, program `curl.exe`, every five minutes:

```bat
curl.exe -fsS "https://www.duckdns.org/update?domains=lancers-bb&token=YOUR_TOKEN&ip="
```

Allow inbound TCP 80 and 443. Leave 8765 closed.

### Linux (nginx)

`deploy/nginx/lancers-bot.conf` proxies `/ws` and `/health` to `127.0.0.1:8765` and logs the path without the query string, so the token is not written to the access log.

```bash
sudo cp deploy/nginx/lancers-bot.conf /etc/nginx/sites-available/lancers-bot
sudo sed -i 's/bot.example.com/lancers-bb.duckdns.org/g' /etc/nginx/sites-available/lancers-bot
sudo ln -s /etc/nginx/sites-available/lancers-bot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d lancers-bb.duckdns.org --redirect
```

Certbot adds TLS on 443 and the HTTP to HTTPS redirect. Until it has run, the site is plain `http://` and the extension cannot use `wss://`.

```bash
curl -fsS https://lancers-bb.duckdns.org/health
```

In the extension: Settings → Server URL = `wss://lancers-bb.duckdns.org`, then the password from `manage.py set-password` in the Token field.

### Windows (Caddy)

The nginx file in this repository is for Linux. On Windows, Caddy obtains the certificate and proxies to the same local port. Install Caddy, then save this as `Caddyfile` in a folder you choose. The name must already point at this PC.

```
lancers-bb.duckdns.org {
	reverse_proxy /ws 127.0.0.1:8765
	reverse_proxy /health 127.0.0.1:8765
	respond 404
}
```

Leave Caddy's access log off. A request log would store `/ws?token=...`.

```powershell
caddy start
curl.exe -fsS https://lancers-bb.duckdns.org/health
```

In the extension on this PC or any other: Settings → Server URL = `wss://lancers-bb.duckdns.org`.

Port 80 must be reachable once, so Caddy can complete the certificate challenge. After that, the extension uses port 443 only.

## 6. Check, restart, update

Linux:

```bash
pm2 status
pm2 logs lancers-bot
pm2 logs lancers-server
pm2 restart lancers-bot
pm2 restart lancers-server
```

Windows, if you used pm2:

```bat
pm2 status
pm2 logs lancers-bot
pm2 logs lancers-server
pm2 restart lancers-bot
pm2 restart lancers-server
```

Restart both processes after any change to `.env` or the code. `.env` is read at startup. Restarting `lancers-server` drops current WebSocket connections. The extensions reconnect on their own.

Update a Linux checkout:

```bash
git pull
.venv/bin/pip install -r requirements.txt
pm2 restart lancers-bot
pm2 restart lancers-server
```

Update a Windows checkout:

```bat
git pull
.venv\Scripts\pip install -r requirements.txt
pm2 restart lancers-bot
pm2 restart lancers-server
```

`data/lancers.db` is rewritten while the bot runs. Leave that directory in place across updates so seen jobs, users, and proposals stay.

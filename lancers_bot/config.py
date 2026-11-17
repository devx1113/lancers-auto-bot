"""Settings shared by the scraper and the server, read from .env next to the project"""

import os
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

# SQLite database shared by the scraper (writer) and the server (reader)
DB_PATH = os.getenv("LANCERS_DB") or os.path.join(ROOT, "data", "lancers.db")
# Old dedup list, imported into the database once
SEEN_JOBS_FILE = os.path.join(ROOT, "seen_jobs.json")

# Optional proxy for Lancers requests only (Slack posts don't go through it)
LANCERS_PROXY = os.getenv("LANCERS_PROXY")

# Slack webhook URLs for each category
SLACK_WEBHOOKS = {
    'system': os.getenv('SLACK_WEBHOOK_SYSTEM'),
    'web': os.getenv('SLACK_WEBHOOK_WEB'),
}

# Optional: bid results (one line per submitted / failed proposal) and operator alerts
# (logged-out relay browser, proposal pages that stop parsing); unset = log only
SLACK_WEBHOOK_BIDS = os.getenv('SLACK_WEBHOOK_BIDS')
SLACK_WEBHOOK_ALERTS = os.getenv('SLACK_WEBHOOK_ALERTS')

# Encrypts the users' OpenRouter keys in the database (Fernet key; manage.py gen-secret makes one)
PROFILE_SECRET = os.getenv("PROFILE_SECRET")
# Proposals generated per user per day (JST) unless their profile sets another limit
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "50"))

# Extension server: listens on localhost only, nginx terminates TLS in front of it
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8765"))
# The extension's source folder: its manifest version is the one every extension must run (older ones
# are told to update and paused), and its files are what an update download contains
EXTENSION_DIR = os.getenv("EXTENSION_DIR") or os.path.join(os.path.dirname(ROOT), "lancers-proposal-extension")

# Max search result pages fetched per category in one poll
MAX_PAGES = 3
# Jobs per Slack message (each job uses 4 of Slack's 50 blocks per message, incl. the divider)
SLACK_JOBS_PER_MESSAGE = 10
# Events older than this are pruned; an extension offline longer just misses them
EVENT_RETENTION_DAYS = 14
# Users' activity log (what they did in the extension), for improving the bot and the proposals
ACTIVITY_RETENTION_DAYS = 365

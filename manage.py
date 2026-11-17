#!/usr/bin/env python3
"""Manage the bot's owner password, the client blacklist and the server secret. The bot has one user:
its owner, who connects with the password.

    python manage.py set-password          set or change the password (asked twice, never shown);
                                           connections that used the old one are closed. Put the
                                           password in the extension's Settings → Token field
                                           (and in the Grok Drafter's, if you use one)
    python manage.py status                password set or not, and the profile state
    python manage.py block <client> [--note TEXT]
                                           never bid on a client: their /client/<id>, the full
                                           https://www.lancers.jp/client/<id> URL, or the exact
                                           クライアント名 shown on the proposal page
    python manage.py unblock <entry id>    / list-blocks
    python manage.py activity [--days N] [--kind KIND] [--json]
                                           what you did in the extension (newest last); KIND may
                                           end in * (e.g. bid.*); --json prints one JSON object per line
    python manage.py grok-free on|off      allow the Grok Drafter (../grok-drafter-extension) and
                                           "Grok Free" as a model
    python manage.py gen-secret            print a new PROFILE_SECRET for .env
"""

import argparse
import getpass
import json
import sys
import time
from datetime import datetime

from cryptography.fernet import Fernet

from lancers_bot import auth, profiles
from lancers_bot.store import Store
from lancers_bot.wire import client_id_of


def ask_password():
    if not sys.stdin.isatty():
        # Piped in (scripts): one line
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(f"New password (at least {auth.MIN_PASSWORD_CHARS} characters): ")
    if getpass.getpass("Repeat it: ") != first:
        sys.exit("the two passwords differ — nothing changed")
    return first


def main():
    parser = argparse.ArgumentParser(description="Manage the owner password and the blacklist")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("set-password")
    sub.add_parser("status")
    block = sub.add_parser("block")
    block.add_argument("client")
    block.add_argument("--note")
    sub.add_parser("unblock").add_argument("entry_id", type=int)
    sub.add_parser("list-blocks")
    activity = sub.add_parser("activity")
    activity.add_argument("--days", type=float, default=7)
    activity.add_argument("--kind")
    activity.add_argument("--limit", type=int, default=1000)
    activity.add_argument("--json", action="store_true")
    sub.add_parser("grok-free").add_argument("state", choices=["on", "off"])
    sub.add_parser("gen-secret")
    args = parser.parse_args()

    if args.command == "gen-secret":
        print(f"PROFILE_SECRET={Fernet.generate_key().decode()}")
        return

    store = Store()
    if args.command == "set-password":
        changing = bool(store.password_version())
        try:
            name = store.set_password(ask_password())
        except ValueError as e:
            sys.exit(f"refused: {e} — nothing changed")
        print(f"Password {'changed' if changing else 'set'} (owner: {name}).")
        print("Enter it in the extension: Settings → Token (the field that used to hold the token).")
        if changing:
            print("Connections that used the old password are closed within a few seconds.")
    elif args.command == "status":
        owner = store.owner()
        if not owner or not store.password_version():
            print("No password set: the server refuses every connection. Run: python manage.py set-password")
            return
        set_at = datetime.fromtimestamp(float(store.password_version())).strftime("%Y-%m-%d %H:%M")
        profile = profiles.public(store.get_profile(owner["id"]))
        state = profile["not_ready"] or f"ready · {profile['model']} · key {profile['key_hint']}"
        print(f"owner: {owner['name']}   password set {set_at}{'   [Grok Free on]' if owner['grok_free'] else ''}\nprofile: {state}")
    elif args.command == "block":
        client = args.client.strip()
        client_id = client_id_of(client if "/client/" in client else None)
        if store.add_block(None, client_id, None if client_id else client, note=args.note):
            print(f"Blocked {client_id or client} (applies to jobs decided from now on)")
        else:
            print(f"{client_id or client} was already blocked")
    elif args.command == "grok-free":
        if not store.set_grok_free(args.state == "on"):
            sys.exit("set a password first (python manage.py set-password)")
        print(f"Grok Free {args.state}" + (" (the extension shows the setting after it reconnects)" if args.state == "on" else ""))
    elif args.command == "activity":
        rows = store.list_activity(None, int(time.time() - args.days * 86400), args.kind, args.limit)
        for r in rows:
            if args.json:
                print(json.dumps(r, ensure_ascii=False))
                continue
            when = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
            data = json.dumps(r["data"], ensure_ascii=False)
            print(f"{when}  {r['kind']:<22} {r['job_id'] or '':<8} {data[:160]}{'…' if len(data) > 160 else ''}")
    elif args.command == "unblock":
        print("removed" if store.remove_block(args.entry_id) else f"no entry {args.entry_id}")
    else:
        for b in store.list_blocks():
            who = b["client_id"] and f"/client/{b['client_id']}" or b["client_name"]
            print(f"{b['id']:>4}  {who:<40} {b['note'] or ''}")


if __name__ == "__main__":
    main()

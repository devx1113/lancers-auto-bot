"""Web pages the server reads for the model, because a model called through the API can't open links:

- links the client put in the job's 依頼内容 (their current site, a reference site, a spec page), read
  when the proposal is written;
- each user's portfolio: past-project URLs with a short note, read once and refreshed weekly.

Pages are cached in the pages table. Only public addresses are fetched: the URLs come from users and
clients, and the server must not be made to read its own network.
"""

import ipaddress
import os
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

# Text kept per page (cache), used per job link, and per portfolio page / for all of a user's portfolio
STORED_CHARS = 3000
JOB_PAGE_CHARS = 3000
PORTFOLIO_PAGE_CHARS = 2000
PORTFOLIO_TOTAL_CHARS = 12000
# Links read from one job's 依頼内容
JOB_LINKS = 3
# Lancers' own pages need a login and say nothing about the client's project
SKIP_HOSTS = ("lancers.jp",)
MAX_ENTRIES = 10
MAX_NOTE_CHARS = 300
MAX_PAGE_BYTES = 2_000_000
TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 5
PORTFOLIO_REFRESH_SECONDS = 7 * 86400
JOB_REFRESH_SECONDS = 86400
# A page that failed is tried again after this long
RETRY_SECONDS = 3600
USER_AGENT = "Mozilla/5.0 (compatible; LancersProposalBot/1.0)"
# Tests serve their pages from 127.0.0.1
ALLOW_PRIVATE = os.getenv("PAGE_FETCH_ALLOW_PRIVATE") == "1"

# ASCII only, so a URL written straight into Japanese text ("https://example.com/を改修") ends where it should
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&*+,;=%]+|(?<![\w/.@-])www\.[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+[A-Za-z0-9\-._~:/?#@!$&*+,;=%]*")


class PageError(Exception):
    pass


def find_urls(text):
    urls = []
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?")
        if url.startswith("www."):
            url = "https://" + url
        if url not in urls:
            urls.append(url)
    return urls


def job_links(info):
    """The links in a job's 依頼内容 worth reading, at most JOB_LINKS"""
    links = []
    for url in find_urls(info.get("description")):
        host = (urlsplit(url).hostname or "").lower()
        if host and not any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
            links.append(url)
    return links[:JOB_LINKS]


def parse_entries(value):
    """The portfolio: one entry per line, "URL  what I did there"; also takes the list the server sends
    back. Returns [{"url", "note"}]; raises ValueError"""
    if isinstance(value, str):
        value = value.splitlines()
    entries, seen = [], set()
    for item in value or []:
        if isinstance(item, dict):
            url, note = str(item.get("url") or "").strip(), str(item.get("note") or "").strip()
        else:
            line = str(item).strip()
            if not line:
                continue
            m = URL_RE.search(line)
            if not m:
                raise ValueError(f"portfolio: no http(s) URL in 「{line[:40]}」")
            url = find_urls(m.group(0))[0]
            note = (line[:m.start()] + " " + line[m.end():]).strip(" \t-–—:：|・")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"portfolio: {url[:60]} is not an http(s) URL")
        if url in seen:
            continue
        seen.add(url)
        entries.append({"url": url, "note": note[:MAX_NOTE_CHARS]})
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"portfolio: at most {MAX_ENTRIES} URLs")
    return entries


def check_host(host):
    """Raise PageError unless every address the host resolves to is a public one"""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        raise PageError(f"{host} does not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global and not ALLOW_PRIVATE:
            raise PageError(f"{host} is not a public address")


def download(url):
    """Returns (final_url, html bytes); follows redirects itself so every hop is checked"""
    session = requests.Session()
    session.trust_env = False        # direct, not through any proxy in the environment
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PageError("not an http(s) URL")
        check_host(parts.hostname)
        with session.get(url, timeout=TIMEOUT_SECONDS, stream=True, allow_redirects=False,
                         headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                                  "Accept-Language": "ja,en;q=0.8"}) as resp:
            if resp.is_redirect and resp.headers.get("Location"):
                url = urljoin(url, resp.headers["Location"])
                continue
            if resp.status_code != 200:
                raise PageError(f"HTTP {resp.status_code}")
            kind = resp.headers.get("Content-Type", "").lower()
            if kind and "html" not in kind and "text/plain" not in kind:
                raise PageError(f"not a web page ({kind.split(';')[0]})")
            body = b""
            for chunk in resp.iter_content(65536):
                body += chunk
                if len(body) > MAX_PAGE_BYTES:
                    break
            return url, body
    raise PageError("too many redirects")


def extract(body):
    """(title, text) of a page: its description first, then the visible text"""
    soup = BeautifulSoup(body, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description = ""
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            description = tag["content"].strip()
            break
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)
    if description and description not in text[:500]:
        text = f"{description}\n{text}"
    return title[:200], text[:STORED_CHARS]


def read(store, url, force=False, max_age=PORTFOLIO_REFRESH_SECONDS):
    """The cached page, read again when it is stale; returns the pages row"""
    page = store.get_page(url)
    if page and not force:
        age = time.time() - page["fetched_ts"]
        if age < (max_age if page["status"] == "ok" else RETRY_SECONDS):
            return page
    try:
        _, body = download(url)
        title, text = extract(body)
        if not text:
            raise PageError("the page has no readable text (built by JavaScript?)")
        store.save_page(url, "ok", title=title, text=text)
    except (PageError, requests.RequestException) as e:
        error = str(e) if isinstance(e, PageError) else type(e).__name__
        if page and page["status"] == "ok":
            return page      # keep the last good copy
        store.save_page(url, "error", error=error[:200])
    return store.get_page(url)


def read_all(store, urls, force=False, max_age=PORTFOLIO_REFRESH_SECONDS):
    return {url: read(store, url, force, max_age) for url in urls}


def portfolio_status(entries, pages):
    """What the extension shows under the portfolio box"""
    out = []
    for e in entries:
        page = pages.get(e["url"])
        item = {"url": e["url"], "note": e["note"], "status": page["status"] if page else "reading"}
        if page:
            item.update(title=page["title"], chars=min(len(page["text"] or ""), PORTFOLIO_PAGE_CHARS),
                        error=page["error"], fetched_ts=page["fetched_ts"])
        out.append(item)
    return out


def job_links_block(urls, pages):
    """The linked pages of the 依頼内容, for the model's input"""
    if not urls:
        return ""
    lines = ["【依頼内容に記載されたURLのページ内容（システムが取得）】"]
    for url in urls:
        page = pages.get(url) or {}
        lines += ["", f"■ {url}"]
        if page.get("status") == "ok":
            if page.get("title"):
                lines.append(f"ページのタイトル: {page['title']}")
            lines.append(f"ページの内容（抜粋）:\n{page['text'][:JOB_PAGE_CHARS]}")
        else:
            lines.append(f"（取得できませんでした: {page.get('error') or '不明'}。このページの内容は推測しない）")
    return "\n".join(lines)


def portfolio_block(entries, pages):
    """The 過去のプロジェクト section of the model's input"""
    if not entries:
        return ""
    lines = ["【過去のプロジェクト（ポートフォリオ）】",
             "各URLのページ内容はシステムが取得済み。本人の担当範囲は「本人による説明」に書かれた範囲だけを事実として扱う。"]
    budget = PORTFOLIO_TOTAL_CHARS
    for i, e in enumerate(entries, 1):
        page = pages.get(e["url"]) or {}
        lines += ["", f"{i}. {e['url']}"]
        if e["note"]:
            lines.append(f"本人による説明: {e['note']}")
        if page.get("status") == "ok" and budget > 0:
            text = page["text"][:min(budget, PORTFOLIO_PAGE_CHARS)]
            budget -= len(text)
            if page.get("title"):
                lines.append(f"ページのタイトル: {page['title']}")
            lines.append(f"ページの内容（抜粋）:\n{text}")
        else:
            lines.append("（ページを取得できませんでした。本人による説明だけを使い、内容を推測しない）")
    return "\n".join(lines)

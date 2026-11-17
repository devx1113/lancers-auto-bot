"""Lancers search page scraping"""

import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from colorama import Fore, Style

from .config import LANCERS_PROXY, MAX_PAGES

# A proxy that can't be reached is skipped (Lancers is fetched directly) for this long, then tried again
PROXY_RETRY_SECONDS = 600
# Failures that mean "the proxy itself is down", not "Lancers didn't answer"
PROXY_DOWN_ERRORS = (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout, requests.exceptions.SSLError)

# type[] excludes the external 求人 ads that otherwise fill whole result pages; work_rank 1 = 市場価格と相違
SEARCH_URLS = [
    { 'category': "system", 'url': "https://www.lancers.jp/work/search/system?open=1&show_description=1&sort=started&work_rank%5B%5D=0&work_rank%5B%5D=1&work_rank%5B%5D=2&work_rank%5B%5D=3&type%5B%5D=project&type%5B%5D=competition&type%5B%5D=task" },
    { 'category': "web", 'url': "https://www.lancers.jp/work/search/web?open=1&show_description=1&sort=started&work_rank%5B%5D=0&work_rank%5B%5D=1&work_rank%5B%5D=2&work_rank%5B%5D=3&type%5B%5D=project&type%5B%5D=competition&type%5B%5D=task" }
]


def job_id_of(url):
    """Lancers work id from a /work/detail/<id> URL"""
    m = re.search(r"/work/detail/(\d+)", url or "")
    return int(m.group(1)) if m else None


class LancersScraper:
    def __init__(self, is_seen, proxy=LANCERS_PROXY):
        # is_seen(job_id) -> bool, used to stop paginating once known jobs appear
        self.is_seen = is_seen
        self.proxy = proxy
        self.proxy_down_until = 0      # while in the future, Lancers is fetched directly

    def get(self, session, url):
        """GET through the proxy when one is set and working; directly otherwise.

        A proxy that can't be reached is skipped for PROXY_RETRY_SECONDS (one warning, one Slack alert),
        so a dead proxy never stops the scraping."""
        use_proxy = self.proxy and time.time() >= self.proxy_down_until
        if use_proxy:
            try:
                return session.get(url, timeout=5, proxies={'http': self.proxy, 'https': self.proxy})
            except PROXY_DOWN_ERRORS as e:
                first = self.proxy_down_until == 0
                self.proxy_down_until = time.time() + PROXY_RETRY_SECONDS
                print(f"{Fore.YELLOW}⚠️  Proxy unreachable ({type(e).__name__}); fetching Lancers directly for "
                      f"{PROXY_RETRY_SECONDS // 60} min{Style.RESET_ALL}", flush=True)
                if first:
                    from . import slack
                    slack.alert(f"The Lancers proxy is unreachable ({type(e).__name__}). The scraper fetches Lancers directly "
                                f"and tries the proxy again every {PROXY_RETRY_SECONDS // 60} min.")
        return session.get(url, timeout=5, proxies={'http': None, 'https': None})

    def scrape_jobs(self):
        """Scrape job postings from Lancers"""
        print(f"{Fore.CYAN}🔍 Scraping Lancers for new job postings...{Style.RESET_ALL}")

        # Categories are fetched concurrently; each one fails independently of the others
        with ThreadPoolExecutor(max_workers=len(SEARCH_URLS)) as executor:
            results = executor.map(self.scrape_category, SEARCH_URLS)
        return [job for category_jobs in results for job in category_jobs]

    def scrape_category(self, base_url):
        """Scrape one category, following pages until an already-seen job appears"""
        jobs = []
        carousel_jobs = []
        # One session per thread (requests.Session isn't guaranteed thread-safe)
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        # Proxy (or not) per request, see get(); never from HTTP(S)_PROXY in the environment
        session.trust_env = False

        for page in range(1, MAX_PAGES + 1):
            url = base_url['url'] if page == 1 else f"{base_url['url']}&page={page}"
            print(f"{Fore.CYAN}Target URL: {url}{Style.RESET_ALL}")

            try:
                response = self.get(session, url)
                response.raise_for_status()
            except requests.RequestException as e:
                print(f"{Fore.RED}❌ Network error ({base_url['category']} p{page}): {e}{Style.RESET_ALL}")
                break

            soup = BeautifulSoup(response.content, 'html.parser')

            if page == 1:
                # The "新着の仕事" carousel also lists jobs that logged-out search results hide (e.g. flagged 市場価格と相違)
                for item in soup.select(".p-search-job__latest-carousel-item"):
                    try:
                        job = self.parse_card(item, base_url['category'])
                    except Exception as e:
                        print(f"{Fore.RED}❌ Scraping error ({base_url['category']} carousel): {e}{Style.RESET_ALL}")
                        continue
                    if job:
                        carousel_jobs.append(job)

            job_cards = soup.select(".p-search-job-media")
            if not job_cards:
                if page == 1:
                    print(f"{Fore.YELLOW}⚠️  Could not find Job posts ({base_url['category']}){Style.RESET_ALL}")
                break

            page_jobs = []
            for card in job_cards:
                try:
                    job = self.parse_card(card, base_url['category'])
                except Exception as e:
                    print(f"{Fore.RED}❌ Scraping error ({base_url['category']} p{page}): {e}{Style.RESET_ALL}")
                    continue
                if job:
                    page_jobs.append(job)
            jobs.extend(page_jobs)

            # Older pages only hold older jobs; pinned PR jobs reappear on top, so they don't count
            if any(self.is_seen(job['id']) for job in page_jobs if not job['pr_job']):
                break

        listed = {job['id'] for job in jobs}
        jobs.extend(job for job in carousel_jobs if job['id'] not in listed)
        return jobs

    def parse_card(self, card, category):
        """Parse a single job card; returns None for cards that should be skipped"""
        # タイトル
        # Search result cards and "新着の仕事" carousel items use different title classes
        title_tag = card.select_one(".p-search-job-media__title, .p-search-job__latest-media-title")

        new_job_tag = card.select_one(".p-search-job-media__tag--new")
        new_job = new_job_tag.get_text(strip=True) if new_job_tag else None

        pr_job_tag = card.select_one(".p-search-job-media__tag--top")
        pr_job = pr_job_tag.get_text(strip=True) if pr_job_tag else None

        post_cnt_tag = card.select_one(".p-search-job-media__tag--inexperiencedCount")
        post_cnt = post_cnt_tag.get_text(strip=True) if post_cnt_tag else None

        # Tags (NEW, PR, ...) are nested inside the title link; drop them so they don't prefix the title
        if title_tag:
            for tags in title_tag.select(".p-search-job-media__tags"):
                tags.decompose()
        title = title_tag.get_text(strip=True) if title_tag else None

        # Any work type badge: project / competition / task / recruit
        work_type_tag = card.select_one('[class*="c-badge--worktype-"]')
        work_type = work_type_tag.get_text(strip=True) if work_type_tag else None
        if work_type == "求人":
            return None

        # 詳細ページURL
        link = title_tag.get("href") if title_tag else None
        if link and not link.startswith("http"):
            link = "https://www.lancers.jp" + link
        # Anything that isn't a job detail page (ads, external links) has no work id
        job_id = job_id_of(link) if link and link.startswith("https://www.lancers.jp/work/detail/") else None
        if not job_id:
            return None

        # 報酬
        price = card.select_one(".p-search-job-media__price")
        # "10,000 円 ~ 20,000 円 / 固定" -> "10,000円 ~ 20,000円 / 固定"
        price_text = re.sub(r"\s+円", "円", price.get_text(" ", strip=True)) if price else None

        # 募集状況
        status = card.select_one(".p-search-job-media__time-text")
        status_text = status.get_text(strip=True) if status else None
        remain_tag = card.select_one(".p-search-job-media__time-remaining")
        remain = remain_tag.get_text(strip=True) if remain_tag else None

        # 説明文
        # Cards have two description blocks: requirement tags, then the job text excerpt
        descs = [d.get_text("\n", strip=True) for d in card.select(".c-media__description")]
        desc_text = "\n\n".join(d for d in descs if d) or None

        client_avatar_tag = card.select_one(".c-avatar__image")
        client_avatar = client_avatar_tag.get("src") if client_avatar_tag else None
        client_name_tag = card.select_one(".p-search-job-media__avatar-note")
        client_name = client_name_tag.get_text(strip=True) if client_name_tag else None
        client_link_tag = card.select_one('a[href^="/client/"]')
        client_url = "https://www.lancers.jp" + client_link_tag.get("href") if client_link_tag else None

        # Divisions list: job sub-category first, then the client's industry
        divisions = [li.get_text(strip=True) for li in card.select(".p-search-job__division")]
        job_category = divisions[0] if divisions else None
        industry = divisions[1] if len(divisions) > 1 else None

        return {
            "id": job_id,
            "title": title,
            "url": link,
            "price": price_text,
            "status": status_text,
            'remain': remain,
            'work_type': work_type,
            'description': desc_text,
            'client_avatar': client_avatar,
            'client_name': client_name,
            'client_url': client_url,
            'job_category': job_category,
            'industry': industry,
            'post_cnt': post_cnt,
            'new_job': new_job,
            'pr_job': pr_job,
            'category': category
        }

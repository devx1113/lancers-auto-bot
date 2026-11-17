"""Slack notifications: one message per category batch, a 2-column grid per job"""

from urllib.error import HTTPError

from colorama import Fore, Style
from slack_webhook import Slack

from .config import SLACK_JOBS_PER_MESSAGE, SLACK_WEBHOOK_ALERTS, SLACK_WEBHOOK_BIDS, SLACK_WEBHOOKS


def job_blocks(job, with_avatar=True):
    """Build the Slack blocks for one job: title + avatar, a 2-column grid of details, and a copyable URL"""
    def esc(text):
        # Slack mrkdwn requires &, <, > to be escaped
        return (text or '-').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def field(label, value):
        return {"type": "mrkdwn", "text": f"*{label}:*\n{value}"}

    client = esc(job.get('client_name'))
    if job.get('client_name') and job.get('client_url'):
        client = f"<{job['client_url']}|{client}>"

    title = {"type": "section", "text": {"type": "mrkdwn", "text": f"🔔 *{esc(job['title'])}*"}}
    if with_avatar and job.get('client_avatar'):
        title["accessory"] = {"type": "image", "image_url": job['client_avatar'], "alt_text": job.get('client_name') or "client"}

    return [
        title,
        # Section fields render as a 2-column grid, filled left to right
        {"type": "section", "fields": [
            field("Source", esc(job['category'])),
            field("Type", esc(job.get('work_type'))),
            field("Budget", esc(job.get('price'))),
            field("Client", client),
            field("Category", esc(job.get('job_category'))),
            field("Industry", esc(job.get('industry'))),
        ]},
        {"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": "ジョブリンク（コピー用）", "style": {"bold": True}}]},
            # Code block: Slack shows a copy button on it
            {"type": "rich_text_preformatted", "elements": [{"type": "text", "text": job['url']}]},
        ]},
    ]


def send_slack_notification(category, jobs):
    """Send a batch of jobs from one category to its slack channel as a single message"""
    try:
        webhook_url = SLACK_WEBHOOKS.get(category)
        if not webhook_url:
            print(f"{Fore.YELLOW}⚠️  No webhook URL found for category: {category}{Style.RESET_ALL}")
            return False

        def build_blocks(with_avatar):
            blocks = []
            for i, job in enumerate(jobs):
                if i:
                    blocks.append({"type": "divider"})
                blocks.extend(job_blocks(job, with_avatar))
            return blocks

        def post(blocks):
            return Slack(url=webhook_url).post(
                text=f"{len(jobs)} new job(s) in {category}: " + ", ".join(job['title'] or '' for job in jobs),  # notification fallback; not shown when blocks render
                blocks=blocks,
                # Disable the link preview cards for the job URLs
                unfurl_links=False,
                unfurl_media=False,
            )

        try:
            response_text = post(build_blocks(with_avatar=True))
        except HTTPError as e:
            if e.code != 400:
                raise
            # Slack rejects the whole message if it can't download an avatar; resend without them
            print(f"{Fore.YELLOW}⚠️  Slack rejected the message ({e}); retrying without avatars{Style.RESET_ALL}")
            response_text = post(build_blocks(with_avatar=False))

        if response_text:
            print(f"{Fore.GREEN}✅ Posted {len(jobs)} job(s) to slack: {category}{Style.RESET_ALL}")
            return True
        else:
            print(f"{Fore.RED}❌ Failed to post to slack{Style.RESET_ALL}")
            return False

    except Exception as e:
        print(f"{Fore.RED}❌ slack webhook error: {e}{Style.RESET_ALL}")
        return False


def notify(new_jobs):
    """One Slack message per category (split into chunks) instead of one per job"""
    by_category = {}
    for job in new_jobs:
        by_category.setdefault(job['category'], []).append(job)
    for category, category_jobs in by_category.items():
        for i in range(0, len(category_jobs), SLACK_JOBS_PER_MESSAGE):
            send_slack_notification(category, category_jobs[i:i + SLACK_JOBS_PER_MESSAGE])


def post_line(webhook_url, text):
    """A plain one-line message (bid results, alerts); logged only when no webhook is set"""
    print(text, flush=True)
    if not webhook_url:
        return False
    try:
        Slack(url=webhook_url).post(text=text, unfurl_links=False, unfurl_media=False)
        return True
    except Exception as e:
        print(f"{Fore.RED}❌ slack webhook error: {e}{Style.RESET_ALL}", flush=True)
        return False


def alert(text):
    return post_line(SLACK_WEBHOOK_ALERTS, f"⚠️ {text}")


BID_ICONS = {"submitted": "✅", "failed": "❌", "manual": "✋", "dryrun": "🧪"}


def bid_result(user_name, title, url, status, message):
    icon = BID_ICONS.get(status, "•")
    return post_line(SLACK_WEBHOOK_BIDS, f"{icon} [{user_name}] {status}: {title} {url}" + (f"\n> {message}" if message else ""))

#!/usr/bin/env python3
"""
Lancers Job Scraping Bot
Detects new job postings from Lancers, posts them to Slack and records them for the extension server
"""

import sys
import time

from colorama import init, Fore, Style

from lancers_bot import slack
from lancers_bot.config import SEEN_JOBS_FILE
from lancers_bot.scraper import LancersScraper
from lancers_bot.store import Store

# Initialize colorama for colored console output
init()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class LancersBot:
    def __init__(self):
        self.store = Store()
        imported = self.store.import_seen_file(SEEN_JOBS_FILE)
        if imported:
            print(f"{Fore.CYAN}Imported {imported} seen job(s) from {SEEN_JOBS_FILE}{Style.RESET_ALL}")
        pruned = self.store.prune_events()
        if pruned:
            print(f"{Fore.CYAN}Pruned {pruned} old event(s){Style.RESET_ALL}")
        self.scraper = LancersScraper(self.store.is_seen)

    def display_jobs(self, jobs, slack_mode):
        """Record new jobs (the server sends them to the extension) and send them to slack"""
        if not jobs:
            print(f"{Fore.YELLOW}📭 No new job postings found.{Style.RESET_ALL}")
            return
        new_jobs = self.store.add_jobs(jobs)
        if slack_mode:
            slack.notify(new_jobs)

        print(f"\n{Fore.GREEN}🎉 Found {len(new_jobs)} new job posting(s)!{Style.RESET_ALL}\n")
        print("=" * 80)

    def run(self, continuous = False, interval = 2, slack_mode = False):
        """Run the scraper"""
        print(f"{Fore.CYAN}Lancers Job Scraping Bot Started{Style.RESET_ALL}")
        print(f"{Fore.CYAN}Continuous mode: {'ON' if continuous else 'OFF'}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}slack mode: {'ON' if slack_mode else 'OFF'}{Style.RESET_ALL}")

        if continuous:
            print(f"{Fore.CYAN}Checking every {interval} seconds...{Style.RESET_ALL}")
            if slack_mode:
                print(f"{Fore.GREEN}New jobs will be posted to slack channels by category{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}Press Ctrl+C to stop{Style.RESET_ALL}\n")

        try:
            while True:
                jobs = self.scraper.scrape_jobs()
                self.display_jobs(jobs, slack_mode)

                if not continuous:
                    break

                print(f"\n{Fore.CYAN}Waiting {interval} seconds before next check...{Style.RESET_ALL}")
                time.sleep(interval)

        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}👋 Bot stopped by user{Style.RESET_ALL}")
        except Exception as e:
            print(f"\n{Fore.RED}❌ Bot error: {e}{Style.RESET_ALL}")

def main():
    """Main function"""
    bot = LancersBot()

    # Parse command line arguments
    continuous = '--continuous' in sys.argv or '-c' in sys.argv
    slack_mode = '--slack' in sys.argv or '-d' in sys.argv
    interval = 2 if slack_mode else 300  # 2 seconds for slack mode, 5 minutes default

    # If slack mode is enabled, force continuous mode
    if slack_mode and not continuous:
        continuous = True
        print(f"{Fore.GREEN}slack mode enabled - switching to continuous mode{Style.RESET_ALL}")

    if continuous:
        # Check for custom interval
        for arg in sys.argv:
            if arg.startswith('--interval='):
                try:
                    interval = int(arg.split('=')[1])
                except ValueError:
                    print(f"{Fore.RED}Invalid interval format. Using default interval.{Style.RESET_ALL}")

    bot.run(continuous=continuous, interval=interval, slack_mode=slack_mode)

if __name__ == "__main__":
    main()

"""
LinkedIn Outreach Bot
Sends personalized connection requests to recruiters, HR, and senior engineers
at target companies.

Usage:
    pip install selenium undetected-chromedriver
    python linkedin_bot.py
    python linkedin_bot.py --companies "Barclays,Microsoft"
    python linkedin_bot.py --dry-run
"""

import json
import csv
import time
import random
import os
import sys
import argparse
from datetime import datetime

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
        ElementClickInterceptedException, StaleElementReferenceException
    )
except ImportError:
    print("=" * 60)
    print("Missing dependencies! Run:")
    print("  pip install selenium undetected-chromedriver")
    print("=" * 60)
    sys.exit(1)


# ─── Paths ────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def random_delay(range_tuple):
    """Sleep a random amount within [low, high] seconds."""
    lo, hi = range_tuple
    delay = random.uniform(lo, hi)
    time.sleep(delay)


# ─── Logger ───────────────────────────────────────────────────
class OutreachLogger:
    def __init__(self, log_file):
        self.log_path = os.path.join(SCRIPT_DIR, log_file)
        self.rows = []
        # Create file with header if it doesn't exist
        if not os.path.exists(self.log_path):
            with open(self.log_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "name", "company", "headline",
                    "profile_url", "note_sent", "status"
                ])

    def log(self, name, company, headline, profile_url, note, status):
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            name, company, headline, profile_url, note, status
        ]
        self.rows.append(row)
        with open(self.log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)
        print(f"  [{status}] {name} @ {company}")

    def summary(self):
        sent = sum(1 for r in self.rows if r[6] == "SENT")
        skipped = sum(1 for r in self.rows if r[6] == "SKIPPED")
        failed = sum(1 for r in self.rows if r[6] == "FAILED")
        print(f"\n{'=' * 50}")
        print(f"Session Summary: {sent} sent, {skipped} skipped, {failed} failed")
        print(f"Log saved to: {self.log_path}")
        print(f"{'=' * 50}")


# ─── Browser Setup ────────────────────────────────────────────
def create_driver(headless=False):
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=en-US")

    driver = uc.Chrome(options=options)
    driver.implicitly_wait(5)
    return driver


# ─── LinkedIn Login ───────────────────────────────────────────
def linkedin_login(driver, email, password):
    print("\n[*] Logging into LinkedIn...")
    driver.get("https://www.linkedin.com/login")
    time.sleep(3)

    try:
        email_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "username"))
        )
        email_field.clear()
        email_field.send_keys(email)

        pass_field = driver.find_element(By.ID, "password")
        pass_field.clear()
        pass_field.send_keys(password)
        pass_field.send_keys(Keys.RETURN)

        # Wait for login to complete
        time.sleep(5)

        # Check if we need to handle security verification
        if "checkpoint" in driver.current_url or "challenge" in driver.current_url:
            print("\n[!] LinkedIn security check detected!")
            print("    Please complete the verification in the browser window.")
            print("    Waiting up to 120 seconds...")
            WebDriverWait(driver, 120).until(
                lambda d: "feed" in d.current_url or "mynetwork" in d.current_url
            )

        if "feed" in driver.current_url or "mynetwork" in driver.current_url:
            print("[+] Login successful!")
            return True
        else:
            print(f"[!] Login may have failed. Current URL: {driver.current_url}")
            print("    If a captcha appeared, solve it manually. Waiting 60s...")
            time.sleep(60)
            return "feed" in driver.current_url or "mynetwork" in driver.current_url

    except TimeoutException:
        print("[X] Login timed out.")
        return False


# ─── Build the personalized note ─────────────────────────────
def build_note(template, first_name, company, role_interest):
    return template.format(
        first_name=first_name,
        company=company,
        role_interest=role_interest
    )


# ─── Search & Send Requests ──────────────────────────────────
def search_and_connect(driver, company, config, logger, sent_count, dry_run=False):
    """Search LinkedIn for people at a company and send connection requests."""

    role_interest = config["role_interest"]
    note_template = config["note_template"]
    max_per_company = config["max_requests_per_company"]
    delay_range = config["delay_between_requests_sec"]
    company_sent = 0

    for role_keyword in config["target_roles"]:
        if company_sent >= max_per_company:
            break
        if sent_count[0] >= config["max_total_requests_per_session"]:
            print(f"\n[!] Reached max session limit ({config['max_total_requests_per_session']}). Stopping.")
            return company_sent

        search_query = f"{role_keyword} {company}"
        print(f"\n[*] Searching: '{search_query}'...")

        # Use LinkedIn people search
        search_url = (
            f"https://www.linkedin.com/search/results/people/"
            f"?keywords={search_query.replace(' ', '%20')}"
            f"&origin=GLOBAL_SEARCH_HEADER"
        )
        driver.get(search_url)
        time.sleep(random.uniform(3, 6))

        # Scroll to load results
        for _ in range(3):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(1)

        # Find all Connect buttons on the page
        try:
            results = driver.find_elements(
                By.CSS_SELECTOR,
                'div.entity-result__item, li.reusable-search__result-container'
            )
        except Exception:
            results = []

        if not results:
            print(f"  No results found for '{search_query}'")
            continue

        print(f"  Found {len(results)} results")

        for result in results:
            if company_sent >= max_per_company:
                break
            if sent_count[0] >= config["max_total_requests_per_session"]:
                return company_sent

            try:
                # Extract person's name
                try:
                    name_el = result.find_element(
                        By.CSS_SELECTOR,
                        'span.entity-result__title-text a span[aria-hidden="true"], '
                        'span.entity-result__title-text span[dir="ltr"] span[aria-hidden="true"]'
                    )
                    full_name = name_el.text.strip()
                except NoSuchElementException:
                    try:
                        name_el = result.find_element(
                            By.CSS_SELECTOR,
                            '.entity-result__title-text a'
                        )
                        full_name = name_el.text.strip().split('\n')[0]
                    except NoSuchElementException:
                        continue

                if not full_name or full_name.lower() == "linkedin member":
                    continue

                first_name = full_name.split()[0]

                # Extract headline
                try:
                    headline_el = result.find_element(
                        By.CSS_SELECTOR,
                        'div.entity-result__primary-subtitle'
                    )
                    headline = headline_el.text.strip()
                except NoSuchElementException:
                    headline = ""

                # Extract profile URL
                try:
                    profile_link = result.find_element(
                        By.CSS_SELECTOR,
                        'a.app-aware-link[href*="/in/"]'
                    )
                    profile_url = profile_link.get_attribute("href").split("?")[0]
                except NoSuchElementException:
                    profile_url = ""

                # Check if there's a Connect button
                try:
                    connect_btn = result.find_element(
                        By.XPATH,
                        './/button[contains(@aria-label, "Invite") and contains(@aria-label, "to connect")]'
                    )
                except NoSuchElementException:
                    # Try alternate selectors
                    try:
                        connect_btn = result.find_element(
                            By.XPATH,
                            './/button[.//span[text()="Connect"]]'
                        )
                    except NoSuchElementException:
                        logger.log(full_name, company, headline, profile_url, "", "SKIPPED")
                        continue

                # Build personalized note
                note = build_note(note_template, first_name, company, role_interest)

                if len(note) > 300:
                    note = note[:297] + "..."

                if dry_run:
                    print(f"  [DRY RUN] Would send to {full_name}: {note[:60]}...")
                    logger.log(full_name, company, headline, profile_url, note, "DRY_RUN")
                    company_sent += 1
                    sent_count[0] += 1
                    continue

                # Click Connect
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", connect_btn)
                    time.sleep(0.5)
                    connect_btn.click()
                    time.sleep(2)
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", connect_btn)
                    time.sleep(2)

                # Look for "Add a note" button in the modal
                try:
                    add_note_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((
                            By.XPATH,
                            '//button[contains(@aria-label, "Add a note")]'
                        ))
                    )
                    add_note_btn.click()
                    time.sleep(1)

                    # Type the note
                    note_field = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((
                            By.CSS_SELECTOR,
                            'textarea[name="message"], textarea#custom-message'
                        ))
                    )
                    note_field.clear()
                    note_field.send_keys(note)
                    time.sleep(1)

                    # Click Send
                    send_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((
                            By.XPATH,
                            '//button[contains(@aria-label, "Send") or .//span[text()="Send"]]'
                        ))
                    )
                    send_btn.click()
                    time.sleep(2)

                    logger.log(full_name, company, headline, profile_url, note, "SENT")
                    company_sent += 1
                    sent_count[0] += 1

                except TimeoutException:
                    # No "Add a note" option — might be Send directly
                    try:
                        send_btn = driver.find_element(
                            By.XPATH,
                            '//button[contains(@aria-label, "Send without a note") or contains(@aria-label, "Send now")]'
                        )
                        # We want to send WITH a note, so dismiss and skip
                        dismiss_btn = driver.find_element(
                            By.XPATH,
                            '//button[contains(@aria-label, "Dismiss") or contains(@aria-label, "close") or .//span[text()="Dismiss"]]'
                        )
                        dismiss_btn.click()
                        logger.log(full_name, company, headline, profile_url, "", "SKIPPED")
                    except NoSuchElementException:
                        logger.log(full_name, company, headline, profile_url, "", "FAILED")

                # Rate-limit delay
                random_delay(delay_range)

            except StaleElementReferenceException:
                continue
            except Exception as e:
                print(f"  [!] Error processing a result: {e}")
                continue

        # Delay between role searches
        random_delay(config["delay_between_searches_sec"])

    return company_sent


# ─── Interactive Setup ────────────────────────────────────────
def interactive_setup(config):
    """Ask user for all inputs at runtime."""
    import getpass

    print("=" * 60)
    print("  LinkedIn Outreach Bot - Setup")
    print("=" * 60)

    # ── Login credentials ──
    email = input("\n  LinkedIn Email: ").strip()
    password = getpass.getpass("  LinkedIn Password: ")
    config["linkedin_email"] = email
    config["linkedin_password"] = password

    # ── Target companies ──
    print("\n  Enter company names to target (one per line).")
    print("  Press Enter on an empty line when done.\n")
    companies = []
    while True:
        company = input("  Company: ").strip()
        if not company:
            if not companies:
                print("  [!] Enter at least one company.")
                continue
            break
        companies.append(company)
    config["target_companies"] = companies

    # ── Role interest ──
    role = input(f"\n  Role to mention in note [{config.get('role_interest', 'Software Engineering')}]: ").strip()
    if role:
        config["role_interest"] = role

    # ── Note template ──
    print(f"\n  Current note template:")
    print(f"  \"{config['note_template']}\"")
    change = input("  Change it? (y/N): ").strip().lower()
    if change == "y":
        print("  Use {first_name}, {company}, {role_interest} as placeholders.")
        new_template = input("  New template: ").strip()
        if new_template:
            config["note_template"] = new_template

    # ── Max requests ──
    max_per = input(f"\n  Max requests per company [{config['max_requests_per_company']}]: ").strip()
    if max_per.isdigit():
        config["max_requests_per_company"] = int(max_per)

    max_total = input(f"  Max total requests this session [{config['max_total_requests_per_session']}]: ").strip()
    if max_total.isdigit():
        config["max_total_requests_per_session"] = int(max_total)

    # ── Dry run ──
    dry = input("\n  Dry run (test without sending)? (y/N): ").strip().lower()
    dry_run = dry == "y"

    return config, dry_run


# ─── Main ─────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="LinkedIn Outreach Bot")
    parser.add_argument(
        "--no-interactive", action="store_true",
        help="Skip interactive prompts, use config.json values directly"
    )
    parser.add_argument(
        "--companies", type=str, default=None,
        help='Comma-separated company names. E.g. "Barclays,Microsoft"'
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without actually sending requests"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run browser in headless mode"
    )
    args = parser.parse_args()

    config = load_config()
    dry_run = args.dry_run

    if not args.no_interactive:
        # ── Interactive mode: ask everything ──
        config, dry_run = interactive_setup(config)
    else:
        # ── Non-interactive: use config.json + CLI overrides ──
        if args.companies:
            config["target_companies"] = [c.strip() for c in args.companies.split(",")]

    if args.headless:
        config["headless"] = True

    print("\n" + "=" * 60)
    print("  LinkedIn Outreach Bot - Starting")
    print("=" * 60)
    print(f"  Companies : {', '.join(config['target_companies'])}")
    print(f"  Role      : {config['role_interest']}")
    print(f"  Max/Co    : {config['max_requests_per_company']}")
    print(f"  Max Total : {config['max_total_requests_per_session']}")
    print(f"  Dry Run   : {dry_run}")
    print("=" * 60)

    logger = OutreachLogger(config["log_file"])
    driver = None

    try:
        driver = create_driver(headless=config["headless"])

        if not linkedin_login(driver, config["linkedin_email"], config["linkedin_password"]):
            print("[X] Could not log in. Exiting.")
            return

        sent_count = [0]  # mutable counter

        for company in config["target_companies"]:
            if sent_count[0] >= config["max_total_requests_per_session"]:
                break

            print(f"\n{'─' * 50}")
            print(f"  Company: {company}")
            print(f"{'─' * 50}")

            count = search_and_connect(
                driver, company, config, logger, sent_count, dry_run=dry_run
            )
            print(f"\n  => Sent {count} requests for {company}")

            # Longer delay between companies
            if company != config["target_companies"][-1]:
                wait = random.uniform(15, 30)
                print(f"  Waiting {wait:.0f}s before next company...")
                time.sleep(wait)

    except KeyboardInterrupt:
        print("\n\n[!] Interrupted by user.")
    except Exception as e:
        print(f"\n[X] Fatal error: {e}")
    finally:
        logger.summary()
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()

"""
PSA Auction Prices link crawler
--------------------------------
Crawls every result link of:
    https://www.psacard.com/auctionprices/search?q=pokemon&category=4
and saves them into a SQLite table that holds only the links:

    CREATE TABLE links(
    link VARCHAR(255) NOT NULL
    UNIQUE
    )

How it works
- Pages are opened directly by URL:  ...&page=1, &page=2, ...
- Uses a persistent Chrome profile (./chrome_profile). The first run opens the
  PSA sign-in page; you log in yourself (email -> password -> one-time code).
  Later runs reuse that session, so you usually won't need to log in again.
- Result links:   <a data-testid="link" href="/spec/psa/...">
- Finds the end from the page itself: it stops after a page with no
  enabled next-arrow button (<svg data-testid="arrow-right-filled-icon">),
  or a page that says "no results".
- A page that loads without links is NOT treated as the end: it reloads up
  to 3 times, then tries clicking the next arrow from the previous page, and
  if that still fails it stops and saves a screenshot + HTML to ./debug/.
- Resumes: the last finished page is stored in progress.txt, so a rerun
  continues from the next page. Start over with:  python main.py --restart
  Start from a specific page with:                python main.py --page 25
- Stops cleanly (with the reason) if it's blocked: login not completed,
  session expired, captcha / bot challenge, access denied, rate limit.

Requirements:  pip install selenium
(Selenium 4.6+ downloads the matching ChromeDriver automatically.)
"""

import argparse
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------- settings ----------------
SEARCH_BASE = "https://www.psacard.com/auctionprices/search"
SEARCH_PARAMS = {"q": "pokemon", "category": "4"}
BASE_URL = "https://www.psacard.com"
DB_PATH = "psa_links.db"
PROGRESS_FILE = Path("progress.txt")
PROFILE_DIR = str(Path("chrome_profile").resolve())
LOGIN_TIMEOUT = 300          # seconds you have to finish logging in
PAGE_TIMEOUT = 20            # seconds to wait for a results page
DELAY_RANGE = (3.0, 6.0)     # polite pause between pages (seconds)
MAX_PAGES = 5000             # safety stop

LINK_SELECTOR = 'a[data-testid="link"][href*="/spec/psa/"]'
NEXT_ARROW_SELECTOR = 'svg[data-testid="arrow-right-filled-icon"]'
NO_RESULTS_RE = re.compile(r"\bno (results|items|matches)\b|(?<![\d,.])\b0 results\b")
RETRIES = 3                  # reloads before falling back to the arrow button
DEBUG_DIR = Path("debug")

# Hard blocks: the script stops.
BLOCK_MARKERS = {
    "access denied": "access denied (IP or bot block)",
    "too many requests": "rate limited (HTTP 429)",
    "error 1020": "Cloudflare firewall block (error 1020)",
    "you have been blocked": "Cloudflare block",
}
# Human checks (Cloudflare "Just a moment...", CAPTCHA): the script pauses so
# you can finish the check yourself in the Chrome window, then it continues.
CHALLENGE_MARKERS = (
    "just a moment", "verifying you are human", "verify you are human",
    "checking your browser", "checking if the site connection is secure",
    "needs to review the security of your connection", "captcha",
    "attention required",
)
CHALLENGE_TIMEOUT = 180      # seconds you have to clear a human check


class Blocked(Exception):
    """Raised when the site stops us; the message explains why."""


def page_url(page: int) -> str:
    return f"{SEARCH_BASE}?{urlencode({**SEARCH_PARAMS, 'page': page})}"


# ---------------- database / progress ----------------
LINKS_DDL = """CREATE TABLE links(
link VARCHAR(255) NOT NULL
UNIQUE
)"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT sql FROM sqlite_master "
                       "WHERE type='table' AND name='links'").fetchone()
    if row is None:
        conn.execute(LINKS_DDL)
    elif "VARCHAR(255)" not in row[0]:
        # Older run used a different schema: migrate the saved links over.
        conn.execute("ALTER TABLE links RENAME TO links_old")
        conn.execute(LINKS_DDL)
        conn.execute("INSERT OR IGNORE INTO links (link) SELECT link FROM links_old")
        conn.execute("DROP TABLE links_old")
    conn.commit()
    return conn


def save_links(conn: sqlite3.Connection, links: list[str]) -> int:
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO links (link) VALUES (?)",
                     [(l,) for l in links])
    conn.commit()
    return conn.total_changes - before


def read_progress() -> int:
    try:
        return int(PROGRESS_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_progress(page: int) -> None:
    PROGRESS_FILE.write_text(str(page))


# ---------------- browser ----------------
def make_driver() -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--window-size=1400,1000")
    return webdriver.Chrome(options=opts)


def visible_text(driver) -> str:
    try:
        return (driver.title + "\n" +
                driver.find_element(By.TAG_NAME, "body").text).lower()
    except NoSuchElementException:
        return driver.title.lower()


def on_challenge_page(driver) -> bool:
    text = visible_text(driver)
    return any(m in text for m in CHALLENGE_MARKERS)


def check_blocked(driver) -> None:
    """Stop on hard blocks; pause on human checks until you clear them.

    Checks the VISIBLE text and title only - the raw HTML of normal pages can
    contain words like "recaptcha" in scripts, which would be a false alarm.
    """
    text = visible_text(driver)
    for marker, reason in BLOCK_MARKERS.items():
        if marker in text:
            raise Blocked(f"Blocked by {reason} at {driver.current_url}")

    if on_challenge_page(driver):
        print("\n[check] PSA/Cloudflare is showing a 'verify you are human' "
              "check in the Chrome window.")
        print(f"        Complete it there yourself. Waiting up to "
              f"{CHALLENGE_TIMEOUT // 60} minutes...")
        deadline = time.time() + CHALLENGE_TIMEOUT
        while time.time() < deadline:
            time.sleep(3)
            if not on_challenge_page(driver):
                print("        Check cleared - continuing.\n")
                time.sleep(2)
                return
        raise Blocked("Bot check (Cloudflare / CAPTCHA) was not cleared in "
                      f"time at {driver.current_url}. PSA flagged the "
                      "automated browser. Wait a while and rerun more slowly.")


def on_signin_page(driver) -> bool:
    return "/signin" in driver.current_url


def ensure_logged_in(driver, first_url: str) -> None:
    driver.get(first_url)
    time.sleep(2)
    if not on_signin_page(driver):
        return

    print("\n[login] PSA redirected to the sign-in page.")
    print("        Log in in the Chrome window (email -> password -> one-time code).")
    print(f"        Waiting up to {LOGIN_TIMEOUT // 60} minutes...\n")

    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        time.sleep(3)
        if not on_signin_page(driver):
            break
    else:
        raise Blocked("Login declined / not completed: still on the sign-in "
                      "page after the timeout. PSA requires a logged-in "
                      "session to view search results.")

    driver.get(first_url)
    time.sleep(2)
    if on_signin_page(driver):
        raise Blocked("Login declined: PSA sent the browser back to sign-in "
                      "after logging in (session not accepted).")


def page_links(driver) -> list[str]:
    links = []
    for a in driver.find_elements(By.CSS_SELECTOR, LINK_SELECTOR):
        href = a.get_attribute("href")
        if href:
            links.append(urljoin(BASE_URL, href))
    return links


def has_next_page(driver) -> bool:
    """True if the right-arrow (next page) button exists and is enabled."""
    for svg in driver.find_elements(By.CSS_SELECTOR, NEXT_ARROW_SELECTOR):
        try:
            btn = svg.find_element(By.XPATH, "./ancestor::button[1]")
        except NoSuchElementException:
            continue
        cls = (btn.get_attribute("class") or "").lower()
        disabled = (btn.get_attribute("disabled") is not None
                    or btn.get_attribute("aria-disabled") == "true"
                    or "cursor-not-allowed" in cls
                    or "pointer-events-none" in cls)
        if not disabled:
            return True
    return False


def shows_no_results(driver) -> bool:
    # Regex so "1,250 results" does NOT count as "0 results".
    text = driver.find_element(By.TAG_NAME, "body").text.lower()
    return NO_RESULTS_RE.search(text) is not None


def wait_for_links(driver, timeout: int) -> list[str]:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
            and d.find_elements(By.CSS_SELECTOR, LINK_SELECTOR))
    except TimeoutException:
        return []
    time.sleep(0.5)                      # let the list finish rendering
    return page_links(driver)


def save_debug(driver, page: int) -> str:
    DEBUG_DIR.mkdir(exist_ok=True)
    base = DEBUG_DIR / f"page_{page}"
    driver.save_screenshot(str(base.with_suffix(".png")))
    base.with_suffix(".html").write_text(driver.page_source, encoding="utf-8")
    return str(base)


def load_page(driver, page: int, prev_links) -> list[str]:
    """Open one results page by URL, retrying; fall back to the arrow button."""
    url = page_url(page)
    for attempt in range(1, RETRIES + 1):
        if attempt == 1:
            driver.get(url)
        else:
            print(f"  page {page}: no links yet, reloading (try {attempt}/{RETRIES})")
            time.sleep(attempt * 3)
            driver.refresh()
        if on_signin_page(driver):
            raise Blocked("Session expired: redirected to sign-in mid-crawl. "
                          "Rerun the script to log in and resume.")
        check_blocked(driver)
        links = wait_for_links(driver, PAGE_TIMEOUT)
        if not links:
            check_blocked(driver)          # a check may appear after loading
            links = wait_for_links(driver, 5)
        if links and links != prev_links:
            return links
        if shows_no_results(driver):
            return []

    # Fallback: open the previous page and click the right arrow like a person.
    if page > 1:
        print(f"  page {page}: trying the next-arrow button from page {page - 1}")
        driver.get(page_url(page - 1))
        if wait_for_links(driver, PAGE_TIMEOUT) and has_next_page(driver):
            svg = driver.find_elements(By.CSS_SELECTOR, NEXT_ARROW_SELECTOR)[-1]
            btn = svg.find_element(By.XPATH, "./ancestor::button[1]")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            driver.execute_script("arguments[0].click();", btn)
            try:
                WebDriverWait(driver, PAGE_TIMEOUT).until(
                    lambda d: page_links(d) and page_links(d) != prev_links)
                return page_links(driver)
            except TimeoutException:
                pass

    path = save_debug(driver, page)
    raise Blocked(f"Page {page} did not load its results (this is NOT the end - "
                  f"page {page - 1} had a next button). Screenshot and HTML saved "
                  f"to {path}.png / .html - open them to see what PSA showed.")


# ---------------- main ----------------
def crawl(start_page: int) -> None:
    conn = open_db(DB_PATH)
    driver = make_driver()
    page, total_new, prev = start_page, 0, None
    print(f"Starting at page {start_page}: {page_url(start_page)}")
    try:
        ensure_logged_in(driver, page_url(start_page))
        check_blocked(driver)

        while page < start_page + MAX_PAGES:
            links = load_page(driver, page, prev)
            if not links:
                if page == start_page:
                    raise Blocked(f"No results on page {page} (start page).")
                print(f"[done] Page {page} says there are no results - reached the end.")
                break

            new = save_links(conn, links)
            total_new += new
            write_progress(page)
            more = has_next_page(driver)
            print(f"[page {page}] {len(links)} links found, {new} new")
            if not more:
                print(f"[done] Page {page} has no next-page button - last page.")
                break

            prev = links
            page += 1
            time.sleep(random.uniform(*DELAY_RANGE))
    except Blocked as e:
        print(f"\n[STOPPED] {e}")
        try:
            print(f"          URL: {driver.current_url}")
            print(f"          Debug files: {save_debug(driver, page)}.png / .html")
        except Exception:
            pass
        print(f"          Last finished page: {read_progress()}. "
              "Rerun to resume from the next page.")
        sys.exit(1)
    finally:
        count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        print(f"\nAdded {total_new} new links this run; "
              f"{count} links total in {DB_PATH} (table: links).")
        conn.close()
        driver.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Crawl PSA auction-price search links.")
    ap.add_argument("--page", type=int, help="start from this page number")
    ap.add_argument("--restart", action="store_true",
                    help="ignore progress.txt and start from page 1")
    args = ap.parse_args()

    if args.page:
        start = args.page
    elif args.restart:
        start = 1
    else:
        start = read_progress() + 1
    crawl(start)
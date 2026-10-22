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
- Uses a persistent Chrome profile (./chrome_profile). If PSA shows the
  sign-in page, the script logs in by itself: it types the email and password
  (from PSA_EMAIL / PSA_PASSWORD, or asks you) and asks you in the terminal for
  the one-time code PSA sends. If that gets stuck, finish it in the Chrome
  window. Later runs reuse the session, so you usually won't log in again.
- Behaves like a person: types with uneven pauses, moves the mouse to buttons
  before clicking, scrolls through each results page in uneven steps (hovering
  some links on the way), and takes a longer break every 15-30 pages.
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

- Proxy (e.g. Thordata): --proxy http://user:pass@host:port, or set the
  THORDATA_PROXY environment variable to that URL.

Requirements:  pip install selenium
(Selenium 4.6+ downloads the matching ChromeDriver automatically.)
"""

import argparse
import getpass
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from selenium import webdriver
from selenium.common.exceptions import (ElementClickInterceptedException,
                                        NoSuchElementException,
                                        StaleElementReferenceException,
                                        TimeoutException, WebDriverException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
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
BREAK_EVERY = (15, 30)       # take a longer break after this many pages...
BREAK_RANGE = (30.0, 90.0)   # ...lasting this long (seconds)
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

# Login form fields (same flow as scrape_auction_prices.py). Several candidate
# selectors per field, tried in order.
EMAIL_SELECTORS = [
    (By.CSS_SELECTOR, "input[type='email']"),
    (By.CSS_SELECTOR, "input[name='email']"),
    (By.CSS_SELECTOR, "input[name='username']"),
    (By.ID, "email"),
    (By.ID, "username"),
]
PASSWORD_SELECTORS = [
    (By.CSS_SELECTOR, "input[type='password']"),
    (By.CSS_SELECTOR, "input[name='password']"),
    (By.ID, "password"),
]
# Field for the one-time code PSA sends after the password.
CODE_SELECTORS = [
    (By.CSS_SELECTOR, "input[autocomplete='one-time-code']"),
    (By.CSS_SELECTOR, "input[name*='code' i]"),
    (By.CSS_SELECTOR, "input[id*='code' i]"),
    (By.CSS_SELECTOR, "input[name*='otp' i]"),
    (By.CSS_SELECTOR, "input[inputmode='numeric']"),
]
_LOWER = "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
SUBMIT_XPATH = ("//button[@type='submit' or " + " or ".join(
    f"contains({_LOWER}, '{w}')" for w in ("continue", "next", "sign in", "log in")) + "]")


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
def make_driver(proxy: str | None = None) -> webdriver.Chrome:
    """Start Chrome, optionally through a proxy.

    proxy: http://host:port, or http://user:pass@host:port for proxies that
    log in with a username/password (e.g. Thordata). Chrome ignores
    credentials in --proxy-server, so they are answered through WebDriver
    BiDi instead. SOCKS5 with a password is not supported by Chrome - use the
    HTTP endpoint.
    """
    opts = webdriver.ChromeOptions()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--window-size=1400,1000")
    user = password = None
    if proxy:
        parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        user = unquote(parts.username) if parts.username else None
        password = unquote(parts.password) if parts.password else ""
        opts.add_argument(f"--proxy-server={parts.scheme}://{parts.hostname}:{parts.port}")
        if user:
            opts.enable_bidi = True
    driver = webdriver.Chrome(options=opts)
    if user:
        driver.network.add_auth_handler(user, password)
    return driver


# ---------------- human-like behavior ----------------
def human_pause(low: float = 0.4, high: float = 1.2) -> None:
    time.sleep(random.uniform(low, high))


def human_type(field, text: str) -> None:
    """Type one character at a time with uneven gaps, like a person."""
    field.clear()
    for ch in text:
        field.send_keys(ch)
        time.sleep(random.uniform(0.05, 0.22))
    human_pause(0.3, 0.8)


def human_click(driver, element) -> None:
    """Move the mouse onto the element, hesitate a moment, then click."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    human_pause(0.2, 0.5)
    try:
        (ActionChains(driver)
         .move_to_element_with_offset(element, random.randint(-3, 3), random.randint(-2, 2))
         .pause(random.uniform(0.15, 0.45))
         .click()
         .perform())
    except WebDriverException:
        driver.execute_script("arguments[0].click();", element)


def links_in_view(driver) -> list:
    return driver.execute_script("""
        return [...document.querySelectorAll(arguments[0])].filter(a => {
            const r = a.getBoundingClientRect();
            return r.width > 0 && r.top > 0 && r.bottom < window.innerHeight;
        });""", LINK_SELECTOR)


def browse_page(driver) -> None:
    """Scroll down the page in uneven mouse-wheel steps, sometimes going back
    up a little and hovering a result, ending near the bottom where the page
    buttons are."""
    for _ in range(40):                              # safety stop
        at_bottom = driver.execute_script(
            "return window.innerHeight + window.pageYOffset"
            " >= document.body.scrollHeight - 5")
        if at_bottom:
            break
        step = random.randint(250, 650)
        if random.random() < 0.12:
            step = -random.randint(100, 300)         # glance back up
        try:
            ActionChains(driver).scroll_by_amount(0, step).perform()
        except WebDriverException:
            driver.execute_script("window.scrollBy(0, arguments[0]);", step)
        human_pause(0.4, 1.3)
        if random.random() < 0.3:
            try:
                in_view = links_in_view(driver)
                if in_view:
                    (ActionChains(driver)
                     .move_to_element(random.choice(in_view))
                     .pause(random.uniform(0.3, 1.0))
                     .perform())
            except WebDriverException:
                pass                                 # hovering is optional
    human_pause(0.5, 1.5)


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


def get_credentials() -> tuple[str, str]:
    email = os.environ.get("PSA_EMAIL") or input("PSA email: ").strip()
    password = os.environ.get("PSA_PASSWORD") or getpass.getpass("PSA password: ")
    return email, password


def find_visible_field(driver, selectors, timeout: float = 15):
    """Wait until any of the candidate selectors matches a visible element."""
    end = time.time() + timeout
    while True:
        for by, value in selectors:
            for el in driver.find_elements(by, value):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except StaleElementReferenceException:
                    continue
        if time.time() >= end:
            raise TimeoutException(f"no visible field for {selectors[0][1]!r}")
        time.sleep(0.5)


def login_finished(driver) -> bool:
    """True once no password/code field is showing and we're off sign-in."""
    for by, value in PASSWORD_SELECTORS[:1] + CODE_SELECTORS:
        for el in driver.find_elements(by, value):
            try:
                if el.is_displayed():
                    return False
            except StaleElementReferenceException:
                continue
    return not on_signin_page(driver)


def wait_for_code_field(driver, timeout: int = 15):
    """Return the visible code input if PSA asks for one, else None."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            return find_visible_field(driver, CODE_SELECTORS, timeout=0)
        except TimeoutException:
            pass
        if login_finished(driver):
            return None
        time.sleep(0.5)
    return None


def submit_step(driver, field) -> None:
    """Click the visible submit / continue button, falling back to Enter."""
    for btn in driver.find_elements(By.XPATH, SUBMIT_XPATH):
        try:
            if btn.is_displayed() and btn.is_enabled():
                human_click(driver, btn)
                return
        except (ElementClickInterceptedException, StaleElementReferenceException):
            continue
    field.send_keys(Keys.ENTER)


def dismiss_cookie_banner(driver) -> None:
    """Best-effort close of a cookie/consent banner that could block clicks."""
    for text in ("accept", "agree", "got it"):
        for btn in driver.find_elements(By.XPATH, f"//button[contains({_LOWER}, '{text}')]"):
            try:
                if btn.is_displayed():
                    human_click(driver, btn)
                    human_pause()
                    return
            except WebDriverException:
                continue


def auto_login(driver) -> None:
    """Email screen -> continue, password screen -> sign in, then the
    one-time code (typed into the terminal)."""
    human_pause(1.0, 2.0)
    dismiss_cookie_banner(driver)
    email, password = get_credentials()

    handles = set(driver.window_handles)
    field = find_visible_field(driver, EMAIL_SELECTORS)
    human_type(field, email)
    submit_step(driver, field)
    human_pause(1.5, 3.0)
    new = set(driver.window_handles) - handles       # password in a popup?
    if new:
        driver.switch_to.window(new.pop())

    field = find_visible_field(driver, PASSWORD_SELECTORS)
    human_type(field, password)
    submit_step(driver, field)

    code_field = wait_for_code_field(driver)
    if code_field is not None:
        code = (os.environ.get("PSA_AUTH_CODE")
                or input("Enter the one-time code PSA just sent you: ").strip())
        human_type(code_field, code)
        submit_step(driver, code_field)

    try:
        WebDriverWait(driver, 20).until(login_finished)
    except TimeoutException:
        pass                                         # manual fallback handles it
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[0])


def ensure_logged_in(driver, first_url: str) -> None:
    driver.get(first_url)
    time.sleep(2)
    if not on_signin_page(driver):
        return

    print("\n[login] PSA redirected to the sign-in page - logging in.")
    try:
        auto_login(driver)
    except TimeoutException as e:
        print(f"[login] Automatic login got stuck ({e}).")

    if on_signin_page(driver):
        print("        Finish logging in in the Chrome window "
              "(CAPTCHA, wrong password, a different code screen...).")
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
    print("[login] Logged in.")

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
            human_click(driver, btn)
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
def crawl(start_page: int, proxy: str | None = None) -> None:
    conn = open_db(DB_PATH)
    driver = make_driver(proxy)
    page, total_new, prev = start_page, 0, None
    next_break = start_page + random.randint(*BREAK_EVERY)
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

            browse_page(driver)
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
            if page >= next_break:
                pause = random.uniform(*BREAK_RANGE)
                print(f"  taking a {pause:.0f}s break")
                time.sleep(pause)
                next_break = page + random.randint(*BREAK_EVERY)
            else:
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
    ap.add_argument("--proxy", default=os.environ.get("THORDATA_PROXY"),
                    help="route Chrome through this proxy, e.g. "
                    "http://user:pass@host:port (default: $THORDATA_PROXY)")
    args = ap.parse_args()

    if args.page:
        start = args.page
    elif args.restart:
        start = 1
    else:
        start = read_progress() + 1
    crawl(start, args.proxy)
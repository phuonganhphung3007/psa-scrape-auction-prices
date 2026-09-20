import os
import sys
import time
import getpass
import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, StaleElementReferenceException, ElementClickInterceptedException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from bs4 import BeautifulSoup

SCRAPE_URL = "https://www.psacard.com/auctionprices"
EXAMPLE_URL = "https://www.psacard.com/auctionprices/baseball-cards/1967-topps/mets-rookies/values/187370"

# --- Login settings -------------------------------------------------------
# Credentials are read from environment variables so they are never stored in
# the script. Set them in your terminal before running:
#   Windows (cmd):   set PSA_EMAIL=you@example.com  &  set PSA_PASSWORD=yourpassword
#   PowerShell:      $env:PSA_EMAIL="you@example.com"; $env:PSA_PASSWORD="yourpassword"
#   macOS / Linux:   export PSA_EMAIL=you@example.com; export PSA_PASSWORD=yourpassword
# If they are not set, the script will prompt you when it starts.
LOGIN_URL = os.environ.get("PSA_LOGIN_URL", "https://app.collectors.com/signin?b=psa&r=https%253A%252F%252Fwww.psacard.com%252Fmyaccount%253FQTM_SID%253D12e2133a5ff942c1946dabe8e12acf2d%2526QTM_UID%253D6b45bfd1561e6d6d4b89f6fe2b98dd8a&QTM_SID=12e2133a5ff942c1946dabe8e12acf2d&QTM_UID=6b45bfd1561e6d6d4b89f6fe2b98dd8a")

# Several candidate selectors per field, tried in order. If login fails, open
# the page in Chrome, right-click the field -> Inspect, and put the real
# id/name at the front of the relevant list.
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
# Field for the one-time authorization / verification code sent after login.
CODE_SELECTORS = [
    (By.CSS_SELECTOR, "input[autocomplete='one-time-code']"),
    (By.CSS_SELECTOR, "input[name*='code' i]"),
    (By.CSS_SELECTOR, "input[id*='code' i]"),
    (By.CSS_SELECTOR, "input[name*='otp' i]"),
    (By.CSS_SELECTOR, "input[inputmode='numeric']"),
]
# Buttons are located by their visible text as a fallback to pressing Enter.
SUBMIT_XPATH = (
    "//button[@type='submit' or "
    "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue') or "
    "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next') or "
    "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in') or "
    "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'log in')]"
)


def get_credentials():
    email = os.environ.get("PSA_EMAIL") or input("PSA email: ").strip()
    password = os.environ.get("PSA_PASSWORD") or getpass.getpass("PSA password: ")
    return email, password


def find_visible_field(driver, selectors, timeout=15):
    """Wait until any of the candidate selectors matches a visible element."""
    end = time.time() + timeout
    while time.time() < end:
        for by, value in selectors:
            for el in driver.find_elements(by, value):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except StaleElementReferenceException:
                    continue
        time.sleep(0.5)
    raise TimeoutException("Could not find a visible field for selectors: {}".format(selectors))


def login_finished(driver):
    """True once no password/code field is showing and we're off the login URL."""
    if driver.find_elements(By.CSS_SELECTOR, "input[type='password']"):
        return False
    for by, value in CODE_SELECTORS:
        for el in driver.find_elements(by, value):
            try:
                if el.is_displayed():
                    return False
            except StaleElementReferenceException:
                continue
    return "login" not in driver.current_url.lower()


def wait_for_code_field(driver, timeout=15):
    """Return the visible code input if the site asks for one, else None."""
    end = time.time() + timeout
    while time.time() < end:
        for by, value in CODE_SELECTORS:
            for el in driver.find_elements(by, value):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except StaleElementReferenceException:
                    continue
        if login_finished(driver):
            return None
        time.sleep(0.5)
    return None


def submit_step(driver, field):
    """Click the visible submit / continue button, falling back to Enter."""
    for btn in driver.find_elements(By.XPATH, SUBMIT_XPATH):
        try:
            if btn.is_displayed() and btn.is_enabled():
                btn.click()
                return
        except (ElementClickInterceptedException, StaleElementReferenceException):
            continue
    field.send_keys(Keys.ENTER)


def dismiss_cookie_banner(driver):
    """Best-effort close of a cookie/consent banner that could block clicks."""
    for text in ("accept", "agree", "got it"):
        xpath = ("//button[contains(translate(normalize-space(.), "
                 "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{}')]").format(text)
        for btn in driver.find_elements(By.XPATH, xpath):
            try:
                if btn.is_displayed():
                    btn.click()
                    time.sleep(0.5)
                    return
            except Exception:
                continue


def login(driver, email, password):
    """Two-step login: (1) email screen -> continue, (2) password screen -> sign in."""
    print("Logging in to PSA...")
    driver.get(LOGIN_URL)
    time.sleep(2)
    dismiss_cookie_banner(driver)

    # Step 1: email
    original_handles = set(driver.window_handles)
    email_field = find_visible_field(driver, EMAIL_SELECTORS)
    email_field.clear()
    email_field.send_keys(email)
    submit_step(driver, email_field)
    time.sleep(2)

    # If the password prompt opened in a new window/tab, switch to it
    new_handles = set(driver.window_handles) - original_handles
    if new_handles:
        driver.switch_to.window(new_handles.pop())

    # Step 2: password
    password_field = find_visible_field(driver, PASSWORD_SELECTORS)
    password_field.clear()
    password_field.send_keys(password)
    submit_step(driver, password_field)

    # Step 3: authorization code (sent by PSA to your email/phone after the password)
    code_field = wait_for_code_field(driver)
    if code_field is not None:
        # Codes are one-time, so ask for it each run. You can also pre-set PSA_AUTH_CODE.
        code = os.environ.get("PSA_AUTH_CODE") or input("Enter the authorization code PSA just sent you: ").strip()
        code_field.clear()
        code_field.send_keys(code)
        submit_step(driver, code_field)

    # Wait until we've left the login flow
    try:
        WebDriverWait(driver, 20).until(login_finished)
    except TimeoutException:
        # Could be a CAPTCHA, a different code screen, or wrong credentials.
        input("Login not confirmed. Finish any steps by hand in the browser, then press Enter here to continue...")

    # Return to the main window if the login used a popup
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[0])

    print("Login complete.")
    time.sleep(2)


class PsaAuctionPricesScraper:
    def __init__(self, card_url, card_name, driver):
        self.card_url = card_url
        self.card_name = card_name
        self.driver = driver

    def scrape(self):
        print("collecting data for {}".format(self.card_name))

        # Navigate to webpage, pause until page finishes loading
        self.driver.get(self.card_url)
        page_loaded = self.pause_for_page_loading(self.driver)
        if not page_loaded:
            print("Error, page won't load for card {}".format(self.card_name))
            return

        # Get data from the current page/tables
        df = self.get_data_from_page(self.driver)

        if df is None or df.empty:
            print("No auction price records found for {}".format(self.card_name))
            return

        # Write to CSV file
        if not os.path.exists("data"):
            os.makedirs("data")

        df.to_csv(self.get_file_name(), index=False)
        print("Successfully saved data to {}".format(self.get_file_name()))

    def pause_for_page_loading(self, driver):
        # Wait for potential client-side elements or spinners to settle
        time.sleep(3)
        tries = 10
        while tries > 0:
            try:
                # If a spinner or loading mask is present, wait for it
                spinner = driver.find_elements(By.ID, "spinner-wrap")
                if spinner and spinner[0].is_displayed():
                    time.sleep(1)
                    tries -= 1
                    continue
                return True
            except (NoSuchElementException, TimeoutException, StaleElementReferenceException):
                return True
            time.sleep(1)
            tries -= 1
        return True

    def get_data_from_page(self, driver):
        res = driver.page_source
        soup = BeautifulSoup(res, "html5lib")

        # Look for tables or data grids containing auction records
        tables = soup.find_all("table")
        all_rows = []
        headers = []

        for table in tables:
            trs = table.find_all("tr")
            if not trs:
                continue

            # Extract headers if available
            ths = trs[0].find_all("th")
            if ths:
                headers = [th.get_text(strip=True).lower() for th in ths]

            # Check if this table looks like sales/auction history
            table_text = table.get_text().lower()
            if "price" in table_text or "date" in table_text or "auction" in table_text:
                for tr in trs[1:]:
                    tds = tr.find_all("td")
                    if tds:
                        row_data = [td.get_text(strip=True) for td in tds]
                        all_rows.append(row_data)

        if not all_rows:
            # Fallback: parse generic container divs if tables aren't structured standardly
            return pd.DataFrame()

        # If headers count doesn't match row lengths, generate generic columns
        if all_rows and len(headers) != len(all_rows[0]):
            headers = ["col_{}".format(i) for i in range(len(all_rows[0]))]

        df = pd.DataFrame(all_rows, columns=headers if headers else None)
        return df

    def get_file_name(self):
        f_name = "{}--{}".format(self.card_name.replace(" ", "-"), str(time.strftime("%Y-%m-%d-%H%M%S")))
        return "{}.csv".format(os.path.join("data", f_name))


def init_driver():
    driver = webdriver.Chrome()
    driver.wait = WebDriverWait(driver, 5)
    driver.maximize_window()
    return driver


if __name__ == '__main__':
    # Input validation matching codebase style
    urls_raw = []
    if len(sys.argv) > 1:
        urls_raw = [sys.argv[1]]
    else:
        if not os.path.exists("urls.txt"):
            raise ValueError("no input url passed and 'urls.txt' not found")
        with open("urls.txt") as f:
            urls_raw = [n for n in f.read().split("\n") if n]

    urls = {}
    for line in urls_raw:
        elems = [n.strip() for n in line.split("|")]
        if len(elems) == 2:
            urls[elems[0]] = elems[1]
        elif len(elems) == 1:
            urls[elems[0]] = elems[0]
        else:
            raise ValueError("Malformed txt line:\n{}\nLines should be pipe-separated elements, like this:\n"
                             "1967 Topps Mets Rookies | https://www.psacard.com/auctionprices/baseball-cards/1967-topps/mets-rookies/values/187370")

    if not os.path.exists("data"):
        os.makedirs("data")

    email, password = get_credentials()

    driver = None
    try:
        driver = init_driver()
        login(driver, email, password)
        for card_name, url in urls.items():
            scraper = PsaAuctionPricesScraper(url, card_name, driver)
            scraper.scrape()
    except Exception as e:
        print("An error occurred during execution: {}".format(e))
        raise
    finally:
        if driver:
            driver.quit()

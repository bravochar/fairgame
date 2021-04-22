import fileinput
import json
import os
import pickle
import platform
import random
import stdiomask
import time
import typing
import urllib
from contextlib import contextmanager
from datetime import datetime
from utils.debugger import debug
import secrets
from fake_useragent import UserAgent

import re

import psutil
import requests
from amazoncaptcha import AmazonCaptcha
from chromedriver_py import binary_path
from furl import furl
from lxml import html
from price_parser import parse_price, Price
from selenium import webdriver

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC, wait
from selenium.webdriver.support.expected_conditions import staleness_of
from selenium.webdriver.support.ui import WebDriverWait

from common.amazon_support import (
    AmazonItemCondition,
    condition_check,
    FGItem,
    get_shipping_costs,
    price_check,
    SellerDetail,
    solve_captcha,
    merchant_check,
)
from notifications.notifications import NotificationHandler
from stores.basestore import BaseStoreHandler
from utils.logger import log
from utils.selenium_utils import enable_headless, options

from functools import wraps

# PDP_URL = "https://smile.amazon.com/gp/product/"
# AMAZON_DOMAIN = "www.amazon.com.au"
# AMAZON_DOMAIN = "www.amazon.com.br"
# AMAZON_DOMAIN = "www.amazon.ca"
# NOT SUPPORTED AMAZON_DOMAIN = "www.amazon.cn"
# AMAZON_DOMAIN = "www.amazon.fr"
# AMAZON_DOMAIN = "www.amazon.de"
# NOT SUPPORTED AMAZON_DOMAIN = "www.amazon.in"
# AMAZON_DOMAIN = "www.amazon.it"
# AMAZON_DOMAIN = "www.amazon.co.jp"
# AMAZON_DOMAIN = "www.amazon.com.mx"
# AMAZON_DOMAIN = "www.amazon.nl"
# AMAZON_DOMAIN = "www.amazon.es"
# AMAZON_DOMAIN = "www.amazon.co.uk"
# AMAZON_DOMAIN = "www.amazon.com"
# AMAZON_DOMAIN = "www.amazon.se"

AMAZON_URLS = {
    "BASE_URL": "https://{domain}/",
    "ALT_OFFER_URL": "https://{domain}/gp/offer-listing/",
    "OFFER_URL": "https://{domain}/dp/",
    "CART_URL": "https://{domain}/gp/cart/view.html",
    "ATC_URL": "https://{domain}/gp/aws/cart/add.html",
    "PTC_GET": "https://{domain}/gp/cart/view.html/ref=lh_co_dup?ie=UTF8&proceedToCheckout.x=129",
    "PYO_POST": "https://{domain}/gp/buy/spc/handlers/static-submit-decoupled.html/ref=ox_spc_place_order?",
}

PDP_PATH = f"/dp/"
REALTIME_INVENTORY_PATH = f"gp/aod/ajax?asin="

CONFIG_FILE_PATH = "config/amazon_requests_config.json"
PROXY_FILE_PATH = "config/proxies.json"
STORE_NAME = "Amazon"
DEFAULT_MAX_TIMEOUT = 10

# Request
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, sdch, br",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36",
    "content-type": "application/x-www-form-urlencoded",
}


def free_shipping_check(seller):
    if seller.shipping_cost.amount > 0:
        return False
    else:
        return True


amazon_config = {}


class AmazonStoreHandler(BaseStoreHandler):
    http_client = False
    http_20_client = False
    http_session = True

    def __init__(
        self,
        notification_handler: NotificationHandler,
        headless=False,
        checkshipping=False,
        detailed=False,
        single_shot=False,
        no_screenshots=False,
        disable_presence=False,
        slow_mode=False,
        encryption_pass=None,
        log_stock_check=False,
        shipping_bypass=False,
        wait_on_captcha_fail=False,
        transfer_headers=False,
        use_atc_mode=False,
        proxy_checkout=False,
    ) -> None:
        super().__init__()

        self.shuffle = True
        self.is_test = False
        self.server_error_count = 0

        self.notification_handler = notification_handler
        self.check_shipping = checkshipping
        self.item_list: typing.List[FGItem] = []
        self.stock_checks = 0
        self.start_time = int(time.time())
        self.amazon_domain = "smile.amazon.com"
        self.webdriver_child_pids = []
        self.take_screenshots = not no_screenshots
        self.shipping_bypass = shipping_bypass
        self.single_shot = single_shot
        self.detailed = detailed
        self.disable_presence = disable_presence
        self.log_stock_check = log_stock_check
        self.wait_on_captcha_fail = wait_on_captcha_fail
        self.amazon_cookies = {}
        self.transfer_headers = transfer_headers
        self.buy_it_now = not use_atc_mode
        self.proxy_checkout = proxy_checkout

        if not self.buy_it_now:
            log.warning("Using legacy add-to-cart mode instead of turbo_initiate")

        self.ua = UserAgent()

        from cli.cli import global_config

        global amazon_config
        amazon_config = global_config.get_amazon_config(encryption_pass)
        self.profile_path = global_config.get_browser_profile_path()

        # Load up our configuration
        self.parse_config()

        # Set up the Chrome options based on user flags
        if headless:
            enable_headless()

        prefs = get_prefs(no_image=False)
        set_options(profile_path=self.profile_path, prefs=prefs, slow_mode=slow_mode)
        modify_browser_profile()

        # Initialize the Session we'll use for stock checking
        self._session_stock_check = requests.Session()
        self._session_stock_check.headers.update(HEADERS)

        # self.conn = http.client.HTTPSConnection(self.amazon_domain)
        # self.conn20 = HTTP20Connection(self.amazon_domain)

        # Initialize proxies for stock check session:
        # Assuming same username/password for all proxies
        # username = [INSERT USERNAME HERE]
        # password = [INSERT PASSWORD HERE]
        #
        # self.proxies = [
        #     {
        #       'http': f"http://{username}:{password}@X.X.X.X:XXXX",
        #       'https': f"http://{username}:{password}@X.X.X.X:XXXX",
        #     },
        #     {
        #       'http': f"http://{username}:{password}@X.X.X.X:XXXX",
        #       'https': f"http://{username}:{password}@X.X.X.X:XXXX",
        #     },
        # ]
        self.proxies = []
        self.proxy_sessions = []
        self.used_proxy_sessions = []
        if os.path.exists(PROXY_FILE_PATH):
            proxy_json = json.load(open(PROXY_FILE_PATH))
            self.proxies = proxy_json.get('proxies', [])

            # initialize sessions for each proxy
            for proxy in self.proxies:
                s = requests.Session()
                s.headers.update(HEADERS)
                s.headers.update({"user-agent": self.ua.random})
                s.proxies.update(proxy)

                # add a strikes count to weed out bad proxies/configurations
                s.strikes = 0
                self.proxy_sessions.append(s)

            random.shuffle(self.proxy_sessions)

            # initialize first 2 proxies with Amazon cookies
            for s in self.proxy_sessions[:2]:
                s.get(f'https://{self.amazon_domain}')
                time.sleep(1)

            log.info(f"Created {len(self.proxy_sessions)} proxy sessions")

        # Spawn the web browser
        self.driver = create_driver(options)
        self.webdriver_child_pids = get_webdriver_pids(self.driver)

        # Initialize the checkout session
        self.session_checkout = requests.Session()
        self.session_checkout.headers.update(HEADERS)

    @property
    def session_stock_check(self):
        rval = self._session_stock_check

        if self.proxies:
            if not self.proxy_sessions:
                self.proxy_sessions = self.used_proxy_sessions
                self.used_proxy_sessions = []
                log.debug('Shuffling proxies...')
                random.shuffle(self.proxy_sessions)

            if self.proxy_sessions:
                rval = self.proxy_sessions.pop(0)
                self.used_proxy_sessions.append(rval)
                log.debug(f"Using proxy {rval.proxies}")


        return rval

    def __del__(self):
        message = f"Shutting down {STORE_NAME} Store Handler."
        log.info(message)
        self.notification_handler.send_notification(message)
        self.delete_driver()

    def delete_driver(self):
        try:
            if platform.system() == "Windows" and self.driver:
                log.debug("Cleaning up after web driver...")
                # brute force kill child Chrome pids with fire
                for pid in self.webdriver_child_pids:
                    try:
                        log.debug(f"Killing {pid}...")
                        process = psutil.Process(pid)
                        process.kill()
                    except psutil.NoSuchProcess:
                        log.debug(f"{pid} not found. Continuing...")
                        pass
            elif self.driver:
                self.driver.quit()

        except Exception as e:
            log.debug(e)
            log.debug(
                "Failed to clean up after web driver.  Please manually close browser."
            )
            return False
        return True

    def handle_startup(self):
        time.sleep(3)
        if self.is_logged_in():
            log.info("Already logged in")
        else:
            log.info("Lets log in.")

            is_smile = "smile" in AMAZON_URLS["BASE_URL"]
            xpath = (
                '//*[@id="ge-hello"]/div/span/a'
                if is_smile
                else '//*[@id="nav-link-accountList"]/div/span'
            )

            try:
                self.driver.find_element_by_xpath(xpath).click()
            except NoSuchElementException:
                log.error("Log in button does not exist")
            log.info("Wait for Sign In page")
            time.sleep(3)

    def is_logged_in(self):
        try:
            text = self.driver.find_element_by_id("nav-link-accountList").text
            return not any(sign_in in text for sign_in in amazon_config["SIGN_IN_TEXT"])
        except NoSuchElementException:

            return False

    def login(self):
        log.info(f"Logging in to {self.amazon_domain}...")

        email_field: WebElement
        remember_me: WebElement
        password_field: WebElement

        # # Look for a sign in link
        # try:
        #     skip_link: WebElement = WebDriverWait(self.driver, 10).until(
        #         EC.presence_of_element_located(
        #             (By.XPATH, "//a[contains(@href, '/ap/signin/')]")
        #         )
        #     )
        #     skip_link.click()
        # except TimeoutException as e:
        #     log.debug(
        #         "Timed out waiting for signin link.  Unable to find matching "
        #         "xpath for '//a[@data-nav-role='signin']'"
        #     )
        #     log.exception(e)
        #     exit(1)

        log.info("Inputting email...")
        try:

            email_field: WebElement = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="ap_email"]'))
            )
            with self.wait_for_page_content_change():
                email_field.clear()
                email_field.send_keys(amazon_config["username"] + Keys.RETURN)
            if self.driver.find_elements_by_xpath('//*[@id="auth-error-message-box"]'):
                log.error("Login failed, delete your credentials file")
                time.sleep(240)
                exit(1)
        except wait.TimeoutException as e:
            log.error("Timed out waiting for email login box.")
            log.exception(e)
            exit(1)

        log.debug("Checking 'rememberMe' checkbox...")
        try:
            remember_me = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//*[@name="rememberMe"]'))
            )
            remember_me.click()
        except NoSuchElementException:
            log.error("Remember me checkbox did not exist")

        log.info("Inputting Password")
        captcha_entry: WebElement = None
        try:
            password_field = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="ap_password"]'))
            )
            password_field.clear()
            password_field.send_keys(amazon_config["password"])
            # check for captcha
            try:
                captcha_entry = self.driver.find_element_by_xpath(
                    '//*[@id="auth-captcha-guess"]'
                )
            except NoSuchElementException:
                with self.wait_for_page_content_change(timeout=10):
                    password_field.send_keys(Keys.RETURN)

        except NoSuchElementException:
            log.error("Unable to find password input box.  Unable to log in.")
        except wait.TimeoutException:
            log.error("Timeout expired waiting for password input box.")

        if captcha_entry:
            try:
                log.debug("Stuck on a captcha... Lets try to solve it.")
                captcha = AmazonCaptcha.fromdriver(self.driver)
                solution = captcha.solve()
                log.debug(f"The solution is: {solution}")
                if solution == "Not solved":
                    log.debug(
                        f"Failed to solve {captcha.image_link}, lets reload and get a new captcha."
                    )
                    self.send_notification(
                        "Unsolved Captcha", "unsolved_captcha", self.take_screenshots
                    )
                    self.driver.refresh()
                else:
                    self.send_notification(
                        "Solving catpcha", "captcha", self.take_screenshots
                    )
                    with self.wait_for_page_content_change(timeout=10):
                        captcha_entry.clear()
                        captcha_entry.send_keys(solution + Keys.RETURN)

            except Exception as e:
                log.debug(e)
                log.debug("Error trying to solve captcha. Refresh and retry.")
                self.driver.refresh()
                time.sleep(5)

        # Deal with 2FA
        if self.driver.title in amazon_config["TWOFA_TITLES"]:
            log.warning("Encountered 2FA page")
            otp_field = self.wait_get_amazon_element("OTP_FIELD")
            if otp_field:
                log.info("Setting OTP using CLI...")
                otp_remember_me = self.wait_get_clickable_amazon_element(
                    "OTP_REMEMBER_ME"
                )
                if otp_remember_me:
                    otp_remember_me.click()
                else:
                    log.error("OTP Remember me checkbox did not exist")

                otp = stdiomask.getpass(
                    prompt="enter in your two-step verification code: ", mask="*"
                )
                otp_field.send_keys(otp + Keys.RETURN)
                time.sleep(2)

            else:
                log.error("OTP entry box did not exist, please fill in OTP manually...")
                while self.driver.title in amazon_config["TWOFA_TITLES"]:
                    # Wait for the user to enter 2FA
                    time.sleep(2)

        # Deal with Account Fix Up prompt
        if "accountfixup" in self.driver.current_url:
            # Click the skip link
            try:
                skip_link: WebElement = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//a[contains(@id, 'skip-link')]")
                    )
                )
                skip_link.click()
            except TimeoutException as e:
                log.debug(
                    "Timed out waiting for the skip link.  Unable to find matching "
                    "xpath for '//a[contains(@id, 'skip-link')]'"
                )
                log.exception(e)

        log.info(f'Logged in as {amazon_config["username"]}')

    def transfer_selenium_cookies(
        self, s: requests.Session, all_cookies=False
    ):
        # get cookies, might use these for checkout later, with no cookies on
        # cookie_names = ["session-id", "ubid-main", "x-main", "at-main", "sess-at-main"]
        # for c in self.driver.get_cookies():
        #     if c["name"] in cookie_names:
        #         self.amazon_cookies[c["name"]] = c["value"]

        # update session with cookies from Selenium
        cookie_names = ["session-id", "ubid-main", "x-main", "at-main", "sess-at-main"]
        self.selenium_cookies_copy = self.driver.get_cookies().copy()
        self.checkout_cookies = {}
        log.debug(self.driver.get_cookies())
        for c in self.driver.get_cookies():
            if all_cookies or c["name"] in cookie_names:
                self.checkout_cookies[c["name"]] = c["value"]
                log.debug(f'Set Cookie {c["name"]} as value {c["value"]}')
            else:
                log.debug(f'Ignoring Cookie {c["name"]} as value {c["value"]}')

        s.cookies.update(self.checkout_cookies)

    # Test code, use at your own RISK
    def run_offer_id(self, offerid, delay=5, all_cookies=False, test=False):
        self.is_test = test

        # Load up the homepage
        with self.wait_for_page_change():
            self.driver.get(f"https://{self.amazon_domain}")

        self.handle_startup()
        # Get a valid amazon session for our requests
        if not self.is_logged_in():
            self.login()

        self.transfer_selenium_cookies(
            self.session_checkout, all_cookies=all_cookies
        )

        message = f"Starting to hunt items at {STORE_NAME}"
        log.info(message)
        self.notification_handler.send_notification(message)
        self.save_screenshot("logged-in")
        print("\n\n")
        recurring_message = "The hunt continues! "
        idx = 0
        spinner = ["-", "\\", "|", "/"]
        check_count = 1
        print(
            "Do not ask in Discord what to do if your account gets banned running this code!"
        )
        print(f"Checking OfferID: {offerid}\n")

        seller = SellerDetail(
            offering_id=offerid,
            merchant_id="",
            price=parse_price("0"),
            shipping_cost=parse_price("0"),
        )
        while True:
            print(
                recurring_message,
                f"Check Count: {check_count} ({100 * self.server_error_count / check_count:02.2f}% error),",
                spinner[idx],
                end="    \r",
            )
            check_count += 1
            idx += 1
            if idx == len(spinner):
                idx = 0
            delay_time = time.time() + delay

            # try to checkout
            if self.attempt_turbo_checkout(seller):
                break

            # sleep remainder of delay_time
            time_left = delay_time - time.time()
            if time_left > 0:
                time.sleep(time_left)

        log.info("Shutting down")

    def run(self, delay=5, test=False, all_cookies=False, turbo_iters=1):
        self.is_test = test

        # Load up the homepage
        with self.wait_for_page_change():
            self.driver.get(f"https://{self.amazon_domain}")

        self.handle_startup()
        # Get a valid amazon session for our requests
        if not self.is_logged_in():
            self.login()

        time.sleep(5)

        sel_headers = self.driver.execute_script(
            "var req = new XMLHttpRequest();req.open('GET', document.location, false);req.send(null);return req.getAllResponseHeaders()"
        )

        # type(headers) == str

        headers = sel_headers.splitlines()
        header_dict = {}
        for h in headers:
            header_dict[h.split(": ")[0]] = h.split(": ")[1]

        # Transfer cookies from selenium session.
        # Do not transfer cookies to stock check if using proxies
        if not self.proxies:
            self.transfer_selenium_cookies(
                self._session_stock_check, all_cookies=all_cookies
            )
            self._session_stock_check.headers.update(header_dict)

        self.transfer_selenium_cookies(
            self.session_checkout, all_cookies=all_cookies
        )
        self.session_checkout.headers.update(header_dict)

        # Verify the configuration file
        if not self.verify():
            # try one more time
            log.debug("Failed to verify... trying more more time")
            self.verify()

        # To keep the user busy https://github.com/jakesgordon/javascript-tetris
        # ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
        # uri = pathlib.Path(f"{ROOT_DIR}/../tetris/index.html").as_uri()
        # log.debug(f"Tetris URL: {uri}")
        # self.driver.get(uri)

        message = f"Starting to hunt items at {STORE_NAME}"
        log.info(message)
        self.notification_handler.send_notification(message)
        self.save_screenshot("logged-in")
        print("\n\n")
        recurring_message = "The hunt continues! "
        idx = 0
        spinner = ["-", "\\", "|", "/"]
        check_count = 1
        while self.item_list:
            for item in self.item_list:
                end_time_cart = 0
                start_time = time.time()
                delay_time = start_time + delay
                qualified_seller = self.find_qualified_seller(item)
                end_time_check = time.time()
                if qualified_seller:
                    if self.buy_it_now:
                        if self.attempt_turbo_checkout(
                            qualified_seller, delay, turbo_iters
                        ):
                            if self.single_shot:
                                self.item_list.clear()
                            else:
                                self.item_list.remove(item)
                        end_time_cart = time.time()

                    elif self.atc(qualified_seller=qualified_seller, item=item):
                        r = self.ptc()
                        # with open("ptc-source.html", "w", encoding="utf-8") as f:
                        #     f.write(r)
                        if r:
                            log.debug(r)
                            if test:
                                print(
                                    "Proceeded to Checkout - Will not Place Order as this is a Test",
                                    end="\r",
                                )
                                # in Test mode, clear the list
                                self.item_list.clear()

                            elif self.pyo(page=r):
                                if self.single_shot:
                                    self.item_list.clear()
                                else:
                                    self.item_list.remove(item)
                        end_time_cart = time.time()

                log.debug(
                    f"ASIN check for {item.id} took {end_time_check - start_time:.3f} seconds."
                )
                if end_time_cart:
                    log.debug(
                        f"Checkout for {item.id} took {end_time_cart - start_time:.3f} seconds."
                    )

                print(
                    recurring_message,
                    f"Check Count: {check_count} ({100 * self.server_error_count / check_count:02.2f}% error),",
                    spinner[idx],
                    end="    \r",
                )
                check_count += 1
                idx += 1
                if idx == len(spinner):
                    idx = 0

                # initialize amazon session cookie, if missing from _next_ proxy
                if self.proxy_sessions \
                        and len(self.proxy_sessions) > 1 \
                        and not self.proxy_sessions[1].cookies:
                    self.proxy_sessions[1].get(f'https://{self.amazon_domain}')

                    # need to slow down proxy initialization by at least one second
                    # trying to do it quicker results in a lot of 503s,
                    # even with random user-agent strings
                    time.sleep(1)

                # sleep remainder of delay_time
                time_left = delay_time - time.time()
                if time_left > 0:
                    time.sleep(time_left)

            if self.shuffle:
                random.shuffle(self.item_list)

    @contextmanager
    def wait_for_page_change(self, timeout=30):
        """Utility to help manage selenium waiting for a page to load after an action, like a click"""
        kill_time = get_timeout(timeout)
        old_page = self.driver.find_element_by_tag_name("html")
        yield
        WebDriverWait(self.driver, timeout).until(staleness_of(old_page))
        # wait for page title to be non-blank
        while self.driver.title == "" and time.time() < kill_time:
            time.sleep(0.05)

    @contextmanager
    def wait_for_page_content_change(self, timeout=5):
        """Utility to help manage selenium waiting for a page to load after an action, like a click"""
        old_page = self.driver.find_element_by_tag_name("html")
        yield
        try:
            WebDriverWait(self.driver, timeout).until(EC.staleness_of(old_page))
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, "//title"))
            )
        except TimeoutException:
            log.debug("Timed out reloading page, trying to continue anyway")
            pass
        except Exception as e:
            log.debug(f"Trying to recover from error: {e}")
            pass
        return None

    def do_button_click(
        self,
        button,
        clicking_text="Clicking button",
        clicked_text="Button clicked",
        fail_text="Could not click button",
    ):
        try:
            with self.wait_for_page_content_change():
                log.debug(clicking_text)
                button.click()
                log.debug(clicked_text)
            return True
        except WebDriverException as e:
            log.debug(fail_text)
            log.debug(e)
            return False

    def find_qualified_seller(self, item) -> SellerDetail or None:
        for seller in self.get_item_sellers(item, amazon_config["FREE_SHIPPING"]):
            if not self.check_shipping and not free_shipping_check(seller):
                log.debug(f"Failed shipping hurdle.")
                continue
            if not condition_check(item, seller):
                log.debug(f"Failed item condition hurdle: {seller.condition}")
                continue
            if not price_check(item, seller):
                log.debug(f"Failed price condition hurdle: {seller.price}")
                continue
            if not merchant_check(item, seller):
                log.debug(f"Failed merchant id hurdle: {seller.merchant_id}")
                continue

            # Returns first seller that passes condition checks
            log.debug(f"Found seller: {seller}")
            return seller

    def parse_config(self):
        log.debug(f"Processing config file from {CONFIG_FILE_PATH}")
        # Parse the configuration file to get our hunt list
        try:
            with open(CONFIG_FILE_PATH) as json_file:
                config = json.load(json_file)
                self.amazon_domain = config.get("amazon_domain", "smile.amazon.com")

                for key in AMAZON_URLS.keys():
                    AMAZON_URLS[key] = AMAZON_URLS[key].format(
                        domain=self.amazon_domain
                    )

                json_items = config.get("items")
                self.parse_items(json_items)

        except FileNotFoundError:
            log.error(
                f"Configuration file not found at {CONFIG_FILE_PATH}.  Please see {CONFIG_FILE_PATH}_template."
            )
            exit(1)
        log.debug(f"Found {len(self.item_list)} items to track at {STORE_NAME}.")

    def parse_items(self, json_items):
        for json_item in json_items:
            if (
                "max-price" in json_item
                and "asins" in json_item
                and "min-price" in json_item
            ):
                max_price = json_item["max-price"]
                min_price = json_item["min-price"]
                if type(max_price) is str:
                    max_price = parse_price(max_price)
                else:
                    max_price = Price(max_price, currency=None, amount_text=None)
                if type(min_price) is str:
                    min_price = parse_price(min_price)
                else:
                    min_price = Price(min_price, currency=None, amount_text=None)

                if "condition" in json_item:
                    condition = parse_condition(json_item["condition"])
                else:
                    condition = AmazonItemCondition.New

                if "merchant_id" in json_item:
                    merchant_id = json_item["merchant_id"]
                else:
                    merchant_id = "any"

                # Create new instances of an item for each asin specified
                asins_collection = json_item["asins"]
                if isinstance(asins_collection, str):
                    log.warning(
                        f"\"asins\" node needs be an list/array and included in braces (e.g., [])  Attempting to recover {json_item['asins']}"
                    )
                    # did the user forget to put us in an array?
                    asins_collection = asins_collection.split(",")
                for asin in asins_collection:
                    self.item_list.append(
                        FGItem(
                            asin,
                            min_price,
                            max_price,
                            condition=condition,
                            merchant_id=merchant_id,
                        )
                    )
            else:
                log.error(
                    f"Item isn't fully qualified.  Please include asin, min-price and max-price. {json_item}"
                )

    def verify(self):
        log.debug("Verifying item list...")
        items_to_purge = []
        verified = 0
        item_cache_file = os.path.join(
            os.path.dirname(os.path.abspath("__file__")),
            "stores",
            "store_data",
            "item_cache.p",
        )

        if os.path.exists(item_cache_file) and os.path.getsize(item_cache_file) > 0:
            item_cache = pickle.load(open(item_cache_file, "rb"))
        else:
            item_cache = {}

        for idx, item in enumerate(self.item_list):
            # Check the cache first to save the scraping...
            item.pdp_url = f"https://{self.amazon_domain}{PDP_PATH}{item.id}"
            if item.id in item_cache.keys():
                cached_item = item_cache[item.id]
                log.debug(f"Verifying ASIN {cached_item.id} via cache  ...")
                # Update attributes that may have been changed in the config file
                cached_item.condition = item.condition
                cached_item.min_price = item.min_price
                cached_item.max_price = item.max_price
                cached_item.merchant_id = item.merchant_id
                cached_item.pdp_url = item.pdp_url
                self.item_list[idx] = cached_item
                log.debug(
                    f"Verified ASIN {cached_item.id} as '{cached_item.short_name}'"
                )
                verified += 1
                continue

            # Verify that the ASIN hits and that we have a valid inventory URL
            log.debug(f"Verifying at {item.pdp_url} ...")

            # use checkout session and good cookie to verify item list
            session = self.session_checkout

            data, status = self.get_html(item.pdp_url, s=session)
            if not data and not status:
                log.debug("Response empty, skipping item")
                continue
            if status == 503:
                # Check for CAPTCHA
                tree = html.fromstring(data)
                captcha_form_element = tree.xpath(
                    "//form[contains(@action,'validateCaptcha')]"
                )
                if captcha_form_element:
                    # Solving captcha and resetting data
                    data, status = solve_captcha(
                        session, captcha_form_element[0], item.pdp_url
                    )

            if status == 200:
                item.furl = furl(
                    f"https://{self.amazon_domain}/{REALTIME_INVENTORY_PATH}{item.id}"
                )
                tree = html.fromstring(data)
                captcha_form_element = tree.xpath(
                    "//form[contains(@action,'validateCaptcha')]"
                )
                if captcha_form_element:
                    data, status = solve_captcha(
                        session, captcha_form_element[0], item.pdp_url
                    )
                    if status != 200:
                        log.debug(f"ASIN {item.id} failed, skipping...")
                        continue
                    tree = html.fromstring(data)

                title = tree.xpath('//*[@id="productTitle"]')
                if len(title) > 0:
                    item.name = title[0].text.strip()
                    item.short_name = (
                        item.name[:40].strip() + "..."
                        if len(item.name) > 40
                        else item.name
                    )
                    log.debug(f"Verified ASIN {item.id} as '{item.short_name}'")
                    item_cache[item.id] = item
                    verified += 1
                else:
                    # TODO: Evaluate if this happens with a 200 code
                    doggo = tree.xpath("//img[@alt='Dogs of Amazon']")
                    if doggo:
                        # Bad ASIN or URL... dump it
                        log.error(
                            f"Bad ASIN {item.id} for the domain or related failure.  Removing from hunt."
                        )
                        items_to_purge.append(item)
                    else:
                        log.debug(
                            f"Unable to verify ASIN {item.id}.  Continuing without verification."
                        )
            else:
                log.error(
                    f"Unable to locate details for {item.id} at {item.pdp_url}.  Removing from hunt."
                )
                items_to_purge.append(item)

        # Purge any items we didn't find while verifying
        for item in items_to_purge:
            self.item_list.remove(item)

        log.debug(
            f"Verified {verified} out of {len(self.item_list)} items on {STORE_NAME}"
        )
        pickle.dump(item_cache, open(item_cache_file, "wb"))

        return True

    def get_item_sellers(self, item, free_shipping_strings):
        """Parse out information to from the aod-offer nodes populate ItemDetail instances for each item """
        payload = self.get_real_time_data(item)
        sellers = []
        if payload is None or len(payload) == 0:
            log.error("Empty Response.  Skipping...")
            return
        # This is where the parsing magic goes
        log.debug(f"payload is {len(payload)} bytes")

        tree = html.fromstring(payload)

        if item.status_code == 503:
            self.server_error_count += 1
            with open("503-page.html", "w", encoding="utf-8") as f:
                f.write(payload)
            # Check for CAPTCHA
            captcha_form_element = tree.xpath(
                "//form[contains(@action,'validateCaptcha')]"
            )
            if captcha_form_element:
                log.info("captcha found")
                url = f"https://{self.amazon_domain}/{REALTIME_INVENTORY_PATH}{item.id}"
                # Solving captcha and resetting data
                data, status = solve_captcha(
                    session, captcha_form_element[0], url
                )
                if status != 503:
                    payload = data
                    tree = html.fromstring(payload)
                else:
                    log.info(f"No valid page for ASIN {item.id}")
                    return
            else:
                log.debug("Got Server error")

            # 503 responses won't ever have sellers
            return

        # look for product ASIN
        page_asin = tree.xpath("//input[@id='ftSelectAsin']")
        if page_asin:
            try:
                found_asin = page_asin[0].value.strip()
            except (AttributeError, IndexError):
                found_asin = "[NO ASINS FOUND ON PAGE]"
        else:
            find_asin = re.search(r"asin\s?(?:=|\.)?\s?\"?([A-Z0-9]+)\"?", payload)
            if find_asin:
                found_asin = find_asin.group(1)
            else:
                found_asin = "[NO ASIN FOUND ON PAGE]"

        if found_asin != item.id:
            log.debug(
                f"Aborting Check, ASINs do not match. Found {found_asin}; Searching for {item.id}."
            )
            return

        # Get all the offers (pinned and others)
        offers = tree.xpath(
            "//div[@id='aod-sticky-pinned-offer'] | //div[@id='aod-offer']"
        )

        # I don't really get the OR part of this, how could the first part fail, but the second part not fail?
        # if not pinned_offer or not tree.xpath(
        #     "//div[@id='aod-sticky-pinned-offer']//input[@name='submit.addToCart'] | //div[@id='aod-offer']//input[@name='submit.addToCart']"
        # ):
        #
        if not offers:
            log.debug(f"No offers for {item.id} = {item.short_name}")
        else:
            for idx, offer in enumerate(offers):
                try:
                    merchant_id = offer.xpath(
                        ".//input[@id='ftSelectMerchant' or @id='ddmSelectMerchant']"
                    )[0].value
                except IndexError:
                    try:
                        merchant_script = offer.xpath(".//script")[0].text.strip()
                        find_merchant_id = re.search(
                            r"merchantId = \"(\w+?)\";", merchant_script
                        )
                        if find_merchant_id:
                            merchant_id = find_merchant_id.group(1)
                        else:
                            log.debug("No Merchant ID found")
                            merchant_id = ""
                    except IndexError:
                        log.debug("No Merchant ID found")
                        merchant_id = ""
                try:
                    price_text = offer.xpath(".//span[@class='a-price-whole']")[
                        0
                    ].text.strip()
                except IndexError:
                    log.debug("No price found for this offer, skipping")
                    continue
                price = parse_price(price_text)
                shipping_cost = get_shipping_costs(offer, free_shipping_strings)
                condition_heading = offer.xpath(".//div[@id='aod-offer-heading']/h5")
                if condition_heading:
                    condition = AmazonItemCondition.from_str(
                        condition_heading[0].text.strip()
                    )
                else:
                    condition = AmazonItemCondition.Unknown
                offer_ids = offer.xpath(f".//input[@name='offeringID.1']")
                offer_id = None
                if len(offer_ids) > 0:
                    offer_id = offer_ids[0].value
                else:
                    log.error("No offer ID found for this offer, skipping")
                    continue
                try:
                    atc_form = [
                        offer.xpath(".//form[@method='post']")[0].action,
                        offer.xpath(".//form//input"),
                    ]
                except IndexError:
                    log.error("ATC form items did not exist, skipping")
                    continue

                yield SellerDetail(
                    merchant_id,
                    price,
                    shipping_cost,
                    condition,
                    offer_id,
                    atc_form=atc_form,
                    item_id=item.id
                )

    def do_turbo_checkout(self, session, seller):
        pid, anti_csrf = self.turbo_initiate(session, qualified_seller=seller)
        if pid and anti_csrf:
            if self.turbo_checkout(session, pid=pid, anti_csrf=anti_csrf):
                return True

        return False

    def attempt_turbo_checkout(self, seller, delay=5, iters=1):
        # try to checkout
        if self.do_turbo_checkout(self.session_checkout, seller):
            return True

        if self.proxies and self.proxy_checkout:
            log.debug("Trying with all proxies...")
            proxies = self.proxies
            random.shuffle(proxies)

            if self.used_proxy_sessions:
                last_proxy = self.used_proxy_sessions[-1].proxies
                log.debug(f"Using proxy that pinged stock first: {last_proxy}")
                proxies = [d for d in self.proxies if  d != last_proxy]
                proxies.insert(0, last_proxy)

            for proxy in proxies:
                # make a copy of the checkout session
                session = requests.Session()
                session.proxies.update(proxy)
                session.headers.update(self.session_checkout.headers)
                session.cookies.update(self.session_checkout.cookies)

                if self.do_turbo_checkout(session, seller):
                    return True

        return False

    def turbo_initiate(self, session, qualified_seller, asin=""):
        pid, anti_csrf = None, None
        log_message = 'turbo_ini_unsuccessful'

        url = f"https://{self.amazon_domain}/checkout/turbo-initiate"
        query_dict = {
            "ref_": "dp_start-bbf_1_glance_buyNow_2-1",
            "pipelineType": "turbo",
            "weblab": "RCX_CHECKOUT_TURBO_DESKTOP_PRIME_87783",
            "temporaryAddToCart": "1",
        }
        payload_inputs = {
            "offerListing.1": qualified_seller.offering_id,
            "quantity.1": "1",
        }

        # set ASIN if supplied
        if asin:
            payload_inputs["asin.1"] = asin

        # clear all except default headers
        session.headers.clear()
        session.headers.update(HEADERS)

        # try up to 3 times, as long as we get 200
        for _ in range(3):
            start_time = time.perf_counter()
            r = session.post(url=url, params=query_dict, data=payload_inputs)
            if r.status_code == 200:
                if r.text:
                    find_pid = re.search(r"pid=(.*?)&amp;", r.text)
                    if find_pid:
                        pid = find_pid.group(1)

                    find_anti_csrf = re.search(r"'anti-csrftoken-a2z' value='(.*?)'", r.text)
                    if find_anti_csrf:
                        anti_csrf = find_anti_csrf.group(1)
                        break
            else:
                log.debug(f"turbo_initiate returned {r.status_code}")
                break

            log.debug(log_message)
            save_request_response(log_message, r)

        return pid, anti_csrf

    def turbo_checkout(self, session, pid, anti_csrf):
        rval = False
        log.debug("trying to checkout")
        url = f"https://{self.amazon_domain}/checkout/spc/place-order"
        query_dict = {
            "ref_": "chk_spc_placeOrder",
            "clientId": "retailwebsite",
            "pipelineType": "turbo",
            "pid": f"{pid}",
        }

        header_update = {"anti-csrftoken-a2z": anti_csrf}
        session.headers.update(header_update)

        if not self.is_test:
            r = session.post(url=url, params=query_dict)
            if r.status_code == 200 or r.status_code == 500:
                log.info("Checkout maybe successful, check order page!")
                # TODO: Implement GET request to confirm checkout
                rval = True

            log_request(r)
            save_html_response(
                filename="turbo_checkout_response", status=r.status_code, body=r.text
            )
        else:
            log.warning("This was a test, not completing checkout; returning True")
            rval = True

        return rval

    def atc(self, qualified_seller, item):
        post_action = qualified_seller.atc_form[0]
        payload_inputs = {}
        for idx, payload_input in enumerate(qualified_seller.atc_form[1]):
            payload_inputs[payload_input.name] = payload_input.value

        payload_inputs.update({"submit.addToCart": "Submit"})
        self.session_checkout.headers.update(
            {"referer": f"https://{self.amazon_domain}/gp/aod/ajax?asin={item.id}"}
        )
        # payload_inputs = {
        #     "offerListing.1": qualified_seller.offering_id,
        #     "quantity.1": "1",
        # }

        url = f"https://{self.amazon_domain}{post_action}"
        session_id = self.driver.get_cookie("session-id")
        payload_inputs["session-id"] = session_id["value"]

        r = self.session_checkout.post(url=url, data=payload_inputs)

        # print(r.status_code)
        # with open("atc-response.html", "w") as f:
        #     f.write(r.text)
        if r.status_code == 200:
            log.info("ATC successful")
            return True
        else:
            log.info("ATC unsuccessful")
            return False

    def ptc(self):
        url = f"https://{self.amazon_domain}/gp/cart/view.html/ref=lh_co_dup?ie=UTF8&proceedToCheckout.x=129"
        try:
            r = self.session_checkout.get(url=url, timeout=5)
        except requests.exceptions.Timeout:
            log.debug("Request Timed Out")
            return None

        if r.status_code == 200 and r.text:
            log.info("PTC successful")
            return r.text
        else:
            log.info("PTC unsuccessful")
            return None

    def pyo(self, page):
        pyo_html = html.fromstring(page)
        pyo_params = {
            "submitFromSPC": "",
            "fasttrackExpiration": "",
            "countdownThreshold": "",
            "showSimplifiedCountdown": "",
            "countdownId": "",
            "gift-message-text": "",
            "concealment-item-message": "Ship+in+Amazon+packaging+selected",
            "dupOrderCheckArgs": "",
            "order0": "",
            "shippingofferingid0.0": "",
            "guaranteetype0.0": "",
            "issss0.0": "",
            "shipsplitpriority0.0": "",
            "isShipWhenCompleteValid0.0": "",
            "isShipWheneverValid0.0": "",
            "shippingofferingid0.1": "",
            "guaranteetype0.1": "",
            "issss0.1": "",
            "shipsplitpriority0.1": "",
            "isShipWhenCompleteValid0.1": "",
            "isShipWheneverValid0.1": "",
            "shippingofferingid0.2": "",
            "guaranteetype0.2": "",
            "issss0.2": "",
            "shipsplitpriority0.2": "",
            "isShipWhenCompleteValid0.2": "",
            "isShipWheneverValid0.2": "",
            "previousshippingofferingid0": "",
            "previousguaranteetype0": "",
            "previousissss0": "",
            "previousshippriority0": "",
            "lineitemids0": "",
            "currentshippingspeed": "",
            "previousShippingSpeed0": "",
            "currentshipsplitpreference": "",
            "shippriority.0.shipWhenever": "",
            "groupcount": "",
            "shiptrialprefix": "",
            "csrfToken": "",
            "fromAnywhere": "",
            "redirectOnSuccess": "",
            "purchaseTotal": "",
            "purchaseTotalCurrency": "",
            "purchaseID": "",
            "purchaseCustomerId": "",
            "useCtb": "",
            "scopeId": "",
            "isQuantityInvariant": "",
            "promiseTime-0": "",
            "promiseAsin-0": "",
            "selectedPaymentPaystationId": "",
            "hasWorkingJavascript": "1",
            "placeYourOrder1": "1",
            "isfirsttimecustomer": "0",
            "isTFXEligible": "",
            "isFxEnabled": "",
            "isFXTncShown": "",
        }

        pyo_keys = amazon_config["PYO_KEYS"]

        quantity_key = pyo_html.xpath(
            ".//label[@class='a-native-dropdown quantity-dropdown-select js-select']"
        )
        try:
            pyo_params[quantity_key[0].get("for")] = "1"
        except IndexError as e:
            log.debug(e)
            log.debug("quantity key error, skipping")

        for key in pyo_keys:
            try:
                value = pyo_html.xpath(f".//input[@name='{key}']")[0].value
                pyo_params[key] = value
            except IndexError as e:
                log.debug(e)
                log.debug(f"Error with {key}, skipping")

        url = f"https://{self.amazon_domain}/gp/buy/spc/handlers/static-submit-decoupled.html/ref=ox_spc_place_order?"
        r = self.session_checkout.post(url=url, data=pyo_params)

        if r.status_code == 200:
            return True
        else:
            return False

    def get_real_time_data(self, item: FGItem):
        log.debug(f"Calling {STORE_NAME} for {item.short_name} using {item.furl.url}")

        session = self.session_stock_check

        params = {"anticache": str(secrets.token_urlsafe(32))}
        item.furl.args.update(params)
        session.headers['referer'] = item.pdp_url
        data, status = self.get_html(
            item.furl.url, s=session
        )
        session.headers.pop('referer', None)

        if item.status_code != status:
            # Track when we flip-flop between status codes.  200 -> 204 may be intelligent caching at Amazon.
            # We need to know if it ever goes back to a 200
            log.warning(
                f"{item.short_name} started responding with Status Code {status} instead of {item.status_code}"
            )
            item.status_code = status

        return data

    def handle_captcha(self, check_presence=True):
        # wait for captcha to load
        log.debug("Waiting for captcha to load.")
        time.sleep(3)
        try:
            if not check_presence or self.driver.find_element_by_xpath(
                '//form[contains(@action,"validateCaptcha")]'
            ):
                try:
                    log.debug("Stuck on a captcha... Lets try to solve it.")
                    captcha = AmazonCaptcha.fromdriver(self.driver)
                    solution = captcha.solve()
                    log.debug(f"The solution is: {solution}")
                    if solution == "Not solved":
                        log.debug(
                            f"Failed to solve {captcha.image_link}, lets reload and get a new captcha."
                        )
                        self.driver.refresh()
                        time.sleep(3)
                    else:
                        self.send_notification(
                            "Solving catpcha", "captcha", self.take_screenshots
                        )
                        with self.wait_for_page_content_change():
                            self.driver.find_element_by_xpath(
                                '//*[@id="captchacharacters"]'
                            ).send_keys(solution + Keys.RETURN)
                except Exception as e:
                    log.debug(e)
                    log.debug("Error trying to solve captcha. Refresh and retry.")
                    with self.wait_for_page_content_change():
                        self.driver.refresh()
        except NoSuchElementException:
            log.debug("captcha page does not contain captcha element")
            log.debug("refreshing")
            with self.wait_for_page_content_change():
                self.driver.refresh()

    def get_html(self, url, s: requests.Session, randomize_ua=False):
        """Unified mechanism to get content to make changing connection clients easier"""
        f = furl(url)
        if randomize_ua and not self.proxies:
            user_agent = {"user-agent": self.ua.random}
            s.headers.update(user_agent)
        if not f.scheme:
            f.set(scheme="https")
        try:
            response = s.get(f.url, timeout=5)
        except requests.exceptions.RequestException as e:
            log.debug(e)
            log.debug("timeout on get_html")
            return None, None
        except Exception as e:
            log.debug(e)
            return None, None
        return response.text, response.status_code

    # returns negative number if cart element does not exist, returns number if cart exists
    def get_cart_count(self):
        # check if cart number is on the page, if cart items = 0
        try:
            element = self.get_amazon_element(key="CART")
        except NoSuchElementException:
            return -1
        if element:
            try:
                return int(element.text)
            except Exception as e:
                log.debug("Error converting cart number to integer")
                log.debug(e)
                return -1

    def wait_get_amazon_element(self, key):
        xpath = join_xpaths(amazon_config["XPATHS"][key])
        if wait_for_element_by_xpath(self.driver, xpath):
            return self.get_amazon_element(key=key)
        else:
            return None

    def wait_get_clickable_amazon_element(self, key):
        xpath = join_xpaths(amazon_config["XPATHS"][key])
        if wait_for_element_by_xpath(self.driver, xpath):
            try:
                return WebDriverWait(self.driver, timeout=5).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
            except TimeoutException:
                return None
        else:
            return None

    def wait_get_amazon_elements(self, key):
        xpath = join_xpaths(amazon_config["XPATHS"][key])
        if wait_for_element_by_xpath(self.driver, xpath):
            return self.get_amazon_elements(key=key)
        else:
            return None

    def get_amazon_element(self, key):
        return self.driver.find_element_by_xpath(
            join_xpaths(amazon_config["XPATHS"][key])
        )

    def get_amazon_elements(self, key):
        return self.driver.find_elements_by_xpath(
            join_xpaths(amazon_config["XPATHS"][key])
        )

    def get_page(self, url):
        try:
            with self.wait_for_page_content_change():
                self.driver.get(url=url)
            return True
        except TimeoutException:
            log.debug("Failed to load page within timeout period")
            return False
        except Exception as e:
            log.debug(f"Other error encountered while loading page: {e}")

    def save_screenshot(self, page):
        file_name = get_timestamp_filename("screenshots/screenshot-" + page, ".png")
        try:
            self.driver.save_screenshot(file_name)
            return file_name
        except TimeoutException:
            log.debug("Timed out taking screenshot, trying to continue anyway")
            pass
        except Exception as e:
            log.debug(f"Trying to recover from error: {e}")
            pass
        return None

    def save_page_source(self, page):
        """Saves DOM at the current state when called.  This includes state changes from DOM manipulation via JS"""
        file_name = get_timestamp_filename("html_saves/" + page + "_source", "html")

        page_source = self.driver.page_source
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(page_source)

    def check_captcha_selenium(self, f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            with self.wait_for_page_content_change():
                f(*args, **kwargs)
            try:
                captcha_element = self.get_amazon_element(key="CAPTCHA_VALIDATE")
            except NoSuchElementException:
                captcha_element = None
            if captcha_element:
                captcha_link = self.driver.page_source.split('<img src="')[1].split(
                    '">'
                )[
                    0
                ]  # extract captcha link
                captcha = AmazonCaptcha.fromlink(
                    captcha_link
                )  # pass it to `fromlink` class method
                if captcha == "Not solved":
                    log.info(f"Failed to solve {captcha.image_link}")
                    if self.wait_on_captcha_fail:
                        log.info("Will wait up to 60 seconds for user to solve captcha")
                        self.send(
                            "User Intervention Required - captcha check",
                            "captcha",
                            self.take_screenshots,
                        )
                        with self.wait_for_page_content_change():
                            timeout = get_timeout(timeout=60)
                            current_page = self.driver.title
                            while (
                                time.time() < timeout
                                and self.driver.title == current_page
                            ):
                                time.sleep(0.5)
                            # check above is not true, then we must have passed captcha, return back to nav handler
                            # Otherwise refresh page to try again - either way, returning to nav page handler
                            if (
                                time.time() > timeout
                                and self.driver.title == current_page
                            ):
                                log.info(
                                    "User intervention did not occur in time - will attempt to refresh page and try again"
                                )
                                return False
                            elif self.driver.title != current_page:
                                return True
                    else:
                        return False
                else:  # solved!
                    # take screenshot if user asked for detailed
                    if self.detailed:
                        self.send_notification(
                            "Solving catpcha", "captcha", self.take_screenshots
                        )
                    try:
                        captcha_field = self.get_amazon_element(
                            key="CAPTCHA_TEXT_FIELD"
                        )
                    except NoSuchElementException:
                        log.debug("Could not locate captcha entry field")
                        captcha_field = None
                    if captcha_field:
                        try:
                            check_password = self.get_amazon_element(
                                key="PASSWORD_TEXT_FIELD"
                            )
                        except NoSuchElementException:
                            check_password = None
                        if check_password:
                            check_password.clear()
                            check_password.send_keys(amazon_config["password"])
                        with self.wait_for_page_content_change():
                            captcha_field.send_keys(captcha + Keys.RETURN)
                        return True
                    else:
                        return False
            return True

        return wrapper


def parse_condition(condition: str) -> AmazonItemCondition:
    return AmazonItemCondition[condition]


def min_total_price(seller: SellerDetail):
    return seller.selling_price


def new_first(seller: SellerDetail):
    return seller.condition


def create_driver(options):
    try:
        return webdriver.Chrome(executable_path=binary_path, options=options)
    except Exception as e:
        log.error(e)
        log.error(
            "If you have a JSON warning above, try deleting your .profile-amz folder"
        )
        log.error(
            "If that's not it, you probably have a previous Chrome window open. You should close it."
        )
        exit(1)


def modify_browser_profile():
    # Delete crashed, so restore pop-up doesn't happen
    path_to_prefs = os.path.join(
        os.path.dirname(os.path.abspath("__file__")),
        ".profile-amz",
        "Default",
        "Preferences",
    )
    try:
        with fileinput.FileInput(path_to_prefs, inplace=True) as file:
            for line in file:
                print(line.replace("Crashed", "none"), end="")
    except FileNotFoundError:
        pass


def set_options(profile_path, prefs, slow_mode):
    options.add_experimental_option("prefs", prefs)
    options.add_argument(f"user-data-dir={profile_path}")
    if not slow_mode:
        options.set_capability("pageLoadStrategy", "none")


def get_prefs(no_image):
    prefs = {
        "profile.password_manager_enabled": False,
        "credentials_enable_service": False,
    }
    if no_image:
        prefs["profile.managed_default_content_settings.images"] = 2
    else:
        prefs["profile.managed_default_content_settings.images"] = 0
    return prefs


def create_webdriver_wait(driver, wait_time=10):
    return WebDriverWait(driver, wait_time)


def get_webdriver_pids(driver):
    pid = driver.service.process.pid
    driver_process = psutil.Process(pid)
    children = driver_process.children(recursive=True)
    webdriver_child_pids = []
    for child in children:
        webdriver_child_pids.append(child.pid)
    return webdriver_child_pids


def get_timeout(timeout=DEFAULT_MAX_TIMEOUT):
    return time.time() + timeout


def wait_for_element_by_xpath(d, xpath, timeout=10):
    try:
        WebDriverWait(d, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
    except TimeoutException:
        log.debug(f"failed to find {xpath}")
        return False

    return True


def join_xpaths(xpath_list, separator=" | "):
    return separator.join(xpath_list)


def get_timestamp_filename(name, extension):
    """Utility method to create a filename with a timestamp appended to the root and before
    the provided file extension"""
    now = datetime.now()
    date = now.strftime("%m-%d-%Y_%H_%M_%S")
    if extension.startswith("."):
        return name + "_" + date + extension
    else:
        return name + "_" + date + "." + extension


def save_html_response(filename, status, body):
    """Saves response body"""
    file_name = get_timestamp_filename(
        "html_saves/" + filename + "_" + str(status) + "_requests_source", "html"
    )

    page_source = body
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(page_source)


def save_request_response(filename, response):
    """Saves request response

    Saves all data associated with the response, including the initial
    request"""
    file_name = get_timestamp_filename(
        "html_saves/" + filename + "_" + str(response.status_code) + "_full_request_response", "txt"
    )

    with open(file_name, "w", encoding="utf-8") as f:
        if response.request:
            f.write(f"REQUEST: {response.request.method} {response.request.url}\n\n")
            for k, v in response.request.headers.items():
                f.write(f"    {k}: {v}\n")
            f.write(f"\n\n{response.request.body}")

        f.write(f"\n\nRESPONSE: {response.status_code}\n\n")
        for k, v in response.headers.items():
            f.write(f"    {k}: {v}\n")
        f.write(f"\n\n{response.text}")


def log_request(r):
    if hasattr(r, "request") and r.request is not None:
        log.debug(repr(r.request))
        log.debug(f"  headers: {json.dumps(dict(r.request.headers), indent=4)}")
        try:
            log.debug(
                f"  body: {json.dumps(urllib.parse.parse_qs(r.request.body), indent=4)}"
            )
        except ValueError:
            log.debug(f"  body: {r.request.body}")
        log.debug(f"{r.request.method} {r.url}: {r.status_code}")
    else:
        log.debug(f"{r.url}: {r.status_code}")
    log.debug(f"  headers: {json.dumps(dict(r.headers), indent=4)}")
    log.debug(f"  cookies: {json.dumps(r.cookies.get_dict(), indent=4)}")


def log_response(r):
    if hasattr(r, "request") and r.request is not None:
        log.debug(f"REQUEST: {r.request.method} {r.url}: {r.status_code}")
        log.debug(f"  headers: {json.dumps(dict(r.request.headers), indent=4)}")
        try:
            log.debug(
                f"  body: {json.dumps(urllib.parse.parse_qs(r.request.body), indent=4)}"
            )
        except ValueError:
            log.debug(f"  body: {r.request.body}")
    log.debug(f"RESPONSE: {r.status_code}")
    log.debug(f"  headers: {json.dumps(dict(r.headers), indent=4)}")
    log.debug(f"  cookies: {json.dumps(r.cookies.get_dict(), indent=4)}")

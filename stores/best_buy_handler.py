import json
import time
import typing
import asyncio
from furl import furl
from price_parser import parse_price, Price

from notifications.notifications import NotificationHandler
from stores.basestore import BaseStoreHandler
from utils.logger import log

# from stores.best_buy_monitoring import BestBuyMonitoringHandler, BestBuyMonitor
# from stores.best_buy_checkout import BestBuyCheckoutHandler

from utils.selenium_utils import (
    enable_headless,
    options,
    get_cookies,
    save_screenshot,
    selenium_initialization,
    create_driver,
)

CONFIG_FILE_PATH = "config/best_buy_config.json"
STORE_NAME = "Best Buy"


class BestBuyItem:
    def __init__(self, sku):
        self.sku = sku

    def validate_item(self, driver):
        driver.get(f"https://bestbuy.com/site/{self.sku}.p")

        time.sleep(1)
        return "Page Not Found" not in driver.title


class BestBuyStoreHandler(BaseStoreHandler):
    """Best Buy Purchase Handler

    This class will handle monitoring Best Buy stock and automotically
    purchasing items as they come in stock

    Additionally, it should help gain an advantage for Best Buy's queue
    system by supporting multiple Chrome profiles to allow for multiple
    spots in their pseudorandom queue. These sessions will be done
    without authentication in the hope that we can login in after the
    item has been carted and complete the checkout relatively quickly.

    Being logged-in may prevent holding multiple tickets to the queue
    "lottery" so the first try will have logged-out sessions when
    multiple are desired.

    TODO:
        - Verify login credentials
        - Start a new Chrome profile and add all SKUs to saved items
            - Verify SKUs map to actual items as part of this
        - Refresh Cart page and click "Add to Cart" as it becomes available
        - Pause above refreshing when something is _actually_ added to cart
        - checkout/login/complete transaction
        - Implement checkout class to checkout by logging in
        - Pause for SMS verification and bring window to front, maximized:
            - automate by taking confirmation code on CLI and/or waiting for
              user to complete and either: confirm by command line OR have selenium
              detect staleness of element to resume checkout handling
        - Support multiple profiles/sessions to maximize queue chances
        - Add tests to use in-stock SKUs with a don't-confirm-checkout flag
    """

    http_client = False
    http_20_client = False
    http_session = True

    def __init__(
        self,
        notification_handler: NotificationHandler,
        delay: float,
        headless=False,
        single_shot=False,
        encryption_pass=None,
        use_proxies=False,
        check_shipping=False,
    ) -> None:
        super().__init__()
        self.is_test = False

        self.notification_handler = notification_handler
        self.stock_checks = 0
        self.start_time = int(time.time())
        self.store_domain = "bestbuy.com"
        self.single_shot = single_shot
        self.delay = delay
        self.item_list: typing.List[BestBuyItem] = []

        from cli.cli import global_config

        global best_buy_config
        best_buy_config = global_config.get_best_buy_config(encryption_pass)

        # TODO: make unique path for Best Buy profile(s)
        self.profile_path = global_config.get_browser_profile_path()
        selenium_initialization(options=options, profile_path=self.profile_path)
        self.driver = create_driver(options)

        # Load up our configuration
        self.parse_config()
        log.debug("BestBuyStoreHandler initialization complete.")

    def __del__(self):
        message = f"Shutting down {STORE_NAME} Store Handler."
        log.info(message)
        self.notification_handler.send_notification(message)

        self.driver.quit()

    def parse_config(self):
        log.debug(f"Processing config file from {CONFIG_FILE_PATH}")
        # Parse the configuration file to get our hunt list
        try:
            with open(CONFIG_FILE_PATH) as json_file:
                config = json.load(json_file)
                json_items = config.get("items")

                self.parse_items(json_items)

        except FileNotFoundError:
            log.error(
                f"Configuration file not found at {CONFIG_FILE_PATH}.  Please see {CONFIG_FILE_PATH}_template."
            )
            exit(1)

        log.debug(f"Found {len(self.item_list)} items to track at {STORE_NAME}.")

    def parse_items(self, json_items):
        # TODO: encapsulate SKU in class
        #   The class should support adding SKU to Saved Items and
        #   validate the validity of the SKU on init()
        for json_item in json_items:
            b = BestBuyItem(json_item["SKU"])

            if b.validate_item(self.driver):
                self.item_list.append(b)

            else:
                log.error(f"Failed to validate {b.sku}")

    def login(self):
        pass

    def run(self):
        pass

    def check_stock(self, item):
        pass

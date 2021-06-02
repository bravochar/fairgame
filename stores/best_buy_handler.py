import json
import time
import typing
import asyncio
from furl import furl
from price_parser import parse_price, Price

from notifications.notifications import NotificationHandler
from stores.basestore import BaseStoreHandler
from utils.logger import log

from stores.best_buy_monitoring import BestBuyMonitoringHandler, BestBuyMonitor
from stores.best_buy_checkout import BestBuyCheckoutHandler

CONFIG_FILE_PATH = "config/best_buy_aio_config.json"
STORE_NAME = "Best Buy"


class BestBuyStoreHandler(BaseStoreHandler):
    '''Best Buy Purchase Handler

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
        - parse configuration  to get SKUs
        - Verify SKUs map to actual items
        - Implement monitoring using fulfillment URL
        - Implement checkout class to checkout by logging in
        - Pause for SMS verification and bring window to front, maximized:
            - automate by taking confirmation code on CLI and/or waiting for
              user to complete and either: confirm by command line OR have selenium
              detect staleness of element to resume checkout handling
        - Support multiple profiles/sessions to maximize queue chances
        - Add tests to use in-stock SKUs with a don't-confirm-checkout flag
    '''
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
        self.item_list: typing.List[FGItem] = []
        self.stock_checks = 0
        self.start_time = int(time.time())
        self.store_domain = "bestbuy.com"
        self.single_shot = single_shot
        self.delay = delay

        from cli.cli import global_config

        global best_buy_config
        best_buy_config = global_config.get_best_buy_config(encryption_pass)
        self.profile_path = global_config.get_browser_profile_path()

        # Load up our configuration
        self.parse_config()
        log.debug("BestBuyStoreHandler initialization complete.")

    def __del__(self):
        message = f"Shutting down {STORE_NAME} Store Handler."
        log.info(message)
        self.notification_handler.send_notification(message)

    def parse_config(self):
        pass

    def validate_sku(self):
        pass

    def login(self):
        pass

    def run(self):
        pass

    def check_stock(self, item):
        pass


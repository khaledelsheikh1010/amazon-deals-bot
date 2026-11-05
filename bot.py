import os
import re
import json
import time
import random
import hashlib
import logging
import html as html_lib
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
MIN_DISCOUNT = float(os.getenv('MIN_DISCOUNT', '60'))
PRICE_DROP_ALERT_PERCENT = float(os.getenv('PRICE_DROP_ALERT_PERCENT', '30'))
USED_VS_NEW_ALERT_PERCENT = float(os.getenv('USED_VS_NEW_ALERT_PERCENT', '35'))
ALERT_THRESHOLDS = sorted({float(x.strip()) for x in os.getenv('ALERT_THRESHOLDS', '30,50,60,70,80').split(',') if x.strip()})
PRICE_HISTORY_MAX_ENTRIES = int(os.getenv('PRICE_HISTORY_MAX_ENTRIES', '365'))
CHECK_EVERY_MINUTES = int(os.getenv('CHECK_EVERY_MINUTES', '15'))
NORMAL_SCAN_PAGES = int(os.getenv('NORMAL_SCAN_PAGES', '10'))
DEEP_SCAN_PAGES = int(os.getenv('DEEP_SCAN_PAGES', '40'))
DEEP_SCAN_INTERVAL_HOURS = float(os.getenv('DEEP_SCAN_INTERVAL_HOURS', '24'))
DEAD_PAGE_LIMIT = int(os.getenv('DEAD_PAGE_LIMIT', '3'))
SIMILAR_PAGE_LIMIT = int(os.getenv('SIMILAR_PAGE_LIMIT', '3'))
SIMILARITY_THRESHOLD = float(os.getenv('SIMILARITY_THRESHOLD', '0.80'))
REQUEST_DELAY_MIN = float(os.getenv('REQUEST_DELAY_MIN', '2.5'))
REQUEST_DELAY_MAX = float(os.getenv('REQUEST_DELAY_MAX', '4.5'))
SEND_START_MESSAGE = os.getenv('SEND_START_MESSAGE', '1').strip() == '1'
SEND_CYCLE_SUMMARY = os.getenv('SEND_CYCLE_SUMMARY', '0').strip() == '1'

raw_urls = os.getenv('AMAZON_URLS', '').replace(',', '\n').splitlines()
URLS = [
    line.strip().strip('"').strip("'") for line in raw_urls
    if line.strip() and not line.strip().startswith('#') and 'http' in line
]

DEALS_STATE_FILE = 'seen_deals.json'
PRICE_HISTORY_FILE = 'price_history.json'
SCAN_STATE_FILE = 'scan_state.json'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/152.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Cache-Control': 'no-cache',
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

session = requests.Session()
session.headers.update(HEADERS)
retry = Retry(
    total=3,
    connect=3,
    read=3,
    status=3,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(['GET', 'POST']),
    raise_on_status=False,
)
adapter = HTTPAdapter(max_retries=retry)
session.mount('https://', adapter)
session.mount('http://', adapter)


def load_json(

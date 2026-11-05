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


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_ts():
    return int(time.time())


def should_do_deep_scan(scan_state):
    last = scan_state.get('last_deep_scan_ts')
    if not last:
        return True
    return ((now_ts() - int(last)) / 3600.0) >= DEEP_SCAN_INTERVAL_HOURS


def telegram_send(text, parse_mode='HTML'):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID')
    endpoint = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = {
        'chat_id': CHAT_ID,
        'text': text,
        'disable_web_page_preview': False,
    }
    if parse_mode:
        payload['parse_mode'] = parse_mode
    r = session.post(endpoint, json=payload, timeout=30)
    r.raise_for_status()


def normalize_price(text):
    if not text:
        return None
    cleaned = text.replace(',', '').replace('EGP', '').replace('E£', '').replace('جنيه', '').replace('£', '').strip()
    m = re.search(r'(\d+(?:\.\d+)?)', cleaned)
    return float(m.group(1)) if m else None


def get_page_url(url, page):
    parts = list(urlparse(url))
    q = parse_qs(parts[4], keep_blank_values=True)
    q['page'] = [str(page)]
    parts[4] = urlencode(q, doseq=True)
    return urlunparse(parts)


def clean_url(href):
    if not href:
        return None
    full = urljoin('https://www.amazon.eg', href)
    parsed = urlparse(full)
    m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', parsed.path)
    if m:
        return f'https://www.amazon.eg/dp/{m.group(1)}'
    return full.split('?')[0]


def asin_to_url(asin):
    if asin and re.fullmatch(r'[A-Z0-9]{10}', asin):
        return f'https://www.amazon.eg/dp/{asin}'
    return None


def extract_asin(card, product_url=None):
    asin = (card.get('data-asin') or '').strip()
    if re.fullmatch(r'[A-Z0-9]{10}', asin):
        return asin
    if product_url:
        m = re.search(r'/dp/([A-Z0-9]{10})', product_url)
        if m:
            return m.group(1)
    return hashlib.sha1((product_url or '').encode()).hexdigest()[:16]


def calculate_discount(old_price, current_price):
    if old_price and current_price and old_price > current_price > 0:
        return round((old_price - current_price) / old_price * 100, 1)
    return None


def jaccard_similarity(a, b):
    if not a or not b:
        return 0.0
    union = a | b
    return (len(a & b) / len(union)) if union else 0.0


def first_text(node, selectors):
    for selector in selectors:
        el = node.select_one(selector)
        if el:
            text = el.get_text(' ', strip=True)
            if text:
                return text
    return None


def first_href(node, selectors):
    for selector in selectors:
        el = node.select_one(selector)
        if el and el.get('href'):
            return el.get('href')
    return None


def extract_used_or_secondary_price(card, visible_text, title):
    grocery_keywords = [
        'oil', 'zayt', 'زيت', 'grocery', 'بقالة', 'food', 'طعام', 'chocolate', 'شوكولاتة',
        'tea', 'شاي', 'coffee', 'قهوة', 'sugar', 'سكر', 'rice', 'أرز', 'milk', 'حليب', 'laundry',
        'detergent', 'مسحوق', 'gel', 'جل', 'oxi', 'تيد', 'persil', 'hygiene', 'wipes', 'مناديل',
        'yogurt', 'زبادي', 'okra', 'بامية', 'frozen', 'مجمد', 'suisse', 'snack', 'سناك'
    ]
    title_lower = title.lower()
    if any(kw in title_lower for kw in grocery_keywords):
        return None

    patterns = [
        r'More Buying Choices.*?(?:EGP|E£|جنيه)\s*([\d,]+(?:\.\d+)?)',
        r'([\d,]+(?:\.\d+)?)\s*(?:EGP|E£|جنيه).*?(?:used|مستعمل)',
        r'خيارات شراء أخرى.*?(?:EGP|E£|جنيه)?\s*([\d,]+(?:\.\d+)?)',
    ]
    for pat in patterns:
        m = re.search(pat, visible_text, flags=re.I)
        if m:
            val = normalize_price(m.group(1))
            if val and val > 0:
                return val
    return None


def extract_promotions(visible_text):
    """استخراج الكوبونات وعروض التوفير (قطعة وقطعة / خصم إضافي) من كارت المنتج"""
    coupon_pct = None
    coupon_amt = None
    promo_badges = []

    # 1. البحث عن كوبون بنسبة مئوية
    pct_patterns = [
        r'save\s*(\d{1,2})\s*%\s*with\s*coupon',
        r'coupon.*?(\d{1,2})\s*%',
        r'وفر\s*(\d{1,2})\s*%\s*مع\s*الكوبون',
        r'كوبون.*?(\d{1,2})\s*%',
    ]
    for pat in pct_patterns:
        m = re.search(pat, visible_text, flags=re.I)
        if m:
            coupon_pct = float(m.group(1))
            promo_badges.append(f"كوبون خصم {coupon_pct:.0f}%")
            break

    # 2. البحث عن كوبون بمبلغ مالي ثابت (إذا لم يوجد كوبون نسبة)
    if coupon_pct is None:
        amt_patterns = [
            r'save\s*(?:EGP|E£|جنيه)?\s*([\d,]+(?:\.\d+)?)\s*(?:EGP|E£|جنيه)?\s*with\s*coupon',
            r'وفر\s*([\d,]+(?:\.\d+)?)\s*(?:EGP|E£|جنيه)?\s*مع\s*الكوبون',
        ]
        for pat in amt_patterns:
            m = re.search(pat, visible_text, flags=re.I)
            if m:
                val = normalize_price(m.group(1))
                if val and val > 0:
                    coupon_amt = val
                    promo_badges.append(f"كوبون خصم {coupon_amt:.0f} EGP")
                    break

    # 3. البحث عن عروض الكميات والتوفير (Buy X Get Y / BOGO / Extra Discount / خصم عند الدفع)
    promo_patterns = [
        r'(buy\s*\d+\s*get\s*\d+\s*(?:free|off|\d+\s*%\s*off)?)',
        r'(buy\s*and\s*get\s*extra\s*discount\s*\d{1,2}\s*%)',
        r'(get\s*extra\s*discount\s*\d{1,2}\s*%)',
        r'(b\d+g\d+\s*\d*\s*%?\s*(?:off|free)?)',
        r'(save\s*\d{1,2}\s*%\s*(?:on|when\s*you\s*buy)\s*\d+[^.,;\n]{0,25})',
        r'(extra\s*\d{1,2}\s*%\s*off[^.,;\n]{0,25})',
        r'(اشترِ?\s*\d+\s*واحصل\s*على[^.,;\n]{0,30})',
        r'(خصم\s*إضافي\s*\d{1,2}\s*%[^.,;\n]{0,25})',
        r'(وفر\s*\d{1,2}\s*%\s*عند\s*شراء[^.,;\n]{0,25})',
    ]
    for pat in promo_patterns:
        m = re.search(pat, visible_text, flags=re.I)
        if m:
            badge_text = m.group(1).strip()
            if badge_text and badge_text.lower() not in [b.lower() for b in promo_badges]:
                promo_badges.append(badge_text)

    return {
        'coupon_pct': coupon_pct,
        'coupon_amt': coupon_amt,
        'promo_text': ' | '.join(promo_badges) if promo_badges else None,
        'has_multi_buy_or_extra': len(promo_badges) > (1 if (coupon_pct or coupon_amt) else 0),
    }


def page_diagnostics(html):
    soup = BeautifulSoup(html, 'html.parser')
    page_text = soup.get_text(' ', strip=True).lower()
    captcha = (
        'captcha' in page_text
        or 'enter the characters you see below' in page_text
        or "sorry, we just need to make sure you're not a robot" in page_text
    )
    product_links = set()
    for a in soup.select('a[href*="/dp/"], a[href*="/gp/product/"]'):
        href = a.get('href')
        if not href:
            continue
        url = clean_url(href)
        if url and '/dp/' in url:
            product_links.add(url)
    search_cards = soup.select('[data-component-type="s-search-result"]')
    asin_values = []
    for node in soup.select('[data-asin]'):
        asin = (node.get('data-asin') or '').strip()
        if re.fullmatch(r'[A-Z0-9]{10}', asin):
            asin_values.append(asin)
    no_results_phrases = [
        'no results for',
        'your search did not match any products',
        'try checking your spelling',
        'use fewer keywords',
    ]
    explicit_no_results = any(p in page_text for p in no_results_phrases)
    return {
        'captcha': captcha,
        'product_link_count': len(product_links),
        'search_card_count': len(search_cards),
        'asin_card_count': len(asin_values),
        'asin_set': frozenset(asin_values),
        'explicit_no_results': explicit_no_results,
    }


def candidate_cards(soup):
    selectors = [
        '[data-component-type="s-search-result"]',
        '[data-testid="deal-card"]',
        '[data-testid="product-card"]',
        '.DealGridItem-module__dealItemDisplayGrid',
        '.a-cardui',
        '.a-section.a-spacing-base',
        '[data-asin]',
    ]
    cards = []
    seen = set()
    for selector in selectors:
        for node in soup.select(selector):
            ident = id(node)
            if ident not in seen:
                seen.add(ident)
                cards.append(node)
    return cards


def parse_products(html):
    soup = BeautifulSoup(html, 'html.parser')
    products = []
    for card in candidate_cards(soup):
        try:
            asin = (card.get('data-asin') or '').strip()
            href = first_href(card, [
                'h2 a',
                'a[href*="/dp/"]',
                'a[href*="/gp/product/"]',
                'a[data-testid="product-card-link"]',
            ])
            product_url = clean_url(href) if href else asin_to_url(asin)
            if not product_url:
                continue
            title = first_text(card, [
                'h2 span', 'h2', '[data-testid="product-title"]',
                '.a-size-base-plus', '.a-size-medium', '.a-truncate-full', 'span.a-text-normal'
            ]) or f'Amazon.eg product {asin or ""}'.strip()
            current = normalize_price(first_text(card, [
                '.a-price:not(.a-text-price) .a-offscreen',
                '.a-price .a-offscreen',
                '[data-testid="deal-price"]',
                '.a-price-whole',
            ]))
            old = normalize_price(first_text(card, [
                '.a-price.a-text-price .a-offscreen',
                'span[data-a-strike="true"] .a-offscreen',
                '.a-text-price .a-offscreen',
                '.a-price[data-a-strike="true"] .a-offscreen',
            ]))
            visible = card.get_text(' ', strip=True)
            used_price = extract_used_or_secondary_price(card, visible, title)
            promo_info = extract_promotions(visible)

            discount = calculate_discount(old, current)
            if discount is None:
                for pattern in [
                    r'(\d{1,2})\s*%\s*off',
                    r'save\s*(\d{1,2})\s*%',
                    r'خصم\s*(\d{1,2})\s*%',
                    r'(\d{1,2})\s*%\s*خصم',
                ]:
                    m = re.search(pattern, visible, flags=re.I)
                    if m:
                        discount = float(m.group(1))
                        break

            is_warehouse_or_renewed = any(
                w in title.lower() or w in visible.lower()
                for w in ['renewed', 'مجدد', 'مستعمل', 'warehouse', 'like new', 'open box']
            )

            if current and current > 0:
                if not old and discount is not None and 0 < discount < 100:
                    old = round(current / (1 - discount / 100), 2)

                # حساب السعر الفعلي والخصم الفعلي بعد تطبيق الكوبون إن وجد
                effective_current = current
                if promo_info['coupon_pct']:
                    effective_current = round(current * (1 - promo_info['coupon_pct'] / 100.0), 2)
                elif promo_info['coupon_amt'] and current > promo_info['coupon_amt']:
                    effective_current = round(current - promo_info['coupon_amt'], 2)

                if effective_current < current:
                    if old and old > effective_current:
                        discount = calculate_discount(old, effective_current)
                    elif discount is not None and promo_info['coupon_pct']:
                        discount = min(99.0, round(discount + promo_info['coupon_pct'], 1))
                    elif promo_info['coupon_pct']:
                        discount = promo_info['coupon_pct']

                products.append({
                    'asin': extract_asin(card, product_url),
                    'title': title,
                    'url': product_url,
                    'current': effective_current,
                    'listed_price': current if effective_current < current else None,
                    'old': old,
                    'discount': discount,
                    'used_price': used_price if (used_price and used_price < effective_current) else None,
                    'is_used_item': is_warehouse_or_renewed,
                    'promo_text': promo_info['promo_text'],
                    'has_extra_promo': promo_info['has_multi_buy_or_extra'],
                })
        except Exception as e:
            logging.debug('Parse error: %s', e)
    unique = {}
    for product in products:
        key = product['asin'] or product['url']
        current_discount = product.get('discount') or 0
        saved_discount = unique.get(key, {}).get('discount') or 0
        if key not in unique or current_discount > saved_discount:
            unique[key] = product
    return list(unique.values())


def fetch(url):
    r = session.get(url, timeout=35)
    if r.status_code in (429, 503):
        logging.warning('Amazon returned HTTP %s', r.status_code)
    r.raise_for_status()
    return r.text


def format_deal(product, reason, last_price=None, highest_price=None, avg_price=None, real_drop_percent=None, used_saving_percent=None):
    safe_title = html_lib.escape(product['title'])
    safe_reason = html_lib.escape(reason)
    url = product['url']

    header = "♻️ <b>لقطة مستعمل / مجدد - Amazon.eg</b>" if (used_saving_percent or product.get('is_used_item')) else "🔥 <b>تنبيه انخفاض سعر - Amazon.eg</b>"

    lines = [
        header,
        "",
        f"📦 <b>{safe_title}</b>",
        "",
        f"💰 <b>السعر الصافي: {product['current']:.2f} EGP</b>",
    ]

    if product.get('listed_price'):
        lines.append(f"🏷 السعر قبل الكوبون: {product['listed_price']:.2f} EGP")

    if product.get('promo_text'):
        lines.append(f"🎁 <b>عرض إضافي / كوبون: {html_lib.escape(product['promo_text'])}</b>")

    if product.get('used_price') and used_saving_percent:
        lines.append(f"🏷 <b>سعر المستعمل: {product['used_price']:.2f} EGP</b>")
        lines.append(f"⚡ <b>أرخص من الجديد بـ: {used_saving_percent:.1f}%</b>")

    if last_price and last_price != product['current']:
        lines.append(f"📉 آخر سعر مسجل: {last_price:.2f} EGP")
    if avg_price and avg_price > product['current']:
        lines.append(f"📊 متوسط السعر السابق: {avg_price:.2f} EGP")
    if highest_price and highest_price > product['current'] and highest_price != last_price:
        lines.append(f"📈 أعلى سعر مسجل: {highest_price:.2f} EGP")
    if product.get('old'):
        lines.append(f"❌ السعر المشطوب: {product['old']:.2f} EGP")
    if product.get('discount') is not None:
        lines.append(f"📢 إجمالي الخصم المحسوب: {product['discount']:.1f}%")
    if real_drop_percent is not None and real_drop_percent > 0:
        lines.append(f"✅ <b>نسبة الهبوط الحقيقي: {real_drop_percent:.1f}%</b>")

    lines.append(f"💡 سبب التنبيه: {safe_reason}")
    lines.append("")
    lines.append(f"🔗 <a href=\"{url}\">اضغط هنا لفتح المنتج على أمازون</a>")
    lines.append(url)

    return "\n".join(lines)


def scan_url(base_url, max_pages, deals_state, price_history):
    dead_pages_in_a_row = 0
    similar_pages_in_a_row = 0
    previous_asin_set = None
    url_notifications = 0
    pages_scanned = 0
    products_parsed_total = 0
    qualifying_total = 0

    logging.info('=' * 72)
    logging.info('Starting URL: %s', base_url)

    for page in range(1, max_pages + 1):
        url = get_page_url(base_url, page)
        logging.info('Checking %s', url)
        try:
            html = fetch(url)
        except Exception as e:
            logging.warning('Request failed: %s', e)
            break

        pages_scanned += 1
        diag = page_diagnostics(html)
        if diag['captcha']:
            logging.warning('CAPTCHA detected — stopping URL.')
            break

        products = parse_products(html)
        products_parsed_total += len(products)
        qualifying = [p for p in products if (p.get('discount') or 0) >= MIN_DISCOUNT]
        qualifying_total += len(qualifying)

        has_product_evidence = diag['product_link_count'] > 0 or diag['search_card_count'] > 0
        if diag['explicit_no_results'] or not has_product_evidence:
            dead_pages_in_a_row += 1
        else:
            dead_pages_in_a_row = 0

        if dead_pages_in_a_row >= DEAD_PAGE_LIMIT:
            break

        current_asin_set = diag['asin_set']
        if current_asin_set and previous_asin_set:
            if jaccard_similarity(current_asin_set, previous_asin_set) >= SIMILARITY_THRESHOLD:
                similar_pages_in_a_row += 1
            else:
                similar_pages_in_a_row = 0
        else:
            similar_pages_in_a_row = 0

        if current_asin_set:
            previous_asin_set = current_asin_set
        if similar_pages_in_a_row >= SIMILAR_PAGE_LIMIT:
            break

        for product in products:
            asin = product['asin']
            current_price = product['current']
            used_price = product.get('used_price')
            history = price_history.get(asin, {})
            history_list = history.get('history', [])

            last_price = history.get('last_price')
            if last_price is None:
                legacy = deals_state.get(asin, {})
                last_price = legacy.get('current')

            past_prices = [float(item['price']) for item in history_list if item.get('price') is not None]
            if last_price is not None and not past_prices:
                past_prices = [float(last_price)]

            highest_price = max(past_prices) if past_prices else None
            lowest_price = min(past_prices) if past_prices else None
            avg_price = (sum(past_prices) / len(past_prices)) if past_prices else None

            drop_vs_last = calculate_discount(last_price, current_price) if last_price else None
            drop_vs_avg = calculate_discount(avg_price, current_price) if avg_price else None
            drop_vs_high = calculate_discount(highest_price, current_price) if highest_price else None

            used_saving_percent = calculate_discount(current_price, used_price) if used_price else None
            last_alerted_used_price = history.get('last_alerted_used_price')

            real_drops = [d for d in (drop_vs_last, drop_vs_avg, drop_vs_high) if d is not None]
            best_real_drop = max(real_drops) if real_drops else None

            advertised_discount = product.get('discount')
            effective_min_discount = max(30.0, MIN_DISCOUNT - 15.0) if product.get('has_extra_promo') else MIN_DISCOUNT
            qualifies_by_badge = advertised_discount is not None and advertised_discount >= effective_min_discount

            first_seen = (not history) and (asin not in deals_state)
            is_lower_than_last = last_price is not None and current_price < float(last_price)
            last_alerted_price = history.get('last_alerted_price')

            reason = None
            already_alerted_at_this_price = (
                last_alerted_price is not None and current_price >= float(last_alerted_price)
            )

            if (
                used_price
                and used_saving_percent is not None
                and used_saving_percent >= USED_VS_NEW_ALERT_PERCENT
                and (last_alerted_used_price is None or used_price < float(last_alerted_used_price))
            ):
                reason = f'نسخة مستعملة أرخص بـ {used_saving_percent:.1f}% من الجديد'
                last_alerted_used_price = used_price

            elif not already_alerted_at_this_price:
                if drop_vs_last is not None and drop_vs_last >= PRICE_DROP_ALERT_PERCENT:
                    reason = f'هبوط مفاجئ في السعر بنسبة {drop_vs_last:.1f}%'
                elif is_lower_than_last and drop_vs_avg is not None and drop_vs_avg >= PRICE_DROP_ALERT_PERCENT:
                    reason = f'تعديل سعر مخفي بنسبة {drop_vs_avg:.1f}% عن المتوسط'
                elif first_seen and qualifies_by_badge:
                    reason = f'عرض جديد بخصم {advertised_discount:.0f}%'
                elif is_lower_than_last and product.get('promo_text') and qualifies_by_badge:
                    reason = f'سعر منخفض مع عرض إضافي ({product["promo_text"]})'

            if reason:
                telegram_send(
                    format_deal(
                        product,
                        reason,
                        last_price=last_price,
                        highest_price=highest_price,
                        avg_price=avg_price,
                        real_drop_percent=best_real_drop,
                        used_saving_percent=used_saving_percent,
                    )
                )
                url_notifications += 1
                if not used_saving_percent:
                    last_alerted_price = current_price

            if not history_list or last_price is None or float(last_price) != current_price:
                history_list.append({'timestamp': now_ts(), 'price': current_price})
                if PRICE_HISTORY_MAX_ENTRIES > 0:
                    history_list = history_list[-PRICE_HISTORY_MAX_ENTRIES:]

            all_prices = [float(item['price']) for item in history_list if item.get('price') is not None]
            price_history[asin] = {
                'title': product['title'],
                'url': product['url'],
                'last_price': current_price,
                'lowest_price': min(all_prices) if all_prices else current_price,
                'highest_price': max(all_prices) if all_prices else current_price,
                'last_alerted_price': last_alerted_price,
                'last_alerted_used_price': last_alerted_used_price,
                'history': history_list,
                'last_seen': now_ts(),
            }

            deals_state[asin] = {
                'title': product['title'],
                'current': current_price,
                'used_price': used_price,
                'old': product.get('old'),
                'discount': product.get('discount'),
                'promo_text': product.get('promo_text'),
                'url': product['url'],
                'last_seen': now_ts(),
            }

        save_json(DEALS_STATE_FILE, deals_state)
        save_json(PRICE_HISTORY_FILE, price_history)
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    return {
        'pages_scanned': pages_scanned,
        'products_parsed': products_parsed_total,
        'qualifying_found': qualifying_total,
        'notifications': url_notifications,
    }


def run_cycle():
    deals_state = load_json(DEALS_STATE_FILE, {})
    price_history = load_json(PRICE_HISTORY_FILE, {})
    scan_state = load_json(SCAN_STATE_FILE, {})

    deep_scan = should_do_deep_scan(scan_state)
    max_pages = DEEP_SCAN_PAGES if deep_scan else NORMAL_SCAN_PAGES
    mode_name = 'DEEP' if deep_scan else 'NORMAL'

    totals = {'pages_scanned': 0, 'products_parsed': 0, 'qualifying_found': 0, 'notifications': 0}
    for base_url in URLS:
        res = scan_url(base_url, max_pages, deals_state, price_history)
        for k in totals:
            totals[k] += res[k]

    scan_state['last_cycle_ts'] = now_ts()
    scan_state['last_cycle_mode'] = mode_name
    if deep_scan:
        scan_state['last_deep_scan_ts'] = now_ts()
    save_json(SCAN_STATE_FILE, scan_state)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit('Missing token')
    if not URLS:
        raise SystemExit('No URLs')

    if os.getenv('RUN_ONCE', '0').strip() == '1':
        run_cycle()
        return

    if SEND_START_MESSAGE:
        telegram_send('✅ <b>Amazon Bot Started (Coupons & Promos Enabled)</b>')

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            break
        except Exception:
            logging.exception('Error')
        time.sleep(CHECK_EVERY_MINUTES * 60)


if __name__ == '__main__':
    main()

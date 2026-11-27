def parse_products(html):
    soup = BeautifulSoup(html, 'html.parser')
    logging.info(
        'HTML diag | len=%s | s_cards=%s | dp_links=%s | data_asin=%s',
        len(html),
        len(soup.select('[data-component-type="s-search-result"]')),
        len(soup.select('a[href*="/dp/"]')),
        len(soup.select('[data-asin]')),
    )
    products = []
    cards = soup.select('[data-component-type="s-search-result"]') or candidate_cards(soup)
    for card in cards:
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
            ]) or f'Amazon.eg Used Product {asin or ""}'.strip()

            visible = card.get_text(' ', strip=True)
            low_check = f"{title} {visible}".lower()
            if any(k in low_check for k in [
                'amazon now', 'now.amazon',
                'delivery in 15', 'delivery in 30', 'delivery in 60',
                'توصيل خلال 15', 'توصيل خلال 30', 'توصيل خلال 60',
                'أمازون ناو', 'امازون ناو'
            ]):
                continue

            current = normalize_price(first_text(card, [
                '.a-price:not(.a-text-price) .a-offscreen',
                '.a-price .a-offscreen',
                '[data-testid="deal-price"]',
                '.a-price-whole',
            ]))
            if not current or current <= 0:
                m_price = re.search(r'(?:EGP|E£|جنيه)\s*([\d,]+(?:\.\d+)?)', visible, flags=re.I)
                if m_price:
                    current = normalize_price(m_price.group(1))

            strike_price = normalize_price(first_text(card, [
                '.a-price.a-text-price .a-offscreen',
                'span[data-a-strike="true"] .a-offscreen',
                '.a-text-price .a-offscreen',
                '.a-price[data-a-strike="true"] .a-offscreen',
            ]))

            new_ref_price = extract_new_reference_price(card, visible, strike_price)
            promo_info = extract_promotions(visible)

            if current and current > 0:
                effective_current = current
                if promo_info['coupon_pct']:
                    effective_current = round(current * (1 - promo_info['coupon_pct'] / 100.0), 2)
                elif promo_info['coupon_amt'] and current > promo_info['coupon_amt']:
                    effective_current = round(current - promo_info['coupon_amt'], 2)

                discount_vs_new = calculate_discount(new_ref_price, effective_current) if (new_ref_price and new_ref_price > effective_current) else None

                if discount_vs_new is None:
                    for pattern in [
                        r'(\d{1,2})\s*%\s*off',
                        r'save\s*(\d{1,2})\s*%',
                        r'خصم\s*(\d{1,2})\s*%',
                        r'(\d{1,2})\s*%\s*خصم',
                    ]:
                        m = re.search(pattern, visible, flags=re.I)
                        if m:
                            discount_vs_new = float(m.group(1))
                            if not new_ref_price and 0 < discount_vs_new < 100:
                                new_ref_price = round(effective_current / (1 - discount_vs_new / 100), 2)
                            break

                products.append({
                    'asin': extract_asin(card, product_url),
                    'title': title,
                    'url': product_url,
                    'current': effective_current,
                    'listed_price': current if effective_current < current else None,
                    'new_price': new_ref_price if (new_ref_price and new_ref_price > effective_current) else None,
                    'discount_vs_new': discount_vs_new,
                    'promo_text': promo_info['promo_text'],
                })
        except Exception as e:
            logging.debug('Parse error: %s', e)

    unique = {}
    for product in products:
        key = product['asin'] or product['url']
        if key not in unique or product['current'] < unique[key]['current']:
            unique[key] = product
    return list(unique.values())

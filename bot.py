def extract_promotions(visible_text):
    coupon_pct = None
    coupon_amt = None
    promo_badges = []

    # 1. أي كوبون بنسبة مئوية
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

    # 2. أي كوبون بمبلغ مالي ثابت
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

    # 3. أي عروض إضافية: Buy X Get Y / خصم نسبة معينة عند الدفع أو الشراء / عروض إضافية (بدون بنوك أو بطاقات)
    promo_patterns = [
        r'(buy\s*\d+\s*get\s*\d+[^.,;\n]{0,30})',
        r'(b\d+g\d+[^.,;\n]{0,30})',
        r'(extra\s*\d{1,2}\s*%\s*(?:off|discount)[^.,;\n]{0,35})',
        r'((?:get|save)\s*(?:an\s*)?extra\s*\d{1,2}\s*%[^.,;\n]{0,35})',
        r'(\d{1,2}\s*%\s*(?:off|discount)\s*(?:at\s*checkout|on\s*payment|with\s*(?:code|promo))[^.,;\n]{0,35})',
        r'(save\s*\d{1,2}\s*%[^.,;\n]{0,35})',
        r'(اشترِ?\s*\d+[^.,;\n]{0,35})',
        r'(خصم\s*(?:إضافي|فوري)?\s*\d{1,2}\s*%[^.,;\n]{0,35})',
        r'(خصم\s*\d{1,2}\s*%\s*(?:عند\s*الدفع|عند\s*الشراء|بكود)[^.,;\n]{0,35})',
        r'(وفر\s*\d{1,2}\s*%[^.,;\n]{0,35})',
        r'(عرض\s*إضافي[^.,;\n]{0,35})',
    ]
    for pat in promo_patterns:
        for m in re.finditer(pat, visible_text, flags=re.I):
            badge_text = re.sub(r'\s+', ' ', m.group(1)).strip()
            if badge_text and badge_text.lower() not in [b.lower() for b in promo_badges]:
                promo_badges.append(badge_text)

    # كلمات دالة على عرض إضافي أو خصم عند الدفع بدون ربط بالبنوك
    fallback_keywords = [
        'at checkout', 'on payment', 'extra discount', 'promo code',
        'عند الدفع', 'عند الشراء', 'خصم إضافي', 'كود خصم', 'هدية مجانية', 'مجاناً'
    ]
    if not promo_badges:
        low = visible_text.lower()
        for kw in fallback_keywords:
            if kw in low:
                promo_badges.append(f"عرض إضافي ({kw})")
                break

    return {
        'coupon_pct': coupon_pct,
        'coupon_amt': coupon_amt,
        'promo_text': ' | '.join(promo_badges[:4]) if promo_badges else None,
        'has_multi_buy_or_extra': bool(promo_badges),
    }

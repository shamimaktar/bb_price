#!/usr/bin/env python3
"""Check Best Buy open-box stock and notify via Gmail.

v2 improvements:
- curl_cffi impersonates Chrome TLS fingerprint (Akamai bypass)
- Hits internal button-state & pricing APIs first (definitive signal)
- Page-scrape fallback with fixed false-positive detection
- Detects Akamai block pages and skips cleanly
- Targets "Excellent" condition specifically

Gmail notification, anti-spam state, Config class all preserved from v1.
"""
from __future__ import annotations

import json
import os
import random
import re
import smtplib
import ssl
import time

from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from curl_cffi import requests as curl_requests


# ─── Constants ───────────────────────────────────────────────────
DEFAULT_SKU = "6629541"
DEFAULT_CONDITION = "excellent"
DEFAULT_ZIP = "47907"

PRODUCT_URL = (
    "https://www.bestbuy.com/site/"
    "asus-rog-flow-z13-13-4-2-5k-180hz-touch-screen-gaming-laptop-copilot-pc-"
    "amd-ryzen-ai-max-395-128gb-ram-1tb-ssd-off-black/"
    "{sku}.p?skuId={sku}"
)
OPENBOX_URL = (
    "https://www.bestbuy.com/product/"
    "asus-rog-flow-z13-13-4-2-5k-180hz-touch-screen-gaming-laptop-copilot-pc-"
    "amd-ryzen-ai-max-395-128gb-ram-1tb-ssd-off-black/"
    "JJGGLHC84R/sku/{sku}/openbox?condition={condition}"
)

# FIX: Use profiles that curl_cffi 0.14 actually ships with.
# "chrome" (no version) always maps to the library's latest built-in profile.
IMPERSONATE_PROFILES = ["chrome", "chrome124", "chrome120", "chrome116"]


# ─── Helpers ─────────────────────────────────────────────────────

def _parse_non_negative_int(raw_value: str | None, default: int = 0) -> int:
    value = (raw_value or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _get_profile() -> str:
    return random.choice(IMPERSONATE_PROFILES)


def _get_headers(accept: str = "text/html", referer: str = "") -> dict:
    h = {
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    if referer:
        h["Referer"] = referer
    return h


def _is_akamai_block(text: str) -> bool:
    t = text.lower()
    return (
        "access denied" in t
        or "reference #" in t
        or "access to this page has been denied" in t
    )


class FetchError(Exception):
    """Raised when all fetch attempts fail."""


def _fetch(url: str, profile: str, headers: dict, timeout: int = 20) -> str:
    """Fetch URL with curl_cffi. Auto-retries with 'chrome' if profile unsupported."""
    try:
        r = curl_requests.get(
            url, headers=headers, impersonate=profile,
            timeout=timeout, allow_redirects=True,
        )
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code}")
        return r.text
    except FetchError:
        raise
    except Exception as e:
        err_msg = str(e).lower()
        # If the chosen profile isn't bundled, fall back to generic "chrome"
        if "not supported" in err_msg and profile != "chrome":
            print(f"  Profile '{profile}' unsupported, retrying with 'chrome'")
            return _fetch(url, "chrome", headers, timeout)
        raise FetchError(str(e))


# ─── Config (preserved from v1) ─────────────────────────────────

@dataclass
class Config:
    product_url: str
    openbox_url: str
    sku: str
    condition: str
    zip_code: str
    smtp_user: str
    smtp_password: str
    notify_to: str
    notify_from: str
    state_file: Path
    reminder_minutes: int

    @classmethod
    def from_env(cls) -> "Config":
        sku = os.getenv("BESTBUY_SKU", DEFAULT_SKU).strip()
        condition = os.getenv("BESTBUY_CONDITION", DEFAULT_CONDITION).strip()
        zip_code = os.getenv("BESTBUY_ZIP", DEFAULT_ZIP).strip()

        product_url = PRODUCT_URL.format(sku=sku)
        openbox_url = OPENBOX_URL.format(sku=sku, condition=condition)

        required = {
            "GMAIL_SMTP_USER": os.getenv("GMAIL_SMTP_USER", "").strip(),
            "GMAIL_SMTP_APP_PASSWORD": os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip(),
            "NOTIFY_TO_EMAIL": os.getenv("NOTIFY_TO_EMAIL", "").strip(),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

        notify_from = os.getenv("NOTIFY_FROM_EMAIL", required["GMAIL_SMTP_USER"]).strip()
        state_file = Path(os.getenv("STATE_FILE", ".state/open_box_state.json")).resolve()
        reminder_minutes = _parse_non_negative_int(os.getenv("REMINDER_MINUTES"), default=0)

        return cls(
            product_url=product_url,
            openbox_url=openbox_url,
            sku=sku,
            condition=condition,
            zip_code=zip_code,
            smtp_user=required["GMAIL_SMTP_USER"],
            smtp_password=required["GMAIL_SMTP_APP_PASSWORD"],
            notify_to=required["NOTIFY_TO_EMAIL"],
            notify_from=notify_from,
            state_file=state_file,
            reminder_minutes=reminder_minutes,
        )


# ─── Detection strategies ────────────────────────────────────────

def check_button_api(config: Config, profile: str) -> dict:
    """Best Buy's internal button-state API.

    This is the source of truth — returns the exact state the frontend
    renders (ADD_TO_CART, SOLD_OUT, COMING_SOON, etc.) for a specific
    SKU + condition + zip code.
    """
    result = {"available": False, "price": None, "method": "button_api", "error": None}

    url = (
        "https://www.bestbuy.com/api/tcfb/model.json"
        f'?paths=[["shop","buttonstate","v5","item","skus",{config.sku},'
        f'"conditions","{config.condition.upper()}",'
        f'"destinationZipCode","{config.zip_code}",'
        f'"storeId","","context","cyp","addAll","false"]]'
        f"&method=get"
    )

    headers = _get_headers(accept="application/json", referer=config.product_url)
    headers["sec-fetch-dest"] = "empty"
    headers["sec-fetch-mode"] = "cors"
    headers["sec-fetch-site"] = "same-origin"

    try:
        text = _fetch(url, profile, headers, timeout=15)
    except FetchError as e:
        result["error"] = str(e)
        return result

    if _is_akamai_block(text):
        result["error"] = "akamai_block"
        return result

    text_lower = text.lower()
    if "add_to_cart" in text_lower or '"add to cart"' in text_lower:
        result["available"] = True
        print("  [button_api] ADD_TO_CART detected")
    elif "sold_out" in text_lower:
        print("  [button_api] SOLD_OUT")
    elif "coming_soon" in text_lower:
        print("  [button_api] COMING_SOON")
    else:
        print(f"  [button_api] Unknown response: {text[:200]}")

    return result


def check_pricing_api(config: Config, profile: str) -> dict:
    """Best Buy pricing API — includes openBoxPrice when OB is available."""
    result = {"available": False, "price": None, "method": "pricing_api", "error": None}

    url = (
        f"https://www.bestbuy.com/pricing/v1/price/item"
        f"?allFinanceOffers=true&catalog=bby&context=cyp"
        f"&includeOpenboxPrice=true&salesChannel=LargeView"
        f"&skuId={config.sku}&useCabo=true"
    )

    headers = _get_headers(accept="application/json", referer=config.product_url)
    headers["sec-fetch-dest"] = "empty"
    headers["sec-fetch-mode"] = "cors"
    headers["sec-fetch-site"] = "same-origin"

    try:
        text = _fetch(url, profile, headers, timeout=15)
    except FetchError as e:
        result["error"] = str(e)
        return result

    if _is_akamai_block(text):
        result["error"] = "akamai_block"
        return result

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        result["error"] = "bad_json"
        return result

    ob = data.get("openBoxPrice") or data.get("openbox") or {}
    if ob:
        print(f"  [pricing_api] Open box data found: {json.dumps(ob)[:200]}")
        result["available"] = True
        # Try extracting price for the target condition
        if isinstance(ob, dict):
            for key, val in ob.items():
                if config.condition in str(key).lower():
                    if isinstance(val, (int, float)):
                        result["price"] = f"${val}"
                    elif isinstance(val, dict):
                        p = val.get("lowPrice") or val.get("price") or val.get("currentPrice")
                        if p:
                            result["price"] = f"${p}"
    else:
        print("  [pricing_api] No open box pricing in response")

    return result


def check_openbox_page(config: Config, profile: str) -> dict:
    """Fallback: load the open-box page, look for definitive signals.

    IMPORTANT: We look for button-state data and ADD_TO_CART, NOT for the
    generic words "open box" which appear on every BB page (nav, breadcrumbs).
    """
    result = {"available": False, "price": None, "method": "page", "error": None}

    headers = _get_headers()
    try:
        text = _fetch(config.openbox_url, profile, headers, timeout=25)
    except FetchError as e:
        result["error"] = str(e)
        return result

    if _is_akamai_block(text):
        result["error"] = "akamai_block"
        return result

    text_lower = text.lower()

    # Definitive NOT-available signals
    not_avail_signals = [
        "sold out", "coming soon", "currently unavailable",
        "check back later", "item not available", "not available nearby",
    ]
    for sig in not_avail_signals:
        if sig in text_lower:
            print(f"  [page] Not available (matched: '{sig}')")
            return result

    # Definitive AVAILABLE signals — look for actual button state, not generic text
    # 1. data-button-state attribute set to ADD_TO_CART
    if re.search(r'data-button-state\s*=\s*["\']ADD_TO_CART', text, re.I):
        result["available"] = True
        print("  [page] ADD_TO_CART button attribute found")
    # 2. ADD_TO_CART string in page source (API data embedded in page)
    elif "ADD_TO_CART" in text and "SOLD_OUT" not in text:
        result["available"] = True
        print("  [page] ADD_TO_CART in page source")
    # 3. A non-disabled Add to Cart button in the open-box buying section
    elif re.search(
        r'class="[^"]*add-to-cart[^"]*"[^>]*(?!disabled)',
        text, re.I,
    ):
        result["available"] = True
        print("  [page] Add-to-cart button element found")

    # Price extraction
    m = re.search(
        r'(?:excellent|open.?box)[^$]{0,80}(\$[\d,]+\.?\d{0,2})',
        text, re.I,
    )
    if m:
        result["price"] = m.group(1)

    return result


# ─── Gmail notification (preserved from v1) ─────────────────────

def send_email(config: Config, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.notify_from
    message["To"] = config.notify_to
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)


# ─── State management (preserved from v1) ────────────────────────

def read_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {"last_available": False, "last_notified_at": 0}

    try:
        with state_file.open("r", encoding="utf-8") as f:
            state = json.load(f)
        return {
            "last_available": bool(state.get("last_available", False)),
            "last_notified_at": int(state.get("last_notified_at", 0)),
        }
    except (json.JSONDecodeError, OSError, ValueError):
        return {"last_available": False, "last_notified_at": 0}


def write_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def should_notify(now_available: bool, state: dict, reminder_minutes: int) -> bool:
    if not now_available:
        return False
    if not state.get("last_available", False):
        return True
    if reminder_minutes <= 0:
        return False
    elapsed = int(time.time()) - int(state.get("last_notified_at", 0))
    return elapsed >= (reminder_minutes * 60)


# ─── Main ────────────────────────────────────────────────────────

def main() -> None:
    config = Config.from_env()
    profile = _get_profile()

    print(f"SKU: {config.sku} | Condition: {config.condition} | "
          f"ZIP: {config.zip_code} | Profile: {profile}")

    # --- Run detection strategies in priority order ----------------
    results: list[dict] = []
    akamai_blocked = False

    # Strategy 1: Button-state API (fastest, most authoritative)
    print("[1/3] Button-state API...")
    r1 = check_button_api(config, profile)
    results.append(r1)
    if r1.get("error") == "akamai_block":
        akamai_blocked = True
        print("  Akamai blocked this runner IP")

    if not akamai_blocked:
        time.sleep(random.uniform(0.5, 1.5))

        # Strategy 2: Pricing API (has open-box price data)
        print("[2/3] Pricing API...")
        r2 = check_pricing_api(config, profile)
        results.append(r2)
        if r2.get("error") == "akamai_block":
            akamai_blocked = True

    if not akamai_blocked and not any(r["available"] for r in results):
        time.sleep(random.uniform(0.5, 1.5))

        # Strategy 3: Open-box page scrape (fallback)
        print("[3/3] Open-box page...")
        r3 = check_openbox_page(config, profile)
        results.append(r3)

    # --- Evaluate results -----------------------------------------
    now_available = any(r["available"] for r in results)
    best_price = next((r["price"] for r in results if r.get("price")), None)
    methods_hit = ", ".join(r["method"] for r in results if r["available"])
    all_blocked = all(r.get("error") == "akamai_block" for r in results)

    if all_blocked:
        print("All requests Akamai-blocked (runner IP flagged). "
              "Next run gets a new IP — this is expected.")
        return  # Don't update state on a blocked run

    state = read_state(config.state_file)

    if should_notify(now_available, state, config.reminder_minutes):
        price_line = f"Price: {best_price}\n" if best_price else ""
        send_email(
            config,
            subject="🚨 Best Buy open-box EXCELLENT in stock!",
            body=(
                "Open-box EXCELLENT condition was detected!\n\n"
                f"SKU: {config.sku}\n"
                f"Condition: {config.condition.title()}\n"
                f"{price_line}"
                f"Open-box page: {config.openbox_url}\n"
                f"Product page: {config.product_url}\n\n"
                f"Detected via: {methods_hit}\n"
                "Check out ASAP — these sell in minutes!"
            ),
        )
        print(f"AVAILABLE — email sent (via {methods_hit})")
        state["last_notified_at"] = int(time.time())
    elif now_available:
        print(f"AVAILABLE (via {methods_hit}) — notification suppressed by anti-spam.")
    else:
        print("Not available.")
        # Log which strategies ran successfully vs errored
        for r in results:
            status = "OK" if r["error"] is None else r["error"]
            print(f"  {r['method']}: {status}")

    state["last_available"] = now_available
    write_state(config.state_file, state)


if __name__ == "__main__":
    main()
        

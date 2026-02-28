#!/usr/bin/env python3
"""Check Best Buy product page for open-box stock and notify via Gmail.

No Best Buy API key required.
Anti-spam behavior:
- sends email when stock transitions from unavailable -> available
- optional reminder interval while still available (default disabled)
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any


DEFAULT_PRODUCT_URL = (
    "https://www.bestbuy.com/product/"
    "asus-rog-flow-z13-13-4-2-5k-180hz-touch-screen-gaming-laptop-copilot-pc-"
    "amd-ryzen-ai-max-395-128gb-ram-1tb-ssd-off-black/JJGGLHC84R/sku/6629541"
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

def _parse_non_negative_int(raw_value: str | None, default: int = 0) -> int:
    value = (raw_value or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


@dataclass
class Config:
    product_url: str
    sku: str
    smtp_user: str
    smtp_password: str
    notify_to: str
    notify_from: str
    state_file: Path
    reminder_minutes: int

    @classmethod
    def from_env(cls) -> "Config":
        product_url = os.getenv("BESTBUY_PRODUCT_URL", DEFAULT_PRODUCT_URL).strip()
        sku = os.getenv("BESTBUY_SKU", "6629541").strip()
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
            sku=sku,
            smtp_user=required["GMAIL_SMTP_USER"],
            smtp_password=required["GMAIL_SMTP_APP_PASSWORD"],
            notify_to=required["NOTIFY_TO_EMAIL"],
            notify_from=notify_from,
            state_file=state_file,
            reminder_minutes=reminder_minutes,
        )


def fetch_product_page(product_url: str) -> str:
    import requests

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    response = requests.get(product_url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def _scan_for_open_box_signal(node: Any) -> bool:
    if isinstance(node, dict):
        keys = {str(key).lower() for key in node.keys()}
        text = " ".join(str(v).lower() for v in node.values() if isinstance(v, (str, int, float, bool)))
        if (
            any("open" in key and "box" in key for key in keys)
            or "open box" in text
            or "open-box" in text
        ) and not any(x in text for x in ["unavailable", "sold out", "not available"]):
            return True

        for value in node.values():
            if _scan_for_open_box_signal(value):
                return True
    elif isinstance(node, list):
        return any(_scan_for_open_box_signal(item) for item in node)
    elif isinstance(node, str):
        value = node.lower()
        if ("open box" in value or "open-box" in value) and not any(
            x in value for x in ["no open-box options", "unavailable", "sold out"]
        ):
            return True
    return False


def _extract_next_data_json(html: str) -> dict[str, Any] | None:
    match = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(?P<data>{.*?})</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group("data"))
    except json.JSONDecodeError:
        return None


def has_open_box_stock(html: str) -> bool:
    page_text = re.sub(r"\s+", " ", html).lower()

    strong_positive = [
        "open-box",
        "open box",
        "see open-box",
        "open-box excellent",
        "open-box good",
        "open-box fair",
    ]
    negative = [
        "no open-box options",
        "open-box unavailable",
        "open box unavailable",
        "sold out",
    ]

    if any(signal in page_text for signal in strong_positive) and not any(
        signal in page_text for signal in negative
    ):
        return True

    next_data = _extract_next_data_json(html)
    if next_data and _scan_for_open_box_signal(next_data):
        return True

    return False


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


def main() -> None:
    config = Config.from_env()
    html = fetch_product_page(config.product_url)
    now_available = has_open_box_stock(html)
    state = read_state(config.state_file)

    if should_notify(now_available, state, config.reminder_minutes):
        send_email(
            config,
            subject="🚨 Best Buy open-box stock detected",
            body=(
                "Open-box availability was detected from Best Buy product page signals.\n\n"
                f"SKU: {config.sku}\n"
                f"Product: {config.product_url}\n"
                "Check out ASAP before it sells out."
            ),
        )
        print("Open-box stock detected. Notification email sent.")
        state["last_notified_at"] = int(time.time())
    elif now_available:
        print("Open-box stock detected, but notification suppressed by anti-spam logic.")
    else:
        print("No open-box stock detected.")

    state["last_available"] = now_available
    write_state(config.state_file, state)


if __name__ == "__main__":
    main()

# Best Buy Open-Box Tracker (GitHub Actions + Gmail)

This repo checks Best Buy open-box availability every 5 minutes (minimum GitHub schedule interval) and sends a Gmail alert when stock is detected.

## No Best Buy API key mode (what this uses)
You said you don't have Best Buy API access, so this tracker uses the best no-key alternative:
- fetches the public product page
- detects open-box signals from page text and embedded `__NEXT_DATA__` JSON
- sends anti-spam alerts (state change + optional reminder)

> Note: no-key methods are less stable than official APIs and may need updates if Best Buy changes markup.

## Your tracked item
- URL: `https://www.bestbuy.com/product/asus-rog-flow-z13-13-4-2-5k-180hz-touch-screen-gaming-laptop-copilot-pc-amd-ryzen-ai-max-395-128gb-ram-1tb-ssd-off-black/JJGGLHC84R/sku/6629541`
- Model: `GZ302EA-XS99`
- SKU: `6629541`

## What I need from you (safe for public repo)
### GitHub Actions **Secrets** (private)
1. `GMAIL_SMTP_USER`
2. `GMAIL_SMTP_APP_PASSWORD`
3. `NOTIFY_TO_EMAIL`
4. `NOTIFY_FROM_EMAIL` *(optional)*

### GitHub Actions **Variables** (optional)
1. `BESTBUY_PRODUCT_URL` (optional override)
2. `BESTBUY_SKU` (optional override)
3. `REMINDER_MINUTES` *(optional; default `0`)*

The workflow already defaults to your provided URL and SKU.

## Privacy for public repo
- Keep personal email/password only in **Secrets**, not in files.
- `.env*` and `.state/` are ignored via `.gitignore`.
- Rotate credentials immediately if exposed.
- Prefer a dedicated alert-only Gmail account.

## Enable workflow
1. Push to GitHub.
2. Go to **Settings → Secrets and variables → Actions**.
3. Add Secrets listed above.
4. (Optional) Add Variables if you want overrides.
5. Run **Best Buy Open-Box Checker** manually once.
6. Let the schedule run every 5 minutes.

## Anti-spam behavior
- Sends on transition from unavailable → available.
- Suppresses duplicates while still available.
- Optional periodic reminders via `REMINDER_MINUTES`.

## Speed limits
GitHub Actions cannot run faster than every 5 minutes. For 30–60 second checks, run this script on an always-on server.


## Network/transient error handling
- The checker retries Best Buy fetches automatically on transient request failures/timeouts.
- If fetch still fails after retries, the run exits gracefully (no crash) and tries again on next schedule.

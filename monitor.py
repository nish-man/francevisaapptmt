#!/usr/bin/env python3
"""
TLS Visa Appointment Slot Monitor
Polls visas-fr.tlscontact.com for available France-visa appointment slots.

Usage:
  python monitor.py --login          # one-time manual login + API discovery
  python monitor.py                  # headless polling (every 5 min)
  python monitor.py --interval 10    # poll every 10 minutes
  python monitor.py --check          # single check then exit (for testing)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.sync_api import (
    BrowserContext,
    Page,
    Response,
    sync_playwright,
)

load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────
VAC_CODE = "gbLON2fr"
APP_GROUP_ID = "27253264"
BASE_URL = "https://visas-fr.tlscontact.com"
BOOKING_URL = f"{BASE_URL}/workflow/appointment-booking/{VAC_CODE}/{APP_GROUP_ID}"

COOKIES_FILE = Path("tls_cookies.json")
CONFIG_FILE = Path("tls_config.json")
LOG_FILE = "tls_monitor.log"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Optional: override chromium binary path via env var or auto-detect fallbacks.
_CHROMIUM_FALLBACK_PATHS = [
    # Playwright pre-installed in cloud/CI environments
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium-1117/chrome-linux/chrome",
    # Snap / apt installs
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
]


def _find_chromium() -> Optional[str]:
    """Return chromium executable path override, or None to use Playwright default."""
    env_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "")
    if env_path and Path(env_path).exists():
        return env_path
    for p in _CHROMIUM_FALLBACK_PATHS:
        if Path(p).exists():
            return p
    return None


def _launch_browser(pw, headless: bool):
    """Launch Chromium, falling back to known binary paths if the default is absent."""
    kwargs: dict = {"headless": headless}
    if not headless:
        kwargs["slow_mo"] = 30
    exe = _find_chromium()
    if exe:
        kwargs["executable_path"] = exe
        log.debug("Using chromium: %s", exe)
    return pw.chromium.launch(**kwargs)

# Keywords that suggest a response body contains slot/availability data
_SLOT_URL_RE = re.compile(
    r"slot|appointment|availab|calendar|booking|timeslot|schedule|capacity",
    re.IGNORECASE,
)
# Date pattern used to heuristically detect slot data in JSON
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

log = logging.getLogger("tls_monitor")


# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO") -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
    logging.basicConfig(format=fmt, datefmt=datefmt, level=level, handlers=handlers)


# ── Email / SMS ────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")
    to_addr = os.getenv("EMAIL_TO", "")

    if not (user and password and to_addr):
        log.warning("Email skipped — set SMTP_USER / SMTP_PASS / EMAIL_TO in .env")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL(host, port) as srv:
            srv.login(user, password)
            srv.sendmail(user, to_addr, msg.as_string())
        log.info("Email sent → %s", to_addr)
    except Exception as exc:
        log.error("Email failed: %s", exc)


def send_sms(body: str) -> None:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_num = os.getenv("TWILIO_FROM", "")
    to_num = os.getenv("TWILIO_TO", "")

    if not (sid and token and from_num and to_num):
        return  # SMS not configured — silently skip

    try:
        from twilio.rest import Client  # type: ignore[import-untyped]

        Client(sid, token).messages.create(body=body[:1600], from_=from_num, to=to_num)
        log.info("SMS sent → %s", to_num)
    except ImportError:
        log.warning("twilio not installed — SMS skipped (pip install twilio)")
    except Exception as exc:
        log.error("SMS failed: %s", exc)


def notify(subject: str, body: str) -> None:
    send_email(subject, body)
    send_sms(body)


# ── Cookie helpers ─────────────────────────────────────────────────────────────

def save_cookies(context: BrowserContext) -> None:
    cookies = context.cookies()
    COOKIES_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    log.info("Saved %d cookies → %s", len(cookies), COOKIES_FILE)


def load_cookies(context: BrowserContext) -> bool:
    if not COOKIES_FILE.exists():
        log.error("No cookie file found (%s). Run --login first.", COOKIES_FILE)
        return False
    try:
        cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        context.add_cookies(cookies)
        log.info("Loaded %d cookies from %s", len(cookies), COOKIES_FILE)
        return True
    except Exception as exc:
        log.error("Cookie load failed: %s", exc)
        return False


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    log.info("Config saved → %s", CONFIG_FILE)


# ── Slot parsing ───────────────────────────────────────────────────────────────

def _walk_json(obj, path: str = "") -> list[str]:
    """Walk any JSON value and return path=value strings that look like slot data."""
    results: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(_walk_json(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            results.extend(_walk_json(item, f"{path}[{i}]"))
    else:
        s = str(obj)
        if _DATE_RE.search(s):
            results.append(f"{path}={s}")
        elif isinstance(obj, bool) and obj and re.search(r"availab", path, re.I):
            results.append(f"{path}=true")
    return results


def parse_slots_from_body(body: str) -> list[str]:
    """Return slot descriptor strings from a raw JSON response body."""
    body = body.strip()
    if not body or body[0] not in ("{", "["):
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    return _walk_json(data)


def has_available_slots(slots: list[str]) -> bool:
    """
    True if the slot data contains paths with real slot/date keywords.
    Uses word boundaries so "updatedAt" does not falsely match "date".
    """
    if not slots:
        return False
    # \b ensures "date" in "updatedAt" does not match
    slot_path_re = re.compile(r"\b(slot|date|dates|availab|calendar|timeslot)\b", re.I)
    return any(slot_path_re.search(s.split("=")[0]) for s in slots)


def looks_like_slot_api(url: str, body: str) -> bool:
    """Heuristic: is this response likely the slot availability endpoint?"""
    if not _SLOT_URL_RE.search(url):
        return False
    b = body.strip()
    return bool(b) and b[0] in ("{", "[")


# ── Login / discovery mode ─────────────────────────────────────────────────────

def run_login_mode() -> None:
    """
    Open a visible browser, let the user log in manually, then:
    • save session cookies
    • intercept network responses to auto-discover the slot API endpoint
    """
    print(
        "\n"
        "=== TLS MONITOR — LOGIN MODE ===\n"
        "1. A browser window will open.\n"
        "2. Log in to visas-fr.tlscontact.com.\n"
        "3. Navigate to the appointment booking / slot selection page.\n"
        "4. Browse around until you see the calendar / date picker load.\n"
        "5. Come back here and press Enter to save your session.\n"
    )

    discovered: dict[str, str] = {}  # url → body excerpt
    cfg = load_config()

    def on_response(resp: Response) -> None:
        url = resp.url
        if not url.startswith(BASE_URL) and "tlscontact" not in url:
            return
        try:
            body = resp.text()
            if looks_like_slot_api(url, body) and url not in discovered:
                log.info("[DISCOVERED] %s", url)
                discovered[url] = body[:600]
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = _launch_browser(pw, headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        page.on("response", on_response)

        log.info("Navigating to: %s", BOOKING_URL)
        page.goto(BOOKING_URL, timeout=60_000)

        input("\n[ACTION REQUIRED] Log in and reach the slot selection page, then press Enter here...\n")

        save_cookies(context)
        browser.close()

    # Prompt user to confirm the correct endpoint
    if discovered:
        print("\nDiscovered potential slot API endpoints:")
        urls = list(discovered.keys())
        for i, u in enumerate(urls):
            print(f"  [{i}] {u}")

        choice = input(
            "\nEnter the number of the correct slot endpoint "
            "(Enter to auto-select first, 's' to skip): "
        ).strip()

        if choice == "s":
            log.info("Skipped endpoint selection — will auto-detect during polling")
        elif choice.isdigit() and int(choice) < len(urls):
            cfg["slot_api_url"] = urls[int(choice)]
            log.info("Saved slot API: %s", cfg["slot_api_url"])
        else:
            cfg["slot_api_url"] = urls[0]
            cfg["slot_api_candidates"] = urls
            log.info("Auto-selected: %s", cfg["slot_api_url"])
    else:
        log.warning(
            "No slot endpoints discovered. The monitor will intercept all TLS "
            "network traffic during polling and auto-detect slot data."
        )

    save_config(cfg)
    print(
        "\nLogin complete!\n"
        "Run  python monitor.py  to start polling.\n"
        f"Cookies: {COOKIES_FILE}   Config: {CONFIG_FILE}\n"
    )


# ── Polling mode ───────────────────────────────────────────────────────────────

_LOGIN_PATH_RE = re.compile(r"/(login|auth|signin|sign-in)", re.I)


def _is_session_expired(page: Page) -> bool:
    return bool(_LOGIN_PATH_RE.search(page.url))


def _check_once(context: BrowserContext, cfg: dict) -> tuple[Optional[bool], list[str]]:
    """
    Load the booking page, intercept API responses, parse slot data.

    Returns:
        (True, slots)  — slots found
        (False, [])    — no slots
        (None, [])     — session expired
    """
    slot_api_url: str = cfg.get("slot_api_url", "")
    captured: dict[str, str] = {}

    def on_response(resp: Response) -> None:
        url = resp.url
        if not (url.startswith(BASE_URL) or "tlscontact" in url):
            return
        try:
            body = resp.text()
            if slot_api_url and slot_api_url in url:
                captured[url] = body
                log.debug("Captured configured API: %s", url)
            elif looks_like_slot_api(url, body):
                captured[url] = body
                log.debug("Captured candidate: %s", url)
        except Exception:
            pass

    page = context.new_page()
    try:
        page.on("response", on_response)
        page.goto(BOOKING_URL, timeout=60_000, wait_until="networkidle")

        if _is_session_expired(page):
            return None, []

        # Give deferred JS/fetch calls time to fire
        page.wait_for_timeout(4_000)

        all_slots: list[str] = []
        for url, body in captured.items():
            slots = parse_slots_from_body(body)
            if has_available_slots(slots):
                all_slots.extend(slots)
                log.info("Slots in %s: %s", url, slots[:10])
            else:
                log.debug("No usable slots in %s", url)

        return bool(all_slots), all_slots

    except Exception as exc:
        log.error("Page error: %s", exc)
        return False, []
    finally:
        page.close()


def run_poll_mode(interval_min: int, single_check: bool = False) -> None:
    if not COOKIES_FILE.exists():
        log.error("No saved session. Run  python monitor.py --login  first.")
        sys.exit(1)

    cfg = load_config()
    slot_api = cfg.get("slot_api_url", "")
    log.info("=== POLLING MODE === interval=%d min", interval_min)
    if slot_api:
        log.info("Slot API: %s", slot_api)
    else:
        log.info("No slot API configured — will auto-detect from network traffic")

    consecutive_errors = 0
    MAX_ERRORS = 5

    with sync_playwright() as pw:
        browser = _launch_browser(pw, headless=True)
        context = browser.new_context(user_agent=USER_AGENT)

        if not load_cookies(context):
            browser.close()
            sys.exit(1)

        while True:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.info("[%s] Checking slots...", ts)

            try:
                result, slots = _check_once(context, cfg)
                consecutive_errors = 0

                if result is None:
                    _handle_session_expiry()
                    browser.close()
                    sys.exit(2)

                elif result:
                    _handle_slots_found(slots)
                else:
                    log.info("No slots available.")

            except Exception as exc:
                consecutive_errors += 1
                log.error("Error %d/%d: %s", consecutive_errors, MAX_ERRORS, exc)
                if consecutive_errors >= MAX_ERRORS:
                    subject = "TLS Monitor: Too Many Errors — Check Logs"
                    body = (
                        f"The monitor has failed {MAX_ERRORS} times consecutively.\n"
                        f"Last error: {exc}\n\n"
                        f"Check {LOG_FILE} for details."
                    )
                    notify(subject, body)
                    log.critical("Exiting after %d consecutive errors.", MAX_ERRORS)
                    browser.close()
                    sys.exit(3)

            if single_check:
                log.info("--check mode: exiting after one check.")
                browser.close()
                return

            log.info("Sleeping %d min...", interval_min)
            time.sleep(interval_min * 60)


def _handle_session_expiry() -> None:
    log.warning("Session expired — redirected to login page.")
    subject = "TLS Monitor: Session Expired — Re-Login Required"
    body = (
        "Your TLS visa appointment monitor session has expired.\n\n"
        "Please re-run the login flow to refresh your session:\n\n"
        "  python monitor.py --login\n\n"
        f"Then restart the monitor:\n\n"
        f"  python monitor.py --interval 5\n\n"
        f"Booking URL: {BOOKING_URL}"
    )
    notify(subject, body)
    log.error("Re-run  python monitor.py --login  to refresh the session, then restart.")


def _handle_slots_found(slots: list[str]) -> None:
    subject = "TLS ALERT: France Visa Appointment Slots Available!"
    detail = "\n".join(slots[:30]) or "(see booking URL)"
    body = (
        f"Appointment slots have opened!\n\n"
        f"Booking URL: {BOOKING_URL}\n\n"
        f"Detected data:\n{detail}\n\n"
        f"Book ASAP — slots fill quickly."
    )
    notify(subject, body)
    log.info("SLOTS FOUND — notification sent!")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="TLS France visa appointment slot monitor (London VAC)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open visible browser for manual login + API endpoint discovery",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        metavar="MIN",
        help="Poll interval in minutes (default: 5)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run a single check then exit (useful for testing)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)

    if args.login:
        run_login_mode()
    else:
        run_poll_mode(interval_min=args.interval, single_check=args.check)


if __name__ == "__main__":
    main()

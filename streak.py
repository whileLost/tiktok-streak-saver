#!/usr/bin/env python3
"""headless tiktok streak daemon with timestamp tracking"""

import argparse
import http.server
import json
import logging
import platform
import random
import re
import signal
import socketserver
import sys
import threading
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from playwright.sync_api import sync_playwright, BrowserContext, Page, Response, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config.example.json"
PROFILE_DIR = BASE_DIR / "browser_profile"
HISTORY_PATH = BASE_DIR / "streak_history.json"
SESSION_EXPORT_PATH = BASE_DIR / "tiktok_session.json"
ASSETS_DIR = BASE_DIR / "assets"
LOGO_PATH = ASSETS_DIR / "logo.png"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "streak.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("tiktok-streak-saver")

SELECTORS = {
    "profile_message_btn": [
        'button[data-e2e="user-message-btn"]',
        'button:has-text("Message")',
        'a:has-text("Message")',
        'div[data-e2e="user-message-btn"]',
        '[aria-label="Message"]'
    ],
    "message_input": [
        'div[contenteditable="true"]',
        'div[role="textbox"]',
        'div[data-e2e="chat-input"]',
        'div[data-e2e="message-input"]',
        '.public-DraftEditor-content',
        'textarea[data-e2e="chat-input"]',
        'textarea'
    ],
    "send_btn": [
        'button[data-e2e="chat-send-btn"]',
        'button[data-e2e="send-button"]',
        'button[aria-label="Send"]',
        'button:has(svg[data-icon="send"])',
        'button:has(svg[fill*="currentColor"])'
    ],
    "popup_close_btn": [
        'button[aria-label="Close"]',
        'div[data-e2e="modal-close-inner-button"]',
        'button:has-text("Not now")',
        'button:has-text("Cancel")',
        'button:has-text("Decline")',
        '[aria-label="close"]'
    ],
    "chat_messages": [
        '[data-e2e="chat-message"]',
        'div[class*="ChatMessage"]',
        'div[class*="MessageContainer"]',
        'div[class*="DivMessageBubble"]'
    ],
    "time_indicators": [
        'div[class*="TimeDivider"]',
        'div[class*="MessageTime"]',
        'span[class*="Time"]',
        'div[class*="Time"]',
        'div[class*="time"]',
        'span[class*="Time"]',
        'time'
    ]
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)


class TimestampParser:
    @staticmethod
    def parse_relative_time(text: str) -> Optional[datetime]:
        if not text:
            return None
        raw = text.strip()
        now = datetime.now()

        # check absolute date formats
        for pattern, fmt in [
            (r'[A-Za-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2}', "%B %d, %Y %H:%M"),
            (r'[A-Za-z]{3} \d{1,2}, \d{4} \d{1,2}:\d{2}', "%b %d, %Y %H:%M"),
            (r'\d{1,2}/\d{1,2}/\d{4}', "%m/%d/%Y"),
        ]:
            match = re.search(pattern, raw)
            if match:
                try:
                    return datetime.strptime(match.group(0), fmt)
                except Exception:
                    pass

        raw_lower = raw.lower()
        if raw_lower in ("just now", "now") or "sec" in raw_lower:
            return now

        m_match = re.search(r'(\d+)\s*m(?:in)?(?:\s*ago)?\b', raw_lower)
        if m_match:
            return now - timedelta(minutes=int(m_match.group(1)))

        h_match = re.search(r'(\d+)\s*h(?:our)?s?(?:\s*ago)?\b', raw_lower)
        if h_match:
            return now - timedelta(hours=int(h_match.group(1)))

        if "yesterday" in raw_lower:
            t_match = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)?', raw_lower)
            if t_match:
                hr, mn = int(t_match.group(1)), int(t_match.group(2))
                if t_match.group(3) == "pm" and hr < 12:
                    hr += 12
                elif t_match.group(3) == "am" and hr == 12:
                    hr = 0
                return (now - timedelta(days=1)).replace(hour=hr, minute=mn, second=0)
            return now - timedelta(days=1)

        d_match = re.search(r'(\d+)\s*d(?:ay)?s?(?:\s*ago)?\b', raw_lower)
        if d_match:
            return now - timedelta(days=int(d_match.group(1)))

        t_today = re.search(r'\b(\d{1,2}):(\d{2})\s*(am|pm)?\b', raw_lower)
        if t_today:
            hr, mn = int(t_today.group(1)), int(t_today.group(2))
            if t_today.group(3) == "pm" and hr < 12:
                hr += 12
            elif t_today.group(3) == "am" and hr == 12:
                hr = 0
            msg_dt = now.replace(hour=hr, minute=mn, second=0)
            return msg_dt - timedelta(days=1) if msg_dt > now else msg_dt

        return None

    @staticmethod
    def parse_epoch(val: Any) -> Optional[datetime]:
        if not val:
            return None
        try:
            num = float(val)
            if num > 1e11:
                num /= 1000.0
            return datetime.fromtimestamp(num)
        except Exception:
            return None

    @classmethod
    def get_elapsed_hours(cls, dt: Optional[datetime]) -> Optional[float]:
        if not dt:
            return None
        return max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)


def load_config() -> dict:
    defaults = {
        "recipients": [],
        "messages": ["🔥 streak", "streak 🔥", "🔥"],
        "randomize_message": True,
        "headless": True,
        "browser_channel": "auto",
        "delay_between_users_min": 4,
        "delay_between_users_max": 8,
        "typing_delay_ms": 50,
        "screenshot_on_error": True,
        "daemon_mode": False,
        "daemon_interval_minutes": 30,
        "streak_trigger_elapsed_hours": 18.0,
        "streak_critical_elapsed_hours": 22.0,
        "alert_recipient_expiring": False,
        "recipient_critical_elapsed_hours": 21.0,
        "recipient_reminder_messages": [
            "⚠️ your streak is expiring soon! send a message to keep it alive 🔥",
            "streak expiring soon, send a quick message! ⏰🔥"
        ],
        "skip_if_already_sent_today": False,
        "webhook_url": ""
    }
    cfg_file = CONFIG_PATH if CONFIG_PATH.exists() else (CONFIG_EXAMPLE_PATH if CONFIG_EXAMPLE_PATH.exists() else None)
    if cfg_file:
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                defaults.update(json.load(f))
        except Exception as e:
            logger.warning("could not read %s: %s, using defaults", cfg_file, e)
    return defaults


def load_history() -> dict:
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_history(history: dict) -> None:
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.warning("failed to save history: %s", e)


def record_streak_sent(username: str, message: str, history: dict, config: Optional[dict] = None) -> int:
    clean_user = username.strip().lstrip("@").lower()
    today_str = date.today().isoformat()
    now_str = datetime.now().isoformat()

    prev_entry = history.get(clean_user, {})
    prev_date_str = prev_entry.get("last_date")
    prev_streak = prev_entry.get("streak_days", 0)

    # fallback to initial_streak_days in config if not tracked yet
    if prev_streak == 0 and config:
        init_map = config.get("initial_streak_days", {})
        prev_streak = int(init_map.get(clean_user, init_map.get(username, 0)))

    streak_count = max(prev_streak, 1)
    if prev_date_str:
        try:
            prev_date = date.fromisoformat(prev_date_str)
            if prev_date == date.today() - timedelta(days=1):
                streak_count = prev_streak + 1
            elif prev_date == date.today():
                streak_count = max(prev_streak, 1)
        except Exception:
            pass

    history[clean_user] = {
        **prev_entry,
        "username": username,
        "last_date": today_str,
        "last_timestamp": now_str,
        "last_message": message,
        "streak_days": streak_count
    }
    save_history(history)
    return streak_count


class DiscordNotifier:
    COLOR_GREEN = 0x57F287
    COLOR_ORANGE = 0xE67E22
    COLOR_RED = 0xED4245
    COLOR_BLURPLE = 0x5865F2
    COLOR_GOLD = 0xFEE75C

    DEFAULT_AVATAR_URL = "https://raw.githubusercontent.com/whileLost/tiktok-streak-saver/main/assets/logo.png"
    USERNAME = "tiktok streak saver"
    WIDTH_SPACER = "\u2800" * 38

    def __init__(self, config: dict):
        discord_cfg = config.get("discord", {})
        if isinstance(discord_cfg, str):
            self.webhook_url = discord_cfg.strip()
            self.avatar_url = self.DEFAULT_AVATAR_URL
            self.notify_on_send = True
            self.notify_on_warning = True
            self.notify_on_error = True
            self.notify_on_check = False
        else:
            self.webhook_url = discord_cfg.get("webhook_url", config.get("webhook_url", "")).strip()
            self.avatar_url = discord_cfg.get("avatar_url", self.DEFAULT_AVATAR_URL).strip() or self.DEFAULT_AVATAR_URL
            self.notify_on_send = discord_cfg.get("notify_on_send", True)
            self.notify_on_warning = discord_cfg.get("notify_on_warning", True)
            self.notify_on_error = discord_cfg.get("notify_on_error", True)
            self.notify_on_check = discord_cfg.get("notify_on_check", False)

        self._sync_webhook_profile()

    def _sync_webhook_profile(self) -> None:
        if not self.webhook_url or not self.webhook_url.startswith("http") or not LOGO_PATH.exists():
            return
        try:
            import base64
            b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
            patch_payload = json.dumps({
                "name": self.USERNAME,
                "avatar": f"data:image/png;base64,{b64}"
            }).encode("utf-8")
            patch_req = urllib.request.Request(
                self.webhook_url,
                data=patch_payload,
                headers={"Content-Type": "application/json", "User-Agent": "tiktok-streak-saver/1.0"},
                method="PATCH"
            )
            with urllib.request.urlopen(patch_req, timeout=6):
                pass
        except Exception as e:
            logger.debug("could not sync webhook profile: %s", e)

    def _post(self, embed: dict) -> bool:
        if not self.webhook_url or not self.webhook_url.startswith("http"):
            return False

        icon_ref = self.avatar_url or self.DEFAULT_AVATAR_URL
        if LOGO_PATH.exists() and "?" not in icon_ref:
            icon_ref = f"{icon_ref}?v={int(LOGO_PATH.stat().st_mtime)}"
        if "thumbnail" not in embed:
            embed["thumbnail"] = {"url": icon_ref}
        if "footer" not in embed:
            embed["footer"] = {"text": "tiktok streak saver"}

        payload = {
            "username": self.USERNAME,
            "avatar_url": icon_ref,
            "embeds": [embed]
        }

        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "tiktok-streak-saver/1.0"}
            )
            with urllib.request.urlopen(req, timeout=6):
                return True
        except Exception as e:
            logger.debug("discord webhook failed: %s", e)
            return False

    def notify_streak_sent(
        self,
        username: str,
        message: str,
        streak_days: int,
        my_elapsed_hours: Optional[float] = None,
        their_elapsed_hours: Optional[float] = None
    ) -> None:
        if not self.notify_on_send:
            return
        clean = username.strip().lstrip("@")
        my_text = f"`{my_elapsed_hours:.1f}h` elapsed" if my_elapsed_hours is not None else "`manual`"
        their_text = f"`{their_elapsed_hours:.1f}h` ago" if their_elapsed_hours is not None else "`unknown`"
        fields = [
            {"name": "recipient", "value": f"[{clean}](https://www.tiktok.com/@{clean})", "inline": True},
            {"name": "streak", "value": f"🔥 `{streak_days}` days", "inline": True},
            {"name": "message", "value": f"`{message}`", "inline": True},
            {"name": "your timer", "value": my_text, "inline": True},
            {"name": "their last message", "value": their_text, "inline": True},
            {"name": "status", "value": "🟢 reset to 24h", "inline": True}
        ]

        embed = {
            "title": f"🔥 streak extended: {clean}",
            "url": f"https://www.tiktok.com/@{clean}",
            "color": self.COLOR_ORANGE,
            "description": f"streak message delivered successfully\n{self.WIDTH_SPACER}",
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "tiktok streak saver"}
        }
        self._post(embed)

    def notify_streak_safe(
        self,
        username: str,
        my_elapsed_hours: float,
        wait_hours: float,
        their_elapsed_hours: Optional[float] = None,
        interval_mins: int = 30
    ) -> None:
        if not self.notify_on_check:
            return
        clean = username.strip().lstrip("@")
        their_text = f"`{their_elapsed_hours:.1f}h` ago" if their_elapsed_hours is not None else "`unknown`"
        embed = {
            "title": f"⏱️ streak safe: {clean}",
            "url": f"https://www.tiktok.com/@{clean}",
            "color": self.COLOR_BLURPLE,
            "description": f"current streak window is within safe limits\n{self.WIDTH_SPACER}",
            "fields": [
                {"name": "recipient", "value": f"[{clean}](https://www.tiktok.com/@{clean})", "inline": True},
                {"name": "your last message", "value": f"`{my_elapsed_hours:.1f}h` ago", "inline": True},
                {"name": "their last message", "value": their_text, "inline": True},
                {"name": "safe window", "value": f"~`{max(0.0, 24.0 - my_elapsed_hours):.1f}h` left", "inline": True},
                {"name": "wait time", "value": f"`{wait_hours:.1f}h` remaining", "inline": True},
                {"name": "status", "value": "🟢 safe", "inline": True}
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "tiktok streak saver"}
        }
        self._post(embed)

    def notify_expiring_warning(
        self,
        username: str,
        my_elapsed_hours: float,
        their_elapsed_hours: Optional[float] = None
    ) -> None:
        if not self.notify_on_warning:
            return
        clean = username.strip().lstrip("@")
        their_text = f"`{their_elapsed_hours:.1f}h` ago" if their_elapsed_hours is not None else "`unknown`"
        embed = {
            "title": f"⚠️ streak expiring soon: {clean}",
            "url": f"https://www.tiktok.com/@{clean}",
            "color": self.COLOR_RED,
            "description": f"streak window nearing expiration\n{self.WIDTH_SPACER}",
            "fields": [
                {"name": "recipient", "value": f"[{clean}](https://www.tiktok.com/@{clean})", "inline": True},
                {"name": "your last message", "value": f"🚨 `{my_elapsed_hours:.1f}h` / 24h", "inline": True},
                {"name": "their last message", "value": their_text, "inline": True},
                {"name": "safe window", "value": f"`{max(0.0, 24.0 - my_elapsed_hours):.1f}h` left", "inline": True},
                {"name": "action", "value": "`sending now`", "inline": True},
                {"name": "urgency", "value": "🔴 critical", "inline": True}
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "tiktok streak saver"}
        }
        self._post(embed)

    def notify_recipient_alert(
        self,
        username: str,
        message: str,
        their_elapsed_hours: float,
        my_elapsed_hours: Optional[float] = None
    ) -> None:
        if not self.notify_on_warning and not self.notify_on_send:
            return
        clean = username.strip().lstrip("@")
        my_text = f"`{my_elapsed_hours:.1f}h` ago" if my_elapsed_hours is not None else "`unknown`"
        embed = {
            "title": f"⚠️ recipient streak expiring alert: {clean}",
            "url": f"https://www.tiktok.com/@{clean}",
            "color": self.COLOR_ORANGE,
            "description": f"recipient has not sent a message in `{their_elapsed_hours:.1f}h`; sent reminder alert to preserve streak\n{self.WIDTH_SPACER}",
            "fields": [
                {"name": "recipient", "value": f"[{clean}](https://www.tiktok.com/@{clean})", "inline": True},
                {"name": "their last message", "value": f"🚨 `{their_elapsed_hours:.1f}h` / 24h", "inline": True},
                {"name": "their time left", "value": f"~`{max(0.0, 24.0 - their_elapsed_hours):.1f}h` left", "inline": True},
                {"name": "your last message", "value": my_text, "inline": True},
                {"name": "alert message", "value": f"`{message}`", "inline": True},
                {"name": "status", "value": "🟠 reminder sent", "inline": True}
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "tiktok streak saver"}
        }
        self._post(embed)

    def notify_error(self, title: str, error_msg: str, username: Optional[str] = None) -> None:
        if not self.notify_on_error:
            return
        fields = []
        if username:
            clean = username.strip().lstrip("@")
            fields.append({"name": "recipient", "value": f"[{clean}](https://www.tiktok.com/@{clean})", "inline": True},)
            fields.append({"name": "status", "value": "🔴 failed", "inline": True})
            fields.append({"name": "\u200b", "value": "\u200b", "inline": True})
        fields.append({"name": "error details", "value": f"```{error_msg[:1000]}```", "inline": False})
        embed = {
            "title": f"❌ {title.lower()}",
            "color": self.COLOR_RED,
            "description": f"an error occurred during execution\n{self.WIDTH_SPACER}",
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "tiktok streak saver"}
        }
        self._post(embed)

    def send_test(self) -> bool:
        embed = {
            "title": "🟢 tiktok streak saver connected",
            "color": self.COLOR_GREEN,
            "description": f"discord webhook integration is active\n{self.WIDTH_SPACER}",
            "fields": [
                {"name": "status", "value": "`operational`", "inline": True},
                {"name": "platform", "value": f"`{platform.system().lower()}`", "inline": True},
                {"name": "interval", "value": "`every 30m`", "inline": True}
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "tiktok streak saver"}
        }
        return self._post(embed)


def dismiss_popups(page: Page) -> None:
    for sel in SELECTORS["popup_close_btn"]:
        try:
            locator = page.locator(sel)
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=1000)
                page.wait_for_timeout(300)
        except Exception:
            pass


def resolve_browser_channel(configured_channel: str) -> Optional[str]:
    system_os = platform.system().lower()
    if system_os == "linux":
        return None
    if configured_channel == "auto":
        return "msedge" if system_os == "windows" else None
    return configured_channel if configured_channel in ["msedge", "chrome"] else None


def get_browser_context(playwright, headless: bool, browser_channel: str = "auto") -> BrowserContext:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-default-browser-check",
        "--no-first-run",
    ]
    channel_to_use = resolve_browser_channel(browser_channel)

    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel=channel_to_use,
            headless=headless,
            viewport={"width": 1280, "height": 800},
            user_agent=DEFAULT_USER_AGENT,
            args=launch_args,
            accept_downloads=False,
            java_script_enabled=True,
        )
    except Exception:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 800},
            user_agent=DEFAULT_USER_AGENT,
            args=launch_args,
        )

    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    return context


def check_is_logged_in(context: BrowserContext) -> bool:
    page = context.new_page()
    try:
        page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        if "login" in page.url.lower():
            logger.warning("session redirected to login, not authenticated")
            return False

        cookie_names = [c["name"] for c in context.cookies()]
        if "sessionid" in cookie_names or "sessionid_ss" in cookie_names or "messages" in page.url.lower():
            logger.info("session verified, user is authenticated")
            return True

        return False
    except Exception as e:
        logger.error("session check failed: %s", e)
        return False
    finally:
        page.close()


def export_session(output_path: Path = SESSION_EXPORT_PATH) -> bool:
    config = load_config()
    with sync_playwright() as p:
        context = get_browser_context(p, headless=True, browser_channel=config.get("browser_channel", "auto"))
        if not check_is_logged_in(context):
            logger.error("not logged in, run with --login or --set-cookie first")
            context.close()
            return False
        context.storage_state(path=str(output_path))
        context.close()
        logger.info("session exported to %s", output_path)
        return True


def import_session(input_path: Path) -> bool:
    if not input_path.exists():
        logger.error("session file not found: %s", input_path)
        return False
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            viewport={"width": 1280, "height": 800},
            user_agent=DEFAULT_USER_AGENT,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            cookies = state_data.get("cookies", [])
            if cookies:
                context.add_cookies(cookies)
                logger.info("imported %d cookies into profile", len(cookies))
        except Exception as e:
            logger.error("error reading %s: %s", input_path, e)
            context.close()
            return False

        valid = check_is_logged_in(context)
        context.close()
        return valid


def set_cookie_login(sessionid: str, ttwid: Optional[str] = None) -> bool:
    config = load_config()
    clean_sessionid = sessionid.strip()

    with sync_playwright() as p:
        context = get_browser_context(p, headless=True, browser_channel=config.get("browser_channel", "auto"))
        cookies = [
            {"name": "sessionid", "value": clean_sessionid, "domain": ".tiktok.com", "path": "/", "httpOnly": True, "secure": True},
            {"name": "sessionid_ss", "value": clean_sessionid, "domain": ".tiktok.com", "path": "/", "httpOnly": True, "secure": True}
        ]
        if ttwid:
            cookies.append({"name": "ttwid", "value": ttwid.strip(), "domain": ".tiktok.com", "path": "/", "httpOnly": True, "secure": True})

        context.add_cookies(cookies)
        valid = check_is_logged_in(context)
        if valid:
            context.storage_state(path=str(SESSION_EXPORT_PATH))
            logger.info("cookie authenticated successfully, session saved")
        else:
            logger.error("auth failed with provided sessionid")

        context.close()
        return valid


def run_remote_login_qr(port: int = 8080) -> None:
    config = load_config()
    qr_img_path = BASE_DIR / "login_qr.png"

    class QRHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ["/", "/index.html"]:
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                html = """
                <!DOCTYPE html><html><head><meta charset="utf-8"><title>login qr</title><meta http-equiv="refresh" content="5"></head>
                <body style="font-family:sans-serif;text-align:center;background:#111;color:#eee;padding:40px;">
                <h2>scan with tiktok app</h2>
                <img src="/login_qr.png" style="max-width:340px;border-radius:8px;margin:20px 0;" />
                <p>profile &rarr; menu &rarr; my qr code &rarr; scan</p>
                </body></html>
                """
                self.wfile.write(html.encode("utf-8"))
            elif self.path == "/login_qr.png" and qr_img_path.exists():
                self.send_response(200)
                self.send_header("Content-type", "image/png")
                self.end_headers()
                with open(qr_img_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)

        def log_message(self, format, *args):
            pass

    server = None
    try:
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("", port), QRHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info("qr server listening on port %d", port)
    except Exception as e:
        logger.warning("could not start qr server: %s", e)

    with sync_playwright() as p:
        context = get_browser_context(p, headless=True, browser_channel=config.get("browser_channel", "auto"))
        page = context.new_page()
        page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        dismiss_popups(page)

        try:
            qr_btn = page.locator('div:has-text("Use QR code"), a:has-text("Use QR code")')
            if qr_btn.count() > 0 and qr_btn.first.is_visible():
                qr_btn.first.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        page.screenshot(path=str(qr_img_path))
        logger.info("saved qr screenshot, waiting for scan...")

        max_wait, elapsed = 300, 0
        logged_in = False
        while elapsed < max_wait:
            time.sleep(2)
            elapsed += 2
            if elapsed % 20 == 0:
                try:
                    page.screenshot(path=str(qr_img_path))
                except Exception:
                    pass

            cookies = context.cookies()
            has_session = any(c["name"] in ["sessionid", "sessionid_ss"] for c in cookies)
            if has_session or ("login" not in page.url.lower() and ("tiktok.com/@" in page.url.lower() or "messages" in page.url.lower())):
                logged_in = True
                break

        if logged_in:
            logger.info("login confirmed")
            page.wait_for_timeout(2000)
            context.storage_state(path=str(SESSION_EXPORT_PATH))
        else:
            logger.warning("timed out waiting for login")

        context.close()

    if server:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


def run_login_flow(browser_channel: str = "auto") -> None:
    logger.info("starting interactive login...")
    with sync_playwright() as p:
        context = get_browser_context(p, headless=False, browser_channel=browser_channel)
        page = context.new_page()
        page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")

        max_wait, elapsed = 300, 0
        logged_in = False
        while elapsed < max_wait:
            time.sleep(2)
            elapsed += 2
            cookies = context.cookies()
            has_session = any(c["name"] in ["sessionid", "sessionid_ss"] for c in cookies)
            if has_session or ("login" not in page.url.lower() and ("tiktok.com/@" in page.url.lower() or "messages" in page.url.lower())):
                logged_in = True
                break

        if logged_in:
            logger.info("login successful, saving session state")
            page.wait_for_timeout(2500)
            context.storage_state(path=str(SESSION_EXPORT_PATH))
        else:
            logger.warning("interactive login timed out")

        context.close()


def pick_message(config: dict) -> str:
    messages = config.get("messages", ["🔥 streak"])
    if not messages:
        return "🔥 streak"
    return random.choice(messages) if config.get("randomize_message", True) else messages[0]


def pick_recipient_reminder_message(config: dict) -> str:
    messages = config.get("recipient_reminder_messages", [
        "⚠️ your streak is expiring soon! send a message to keep it alive 🔥",
        "streak expiring soon, send a quick message! ⏰🔥"
    ])
    if not messages:
        return "⚠️ your streak is expiring soon! send a message to keep it alive 🔥"
    return random.choice(messages) if config.get("randomize_message", True) else messages[0]


def find_element_from_selectors(page: Page, selectors: List[str], timeout: int = 5000):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout // len(selectors))
            return loc
        except Exception:
            continue
    return None


class ChatActivity:
    def __init__(
        self,
        last_sent_by_me: Optional[datetime] = None,
        last_received_from_them: Optional[datetime] = None,
        latest_overall: Optional[datetime] = None
    ):
        self.last_sent_by_me = last_sent_by_me
        self.last_received_from_them = last_received_from_them
        self.latest_overall = latest_overall


def detect_chat_activity(page: Page, network_captured_timestamps: Dict[str, datetime], username: str) -> ChatActivity:
    clean_user = username.strip().lstrip("@").lower()
    activity = ChatActivity()

    # strictly target active chat container
    chat_pane = page.locator('div[class*="DivChatMain"], div[class*="DivChatBox"], div[class*="ChatRoom"], div[class*="ChatContainer"]')
    if chat_pane.count() == 0:
        try:
            page.wait_for_selector('div[class*="DivChatMain"], div[class*="DivChatBox"]', timeout=4000)
            chat_pane = page.locator('div[class*="DivChatMain"], div[class*="DivChatBox"]')
        except Exception:
            pass

    if chat_pane.count() == 0:
        logger.warning("chat pane not found, cannot detect chat activity safely")
        return activity

    script = """
    () => {
        const chatMain = document.querySelector('div[class*="DivChatMain"]');
        if (!chatMain) return [];
        const mainBox = chatMain.getBoundingClientRect();
        const midX = mainBox.left + (mainBox.width * 0.45);

        const allNodes = Array.from(chatMain.querySelectorAll('div[class*="DivTimeContainer"], div[class*="DivChatItemWrapper"]'));
        let currentTimeStr = null;
        const messages = [];

        allNodes.forEach((node, idx) => {
            const cName = String(node.className || '');
            if (cName.includes('DivTimeContainer')) {
                const txt = node.innerText ? node.innerText.trim() : '';
                if (txt) {
                    currentTimeStr = txt;
                }
                return;
            }

            if (cName.includes('DivChatItemWrapper')) {
                // ignore pure system tips (e.g. "you deleted a message", streak pet changes)
                const isOnlyTip = !node.querySelector('div[data-e2e="dm-new-message-text"], div[class*="DivTextContainer"], div[class*="DivVideoContainer"], img[class*="Sticker"], div[class*="Sticker"], div[class*="DivMessageHoverContainer"]') && node.querySelector('p[class*="PChatTipContainer"]');
                if (isOnlyTip) {
                    return;
                }

                const bubble = node.querySelector('div[data-e2e="dm-new-message-text"], div[class*="DivTextContainer"], div[class*="DivVideoContainer"], img[class*="Sticker"], div[class*="Sticker"], div[class*="DivMessageHoverContainer"]');
                const bBox = bubble ? bubble.getBoundingClientRect() : node.getBoundingClientRect();
                const isOutgoing = (bBox.left > midX);

                const hasSticker = !!node.querySelector('img[class*="Sticker"], div[class*="Sticker"]');
                const text = node.innerText ? node.innerText.trim() : '';

                const tipEl = node.querySelector('span[class*="SpanInfoTime"]');
                const tipText = tipEl ? tipEl.innerText.trim() : null;

                messages.push({
                    idx,
                    isOutgoing,
                    hasSticker,
                    text,
                    tipText,
                    headerText: currentTimeStr
                });
            }
        });

        return messages;
    }
    """
    try:
        raw_messages = page.evaluate(script)
    except Exception as e:
        logger.warning("error evaluating chat bubbles: %s", e)
        raw_messages = []

    for msg in raw_messages:
        dt = None
        if msg.get("tipText"):
            dt = TimestampParser.parse_relative_time(msg["tipText"])
        if not dt and msg.get("headerText"):
            dt = TimestampParser.parse_relative_time(msg["headerText"])
        if not dt and msg.get("text"):
            first_line = msg["text"].split("\n")[0].strip()
            if len(first_line) < 40 and any(c.isdigit() for c in first_line):
                dt = TimestampParser.parse_relative_time(first_line)

        if dt:
            if msg.get("isOutgoing"):
                if activity.last_sent_by_me is None or dt > activity.last_sent_by_me:
                    activity.last_sent_by_me = dt
            else:
                if activity.last_received_from_them is None or dt > activity.last_received_from_them:
                    activity.last_received_from_them = dt

            if activity.latest_overall is None or dt > activity.latest_overall:
                activity.latest_overall = dt

    # fallback: network captured timestamp if available
    if clean_user in network_captured_timestamps:
        net_dt = network_captured_timestamps[clean_user]
        if activity.latest_overall is None or net_dt > activity.latest_overall:
            activity.latest_overall = net_dt
        if activity.last_received_from_them is None or net_dt > activity.last_received_from_them:
            activity.last_received_from_them = net_dt

    return activity


def setup_network_timestamp_interception(page: Page, captured_store: Dict[str, datetime]):
    def handle_response(response: Response):
        try:
            url = response.url.lower()
            if any(k in url for k in ["message/list", "conversation/list", "/v1/im/"]):
                data = response.json()
                items = data.get("messages", []) or data.get("message_list", []) or data.get("conversations", [])
                for item in items:
                    t_val = item.get("create_time") or item.get("created_at") or item.get("last_message_create_time")
                    dt = TimestampParser.parse_epoch(t_val)
                    if dt:
                        info = item.get("user_info", {}) or item.get("sender_info", {})
                        uname = info.get("unique_id") or info.get("nickname") or ""
                        if uname:
                            captured_store[uname.lower().lstrip("@")] = dt
        except Exception:
            pass

    page.on("response", handle_response)


def open_chat_with_user(page: Page, target_user: str) -> bool:
    clean_target = target_user.strip().lstrip("@")
    logger.info("opening chat for '%s'...", clean_target)

    try:
        page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=25000)
        dismiss_popups(page)

        # wait up to 8s for conversation items in sidebar to render
        try:
            page.wait_for_selector('[data-e2e="dm-new-conversation-item"], div[class*="DivItemWrapper"]', timeout=8000)
        except Exception:
            pass

        # check sidebar conversation items
        matched_item = None
        for _ in range(6):
            dismiss_popups(page)
            items = page.locator('[data-e2e="dm-new-conversation-item"], div[class*="DivItemWrapper"]')
            for i in range(items.count()):
                it = items.nth(i)
                nick_loc = it.locator('[data-e2e="dm-new-conversation-nickname"], span[class*="SpanNicknameText"]')
                nick_text = nick_loc.first.inner_text().strip() if nick_loc.count() > 0 else ""
                full_text = it.inner_text().strip()
                if clean_target.lower() in nick_text.lower() or clean_target.lower() in full_text.lower():
                    matched_item = it
                    break
            if matched_item:
                break
            page.wait_for_timeout(1000)

        if matched_item:
            matched_item.click(timeout=5000)
            page.wait_for_timeout(2500)
            dismiss_popups(page)
            if page.locator('div[class*="DivChatMain"], div[class*="DivChatBox"]').count() > 0:
                return True

        # fallback: grab display name from profile if not found directly
        profile_url = f"https://www.tiktok.com/@{clean_target}"
        page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)
        dismiss_popups(page)

        display_name = None
        try:
            h1 = page.locator('h1[data-e2e="user-title"], [data-e2e="user-title"]').first
            if h1.count() > 0:
                display_name = h1.inner_text().strip()
            if not display_name:
                h1_gen = page.locator('h1').first
                if h1_gen.count() > 0:
                    display_name = h1_gen.inner_text().strip()
        except Exception:
            pass

        # ignore system headers if accidentally picked
        if display_name and display_name.lower() in ["notifications", "messages", "following", "followers", "inbox"]:
            display_name = None

        page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=25000)
        dismiss_popups(page)

        if display_name:
            try:
                page.wait_for_selector('[data-e2e="dm-new-conversation-item"], div[class*="DivItemWrapper"]', timeout=8000)
            except Exception:
                pass
            items = page.locator('[data-e2e="dm-new-conversation-item"], div[class*="DivItemWrapper"]')
            for i in range(items.count()):
                it = items.nth(i)
                nick_loc = it.locator('[data-e2e="dm-new-conversation-nickname"], span[class*="SpanNicknameText"]')
                nick_text = nick_loc.first.inner_text().strip() if nick_loc.count() > 0 else ""
                full_text = it.inner_text().strip()
                if display_name.lower() in nick_text.lower() or display_name.lower() in full_text.lower():
                    it.click(timeout=5000)
                    page.wait_for_timeout(2500)
                    dismiss_popups(page)
                    if page.locator('div[class*="DivChatMain"], div[class*="DivChatBox"]').count() > 0:
                        return True

        logger.error("could not find conversation for '%s'", clean_target)
        return False
    except Exception as e:
        logger.error("error opening chat for '%s': %s", clean_target, e)
        return False


def type_and_send_message(page: Page, message: str, typing_delay_ms: int = 50, dry_run: bool = False) -> bool:
    input_elem = find_element_from_selectors(page, SELECTORS["message_input"], timeout=8000)
    if not input_elem:
        logger.error("message input box not found")
        return False

    input_elem.click()
    page.wait_for_timeout(400)
    page.keyboard.type(message, delay=typing_delay_ms)
    page.wait_for_timeout(500)

    if dry_run:
        logger.info("[dry-run] typed '%s', skipping submit", message)
        return True

    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)

    send_btn = find_element_from_selectors(page, SELECTORS["send_btn"], timeout=2000)
    if send_btn:
        try:
            send_btn.click(timeout=1500)
            page.wait_for_timeout(1000)
        except Exception:
            pass

    return True


def evaluate_and_process_user(
    page: Page,
    username: str,
    config: dict,
    history: dict,
    network_timestamps: Dict[str, datetime],
    notifier: DiscordNotifier,
    dry_run: bool = False,
    force: bool = False
) -> Tuple[bool, str]:
    clean_user = username.strip().lstrip("@")
    trigger_hours = float(config.get("streak_trigger_elapsed_hours", 18.0))
    critical_hours = float(config.get("streak_critical_elapsed_hours", 22.0))
    alert_recipient = bool(config.get("alert_recipient_expiring", False))
    recip_critical_hours = float(config.get("recipient_critical_elapsed_hours", 21.0))

    if not open_chat_with_user(page, username):
        return False, "failed to open chat"

    activity = detect_chat_activity(page, network_timestamps, username)

    # determine elapsed time for your message (sent by you)
    my_last_dt = activity.last_sent_by_me
    user_history = history.get(clean_user.lower(), {})
    if not my_last_dt and user_history.get("last_timestamp"):
        try:
            my_last_dt = datetime.fromisoformat(user_history["last_timestamp"])
        except Exception:
            pass

    my_elapsed = TimestampParser.get_elapsed_hours(my_last_dt)
    their_elapsed = TimestampParser.get_elapsed_hours(activity.last_received_from_them)

    log_my = f"{my_elapsed:.1f}h ago" if my_elapsed is not None else "never/unknown"
    log_their = f"{their_elapsed:.1f}h ago" if their_elapsed is not None else "unknown"
    logger.info("@%s: your last message was %s, their last message was %s", clean_user, log_my, log_their)

    # check if recipient's timer is about to expire and optional alert feature is enabled
    if alert_recipient and their_elapsed is not None and recip_critical_hours <= their_elapsed < 24.5:
        last_alert_str = user_history.get("last_recipient_alert_timestamp")
        can_alert = True
        if last_alert_str:
            try:
                last_alert_dt = datetime.fromisoformat(last_alert_str)
                hours_since_alert = (datetime.now() - last_alert_dt).total_seconds() / 3600.0
                if hours_since_alert < 12.0:
                    logger.info(
                        "@%s: recipient timer expiring (%.1fh ago), but alert was already sent %.1fh ago (cooldown active)",
                        clean_user, their_elapsed, hours_since_alert
                    )
                    can_alert = False
            except Exception:
                pass

        if can_alert:
            alert_msg = pick_recipient_reminder_message(config)
            logger.warning(
                "recipient streak timer expiring soon for @%s (their last: %.1fh ago), sending reminder alert: '%s'",
                clean_user, their_elapsed, alert_msg
            )
            ok = type_and_send_message(page, alert_msg, config.get("typing_delay_ms", 50), dry_run)
            if ok:
                if not dry_run:
                    count = record_streak_sent(username, alert_msg, history, config)
                    history[clean_user.lower()]["last_recipient_alert_timestamp"] = datetime.now().isoformat()
                    save_history(history)
                    logger.info("recipient alert sent successfully, streak days: %d", count)
                    notifier.notify_recipient_alert(username, alert_msg, their_elapsed, my_elapsed)
                return True, "recipient alert sent"
            return False, "failed to send recipient alert"

    if my_elapsed is not None:
        remaining_hours = max(0.0, 24.0 - my_elapsed)
        if not force and my_elapsed < trigger_hours:
            wait_hours = trigger_hours - my_elapsed
            logger.info("window not reached for @%s (your timer needs %.1fh more, streak safe)", clean_user, wait_hours)
            notifier.notify_streak_safe(username, my_elapsed, wait_hours, their_elapsed, int(config.get("daemon_interval_minutes", 30)))
            return True, f"window not reached (wait {wait_hours:.1f}h)"

        if my_elapsed >= critical_hours:
            logger.warning("your streak timer expiring soon for @%s (%.1fh elapsed)", clean_user, my_elapsed)
            notifier.notify_expiring_warning(username, my_elapsed, their_elapsed)
        else:
            logger.info("triggering streak message for @%s (%.1fh elapsed on your timer)", clean_user, my_elapsed)
    else:
        logger.info("no prior outgoing timestamp found for @%s, checking history", clean_user)
        if not force and user_history.get("last_date") == date.today().isoformat() and config.get("skip_if_already_sent_today", True):
            return True, "already sent today"

    msg = pick_message(config)
    logger.info("sending message to @%s: '%s'", clean_user, msg)

    ok = type_and_send_message(page, msg, config.get("typing_delay_ms", 50), dry_run)
    if ok:
        if not dry_run:
            count = record_streak_sent(username, msg, history, config)
            logger.info("sent successfully, streak days: %d", count)
            notifier.notify_streak_sent(username, msg, count, my_elapsed, their_elapsed)
        return True, "success"
    return False, "failed to send"


def run_single_check(args: argparse.Namespace) -> None:
    config = load_config()
    if getattr(args, "alert_recipient", None):
        config["alert_recipient_expiring"] = True
    history = load_history()
    notifier = DiscordNotifier(config)

    headless = not args.headful if args.headful is not None else config.get("headless", True)
    browser_channel = config.get("browser_channel", "auto")
    dry_run = args.dry_run
    force = args.force
    screenshot_on_error = config.get("screenshot_on_error", True)

    recipients = [args.user] if args.user else config.get("recipients", [])
    if not recipients:
        logger.error("no recipients specified in config.json or --user")
        sys.exit(1)

    logger.info("starting check for %d recipient(s) (force=%s, dry_run=%s)", len(recipients), force, dry_run)

    with sync_playwright() as p:
        context = get_browser_context(p, headless=headless, browser_channel=browser_channel)

        if not check_is_logged_in(context):
            err_msg = "session expired or missing. run with --set-cookie or --login."
            logger.error(err_msg)
            notifier.notify_error("session expired", err_msg)
            context.close()
            sys.exit(1)

        page = context.new_page()
        page.set_default_timeout(25000)

        network_timestamps: Dict[str, datetime] = {}
        setup_network_timestamp_interception(page, network_timestamps)

        successful_sends, waiting_users, failed_users = [], [], []

        try:
            for idx, user in enumerate(recipients, 1):
                logger.info("[%d/%d] checking %s", idx, len(recipients), user)
                ok, reason = evaluate_and_process_user(
                    page, user, config, history, network_timestamps, notifier, dry_run, force
                )

                if ok:
                    if "window not reached" in reason.lower() or "already sent" in reason.lower():
                        waiting_users.append(f"{user} ({reason})")
                    else:
                        successful_sends.append(user)
                else:
                    failed_users.append(f"{user} ({reason})")
                    notifier.notify_error("failed to send message", reason, user)
                    if screenshot_on_error:
                        try:
                            err_path = LOGS_DIR / f"error_{user}_{int(time.time())}.png"
                            page.screenshot(path=str(err_path))
                        except Exception:
                            pass

                if idx < len(recipients):
                    time.sleep(random.uniform(
                        config.get("delay_between_users_min", 4),
                        config.get("delay_between_users_max", 8)
                    ))
        finally:
            context.close()

    logger.info("check complete: %d sent, %d waiting, %d failed", len(successful_sends), len(waiting_users), len(failed_users))


def run_daemon_loop(args: argparse.Namespace) -> None:
    config = load_config()
    interval_mins = int(config.get("daemon_interval_minutes", 30))
    trigger_hours = float(config.get("streak_trigger_elapsed_hours", 18.0))
    logger.info("daemon started, polling every %d minutes (window: %.1f-24h)", interval_mins, trigger_hours)

    running = True

    def sig_handler(signum, frame):
        nonlocal running
        logger.info("signal %s received, stopping daemon", signum)
        running = False

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    while running:
        try:
            run_single_check(args)
        except SystemExit as se:
            if se.code != 0:
                logger.error("cycle exited with code %s, retrying next interval", se.code)
        except Exception as e:
            logger.error("daemon cycle error: %s", e, exc_info=True)

        if not running:
            break

        sleep_seconds = interval_mins * 60 + random.randint(-45, 45)
        logger.info("sleeping for %d minutes...", sleep_seconds // 60)

        slept = 0
        while slept < sleep_seconds and running:
            time.sleep(min(5, sleep_seconds - slept))
            slept += 5


def main():
    parser = argparse.ArgumentParser(description="headless tiktok streak daemon with timestamp tracking")
    parser.add_argument("--daemon", action="store_true", help="run continuous background daemon")
    parser.add_argument("--set-cookie", type=str, help="authenticate using sessionid cookie value")
    parser.add_argument("--ttwid", type=str, default=None, help="optional ttwid cookie value")
    parser.add_argument("--remote-login", action="store_true", help="generate qr code login on port")
    parser.add_argument("--port", type=int, default=8080, help="port for qr server (default: 8080)")
    parser.add_argument("--login", action="store_true", help="interactive login in visible browser")
    parser.add_argument("--export-session", action="store_true", help="export session to tiktok_session.json")
    parser.add_argument("--import-session", type=str, help="import session file")
    parser.add_argument("--status", action="store_true", help="check if session is valid")
    parser.add_argument("--user", type=str, help="target a specific recipient")
    parser.add_argument("--force", action="store_true", help="send message immediately ignoring timestamp")
    parser.add_argument("--dry-run", action="store_true", help="check without sending messages")
    parser.add_argument("--set-streak", type=str, help="manually set streak days for a user (e.g. --set-streak Francine:6)")
    parser.add_argument("--alert-recipient", action="store_true", default=None, help="enable alerting recipient when their streak timer is expiring")
    parser.add_argument("--test-discord", action="store_true", help="send a test notification to configured discord webhook")
    parser.add_argument("--headful", action="store_true", default=None, help="run browser with visible ui")

    args = parser.parse_args()
    config = load_config()

    if args.set_streak:
        if ":" not in args.set_streak:
            print("[error] format must be username:days, e.g. --set-streak Francine:6")
            sys.exit(1)
        u_target, days_str = args.set_streak.split(":", 1)
        clean_u = u_target.strip().lstrip("@").lower()
        try:
            days_val = int(days_str.strip())
        except ValueError:
            print("[error] streak days must be an integer")
            sys.exit(1)
        h = load_history()
        entry = h.get(clean_u, {"username": u_target.strip().lstrip("@")})
        entry["streak_days"] = days_val
        h[clean_u] = entry
        save_history(h)
        print(f"[ok] streak days for @{u_target.strip().lstrip('@')} set to {days_val}")
        sys.exit(0)
    elif args.test_discord:
        notifier = DiscordNotifier(config)
        ok = notifier.send_test()
        print("\n[ok] test discord notification sent!" if ok else "\n[error] failed to send discord webhook, check webhook_url")
        sys.exit(0 if ok else 1)
    elif args.set_cookie:
        ok = set_cookie_login(args.set_cookie, ttwid=args.ttwid)
        sys.exit(0 if ok else 1)
    elif args.remote_login:
        run_remote_login_qr(port=args.port)
    elif args.login:
        run_login_flow(browser_channel=config.get("browser_channel", "auto"))
    elif args.export_session:
        export_session()
    elif args.import_session:
        import_session(Path(args.import_session))
    elif args.status:
        with sync_playwright() as p:
            context = get_browser_context(p, headless=True, browser_channel=config.get("browser_channel", "auto"))
            valid = check_is_logged_in(context)
            context.close()
            print("\n[ok] session authenticated" if valid else "\n[error] session invalid or expired")
            sys.exit(0 if valid else 1)
    elif args.daemon or config.get("daemon_mode", False):
        run_daemon_loop(args)
    else:
        run_single_check(args)


if __name__ == "__main__":
    main()

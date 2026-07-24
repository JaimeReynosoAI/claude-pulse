#!/usr/bin/env python3
"""ClaudePulse — a system tray widget showing Claude Pro usage.

Reads the OAuth access token that Claude Code already maintains in
~/.claude/.credentials.json and calls Claude Code's internal usage-check
endpoint (the same one that powers `claude`'s /status command) to show
5-hour session and 7-day weekly utilization in a tray dropdown menu.

This endpoint (GET /api/oauth/usage) is undocumented and reverse-engineered
from Claude Code's bundled CLI — it may change or break without notice.
"""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_ID = "claude-pulse"
API_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"

# Falls back to this if `claude --version` can't be run (e.g. CLI not on
# PATH). Keeping a real version string here matters: this endpoint routes
# requests without a Claude-Code-like User-Agent into a much more
# aggressively rate-limited bucket.
FALLBACK_CLI_VERSION = "2.1.216"

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-pulse"
CACHE_PATH = CACHE_DIR / "usage.json"

CACHE_TTL_SECONDS = 180  # how long a successful fetch is considered fresh
MIN_RETRY_INTERVAL_SECONDS = 20  # floor between fetch *attempts*, success or not

WARNING_THRESHOLD = 80.0
CRITICAL_THRESHOLD = 95.0

ICON_NORMAL = "utilities-system-monitor-symbolic"
ICON_WARNING = "dialog-warning-symbolic"
ICON_CRITICAL = "dialog-error-symbolic"
ICON_AUTH_ERROR = "dialog-error-symbolic"


class CredentialsError(Exception):
    """Credentials are missing, unreadable, or expired — user action needed."""


class AuthError(Exception):
    """The API itself rejected the token (401)."""


class RateLimitedError(Exception):
    pass


class UsageFetchError(Exception):
    pass


def get_cli_version():
    try:
        result = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            match = re.match(r"(\S+)", result.stdout.strip())
            if match:
                return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return FALLBACK_CLI_VERSION


USER_AGENT = f"claude-cli/{get_cli_version()} (external, cli)"


def get_access_token():
    friendly = "Run `claude` once to refresh your session"
    if not CREDENTIALS_PATH.exists():
        raise CredentialsError(friendly)
    try:
        raw = json.loads(CREDENTIALS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        raise CredentialsError(friendly)

    oauth = raw.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires_at_ms = oauth.get("expiresAt")
    if not token:
        raise CredentialsError(friendly)
    if expires_at_ms and (expires_at_ms / 1000) < time.time():
        raise CredentialsError(friendly)
    return token


def fetch_usage(token):
    request = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AuthError("Run `claude` once to refresh your session")
        if e.code == 429:
            raise RateLimitedError("Rate limited by the Claude API")
        raise UsageFetchError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise UsageFetchError(str(e.reason))
    except (TimeoutError, json.JSONDecodeError) as e:
        raise UsageFetchError(str(e))

    five_hour = payload.get("five_hour") or {}
    seven_day = payload.get("seven_day") or {}
    return {
        "five_hour": {
            "utilization": five_hour.get("utilization"),
            "resets_at": five_hour.get("resets_at"),
        },
        "seven_day": {
            "utilization": seven_day.get("utilization"),
            "resets_at": seven_day.get("resets_at"),
        },
        "fetched_at": time.time(),
    }


def load_disk_cache():
    try:
        raw = json.loads(CACHE_PATH.read_text())
        if "five_hour" in raw and "seven_day" in raw and "fetched_at" in raw:
            return raw
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def save_disk_cache(data):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = CACHE_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data))
        tmp_path.chmod(0o600)
        tmp_path.replace(CACHE_PATH)
    except OSError:
        pass  # disk caching is a nice-to-have, never fatal


def fmt_pct(value):
    if value is None:
        return "?%"
    if float(value).is_integer():
        return f"{int(value)}%"
    return f"{value:.1f}%"


def fmt_reset(iso_str):
    if not iso_str:
        return "reset time unknown"
    try:
        target = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return "reset time unknown"
    delta = (target - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "resets any moment"

    days, rem = divmod(int(delta), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    if days > 0:
        return f"resets in {days}d {hours}h"
    if hours > 0:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


def fmt_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes}m ago"


class UsageStore:
    """In-memory usage state shared between the GTK main thread and
    background fetch threads. Disk cache holds only usage numbers and
    reset timestamps — never the access token."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = load_disk_cache()
        self.error = None
        self.error_kind = None  # "auth" | "rate_limited" | "network" | None
        self.fetching = False
        self.last_attempt_mono = 0.0

    def snapshot(self):
        with self.lock:
            return {
                "data": self.data,
                "error": self.error,
                "error_kind": self.error_kind,
                "fetching": self.fetching,
            }

    def _should_refresh(self, force):
        if self.fetching:
            return False
        now = time.monotonic()
        if now - self.last_attempt_mono < MIN_RETRY_INTERVAL_SECONDS:
            return False
        if force or self.data is None:
            return True
        return (time.time() - self.data["fetched_at"]) >= CACHE_TTL_SECONDS

    def start_refresh_if_needed(self, on_update, force=False):
        with self.lock:
            if not self._should_refresh(force):
                return
            self.fetching = True
            self.last_attempt_mono = time.monotonic()
        threading.Thread(target=self._do_refresh, args=(on_update,), daemon=True).start()

    def _do_refresh(self, on_update):
        try:
            token = get_access_token()
            result = fetch_usage(token)
            with self.lock:
                self.data = result
                self.error = None
                self.error_kind = None
            save_disk_cache(result)
        except CredentialsError as e:
            with self.lock:
                self.error = str(e)
                self.error_kind = "auth"
        except AuthError as e:
            with self.lock:
                self.error = str(e)
                self.error_kind = "auth"
        except RateLimitedError as e:
            with self.lock:
                self.error_kind = "rate_limited"
                self.error = (
                    "Rate limited — showing cached data" if self.data else str(e)
                )
        except UsageFetchError as e:
            with self.lock:
                self.error_kind = "network"
                self.error = (
                    f"Couldn't refresh (showing cached data): {e}"
                    if self.data
                    else f"Couldn't reach Claude API: {e}"
                )
        except Exception as e:
            # Belt-and-suspenders: an unanticipated exception here must never
            # leave `fetching` stuck True — that would permanently disable
            # "Refresh now" and freeze the display with no way to recover
            # short of restarting the app.
            with self.lock:
                self.error_kind = "network"
                self.error = f"Unexpected error: {e}"
        finally:
            with self.lock:
                self.fetching = False
            GLib.idle_add(on_update)


def choose_icon(snapshot):
    if snapshot["error_kind"] == "auth":
        return ICON_AUTH_ERROR
    data = snapshot["data"]
    if data is None:
        return ICON_NORMAL
    five = data["five_hour"]["utilization"] or 0
    seven = data["seven_day"]["utilization"] or 0
    peak = max(five, seven)
    if peak >= CRITICAL_THRESHOLD:
        return ICON_CRITICAL
    if peak >= WARNING_THRESHOLD:
        return ICON_WARNING
    return ICON_NORMAL


class TrayApp:
    """Builds the tray menu once with stable widget references and mutates
    their labels/sensitivity in place. AppIndicator menus are proxied to
    GNOME Shell over D-Bus (DBusMenu); the shell only calls back into the
    menu's "show" signal as a one-time layout probe, not on every real
    click, so content must be updated by mutating widget state directly —
    tearing down and rebuilding children on "show" leaves stale content
    and a permanently-disabled "Refresh now" frozen mid-fetch."""

    def __init__(self):
        self.store = UsageStore()
        self.menu = Gtk.Menu()

        self.status_item = self._label_item("Fetching usage…")
        self.week_item = self._label_item("")
        self.warning_item = self._label_item("")
        self.updated_item = self._label_item("")
        self.refresh_item = Gtk.MenuItem(label="Refresh now")
        self.refresh_item.connect("activate", self.on_refresh_clicked)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())

        for item in (self.status_item, self.week_item, self.warning_item, self.updated_item):
            self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(self.refresh_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(quit_item)
        self.menu.show_all()
        self.menu.connect("show", self.on_menu_show)

        self.indicator = AppIndicator3.Indicator.new(
            APP_ID, ICON_NORMAL, AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("ClaudePulse")
        self.indicator.set_menu(self.menu)

        self.apply_snapshot()
        self.store.start_refresh_if_needed(self.on_data_updated)

    def _label_item(self, text):
        # A display-only row. We deliberately avoid set_sensitive(False):
        # GTK renders insensitive items in the theme's dimmed/grey style,
        # which is what made these rows hard to read. Forcing the label
        # color via markup keeps them legible regardless of the system
        # theme. They stay unhooked from any "activate" handler, so
        # clicking one just dismisses the menu like clicking blank space.
        item = Gtk.MenuItem()
        label = Gtk.Label(xalign=0)
        item.add(label)
        self._set_item_text(item, text)
        return item

    def _set_item_text(self, item, text):
        item.get_child().set_markup(f'<span color="black">{GLib.markup_escape_text(text)}</span>')

    def apply_snapshot(self):
        snap = self.store.snapshot()

        if snap["error_kind"] == "auth":
            self._set_item_text(self.status_item, snap["error"])
            self.week_item.hide()
            self.warning_item.hide()
            self.updated_item.hide()
        elif snap["data"] is None:
            self._set_item_text(self.status_item, snap["error"] or "Fetching usage…")
            self.week_item.hide()
            self.warning_item.hide()
            self.updated_item.hide()
        else:
            five = snap["data"]["five_hour"]
            seven = snap["data"]["seven_day"]
            self._set_item_text(
                self.status_item,
                f"Session: {fmt_pct(five['utilization'])} used — {fmt_reset(five['resets_at'])}",
            )
            self._set_item_text(
                self.week_item,
                f"Week: {fmt_pct(seven['utilization'])} used — {fmt_reset(seven['resets_at'])}",
            )
            self.week_item.show()
            if snap["error_kind"] in ("rate_limited", "network"):
                self._set_item_text(self.warning_item, f"⚠ {snap['error']}")
                self.warning_item.show()
            else:
                self.warning_item.hide()
            age = time.time() - snap["data"]["fetched_at"]
            self._set_item_text(self.updated_item, f"Updated {fmt_age(age)}")
            self.updated_item.show()

        self.refresh_item.set_sensitive(not snap["fetching"])
        self.update_icon(snap)

    def update_icon(self, snap):
        self.indicator.set_icon_full(choose_icon(snap), "Claude Pulse")

    def on_menu_show(self, _menu):
        self.apply_snapshot()
        self.store.start_refresh_if_needed(self.on_data_updated)

    def on_refresh_clicked(self, *_args):
        self.store.start_refresh_if_needed(self.on_data_updated, force=True)
        self.apply_snapshot()

    def on_data_updated(self):
        self.apply_snapshot()
        return False  # GLib.idle_add: run once


def main():
    TrayApp()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        Gtk.main_quit()


if __name__ == "__main__":
    main()

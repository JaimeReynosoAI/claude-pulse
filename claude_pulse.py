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

import cairo
import json
import math
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

REFRESH_INTERVAL_SECONDS = 300  # both the auto-refresh timer cadence and cache freshness window
MIN_RETRY_INTERVAL_SECONDS = 20  # floor between fetch *attempts*, success or not

# Thresholds for the 5-hour session ring, in percent utilization.
GREEN_MAX = 60.0   # below this: green
YELLOW_MAX = 85.0  # below this: yellow, at/above: red

SEVERITY_RGB = {
    "ok": (0.086, 0.639, 0.290),        # #16a34a
    "warn": (0.851, 0.467, 0.024),      # #d97706
    "critical": (0.863, 0.149, 0.149),  # #dc2626
}
ICON_NORMAL = "utilities-system-monitor-symbolic"
ICON_AUTH_ERROR = "dialog-error-symbolic"

# Custom ring-chart icons are rendered to PNGs here and looked up by name via
# indicator.set_icon_theme_path(). We alternate between two file names on
# every update — AppIndicator/GNOME Shell caches icons by name and won't
# notice a file's *contents* changing under a name it has already seen.
ICON_DIR = CACHE_DIR / "icons"
ICON_NAMES = ("cp-usage-a", "cp-usage-b")
ICON_BACKDROP_RGBA = (0.11, 0.11, 0.13, 1.0)  # near-black disc for contrast on any panel color
ICON_TRACK_RGBA = (1.0, 1.0, 1.0, 0.16)  # faint full ring behind the usage arc
ICON_LABEL_RGBA = (1.0, 1.0, 1.0, 0.85)

# Two ring charts side by side (5-hour session, 7-day week), each preceded
# by a small bold label. Row height stays fixed; width grows to fit both.
ICON_ROW_HEIGHT = 64
ICON_RING_SIZE = 56
ICON_LABEL_FONT_SIZE = 30
ICON_LABEL_GAP = 6     # between a label and its ring
ICON_GROUP_GAP = 16    # between the 5h group and the 7d group
ICON_SIDE_PADDING = 1


def usage_severity(pct):
    if pct is None:
        return None
    if pct < GREEN_MAX:
        return "ok"
    if pct < YELLOW_MAX:
        return "warn"
    return "critical"


def _draw_ring(ctx, cx, cy, outer_radius, pct):
    ctx.set_source_rgba(*ICON_BACKDROP_RGBA)
    ctx.arc(cx, cy, outer_radius, 0, 2 * math.pi)
    ctx.fill()

    radius = outer_radius * 0.72
    ctx.set_line_width(outer_radius * 0.32)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)

    ctx.set_source_rgba(*ICON_TRACK_RGBA)
    ctx.arc(cx, cy, radius, 0, 2 * math.pi)
    ctx.stroke()

    severity = usage_severity(pct)
    if severity is not None and pct > 0:
        fraction = min(pct, 100.0) / 100.0
        start = -math.pi / 2
        end = start + fraction * 2 * math.pi
        ctx.set_source_rgb(*SEVERITY_RGB[severity])
        ctx.arc(cx, cy, radius, start, end)
        ctx.stroke()


def render_usage_icon(five_pct, seven_pct, path):
    """Draws two ring charts side by side — 5-hour session, then 7-day
    week — each with a small "5h"/"7d" label, and writes the result as a
    PNG. A pct of None (loading/unknown) is drawn as an empty track."""
    # A 1x1 probe surface just to measure label width via text_extents
    # before we know the final canvas size.
    probe_ctx = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    probe_ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    probe_ctx.set_font_size(ICON_LABEL_FONT_SIZE)
    label_width = max(probe_ctx.text_extents(t)[2] for t in ("5h", "7d"))

    group_width = label_width + ICON_LABEL_GAP + ICON_RING_SIZE
    width = ICON_SIDE_PADDING * 2 + group_width * 2 + ICON_GROUP_GAP
    height = ICON_ROW_HEIGHT

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), height)
    ctx = cairo.Context(surface)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(ICON_LABEL_FONT_SIZE)
    ascent, descent = ctx.font_extents()[:2]
    text_y = height / 2 + (ascent - descent) / 2

    x = ICON_SIDE_PADDING
    for label, pct in (("5h", five_pct), ("7d", seven_pct)):
        ctx.set_source_rgba(*ICON_LABEL_RGBA)
        ctx.move_to(x, text_y)
        ctx.show_text(label)
        x += label_width + ICON_LABEL_GAP

        _draw_ring(ctx, x + ICON_RING_SIZE / 2, height / 2, ICON_RING_SIZE / 2, pct)
        x += ICON_RING_SIZE + ICON_GROUP_GAP

    surface.write_to_png(str(path))


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
        return f"resets in: {days}d {hours}h"
    if hours > 0:
        return f"resets in: {hours}h {minutes}m"
    return f"resets in: {minutes}m"


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
        return (time.time() - self.data["fetched_at"]) >= REFRESH_INTERVAL_SECONDS

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

        ICON_DIR.mkdir(parents=True, exist_ok=True)
        self._icon_slot = 0

        self.indicator = AppIndicator3.Indicator.new(
            APP_ID, ICON_NORMAL, AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("ClaudePulse")
        self.indicator.set_icon_theme_path(str(ICON_DIR))
        self.indicator.set_menu(self.menu)

        self.apply_snapshot()
        self.store.start_refresh_if_needed(self.on_data_updated)
        GLib.timeout_add_seconds(REFRESH_INTERVAL_SECONDS, self.on_timer_tick)

    def _label_item(self, text):
        # A display-only row. We deliberately avoid set_sensitive(False):
        # GTK renders insensitive items in the theme's dimmed/grey style,
        # which is what made these rows hard to read. Leaving them sensitive
        # (but with no "activate" handler) keeps them legible under both
        # light and dark themes, since they just inherit the normal
        # foreground color instead of a hardcoded one. Clicking one just
        # dismisses the menu like clicking blank space.
        #
        # Plain text only, no markup: AppIndicator menus are proxied to
        # GNOME Shell over DBusMenu, whose GTK exporter reads item text with
        # gtk_label_get_text() — Pango markup (color, alpha, weight) is
        # silently dropped in transit, and per-item icons get forced into a
        # small fixed square. The shell owns this menu's rendering; custom
        # styling isn't available here, so we don't pretend otherwise.
        return Gtk.MenuItem(label=text)

    def apply_snapshot(self):
        snap = self.store.snapshot()

        if snap["error_kind"] == "auth":
            self.status_item.set_label(snap["error"])
            self.week_item.hide()
            self.warning_item.hide()
            self.updated_item.hide()
        elif snap["data"] is None:
            self.status_item.set_label(snap["error"] or "Fetching usage…")
            self.week_item.hide()
            self.warning_item.hide()
            self.updated_item.hide()
        else:
            five = snap["data"]["five_hour"]
            seven = snap["data"]["seven_day"]
            self.status_item.set_label(
                f"Session: {fmt_pct(five['utilization'])} used — {fmt_reset(five['resets_at'])}"
            )
            self.week_item.set_label(
                f"Week: {fmt_pct(seven['utilization'])} used — {fmt_reset(seven['resets_at'])}"
            )
            self.week_item.show()
            if snap["error_kind"] in ("rate_limited", "network"):
                self.warning_item.set_label(f"⚠ {snap['error']}")
                self.warning_item.show()
            else:
                self.warning_item.hide()
            age = time.time() - snap["data"]["fetched_at"]
            self.updated_item.set_label(f"Updated {fmt_age(age)}")
            self.updated_item.show()

        self.refresh_item.set_sensitive(not snap["fetching"])
        self.update_icon(snap)

    def update_icon(self, snap):
        if snap["error_kind"] == "auth":
            self.indicator.set_icon_full(ICON_AUTH_ERROR, "Claude Pulse — action needed")
            return
        data = snap["data"]
        five_pct = data["five_hour"]["utilization"] if data else None
        seven_pct = data["seven_day"]["utilization"] if data else None
        name = ICON_NAMES[self._icon_slot % 2]
        self._icon_slot += 1
        render_usage_icon(five_pct, seven_pct, ICON_DIR / f"{name}.png")
        self.indicator.set_icon_full(name, "Claude Pulse")

    def on_menu_show(self, _menu):
        # Just repaints from the current snapshot (e.g. to update the "Updated
        # Xm ago" text) — no network call. Fetching happens on the timer, at
        # startup, and via "Refresh now", not on every time the user looks.
        self.apply_snapshot()

    def on_refresh_clicked(self, *_args):
        self.store.start_refresh_if_needed(self.on_data_updated, force=True)
        self.apply_snapshot()

    def on_timer_tick(self):
        self.store.start_refresh_if_needed(self.on_data_updated)
        return True  # GLib.timeout_add_seconds: keep repeating

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

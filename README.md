# ClaudePulse

A small Linux system tray widget that shows your Claude Pro subscription
usage: the 5-hour rolling session window and the 7-day rolling weekly
window, each with utilization % and time until reset.

The tray icon itself is two small ring charts (5h, 7d) that fill up and
change color — green/amber/red — as usage climbs, so you get a signal
without opening anything. Click it for a dropdown with the same numbers as
plain text.

## How it works

Claude Code (the `claude` CLI) already keeps an OAuth session alive in
`~/.claude/.credentials.json` and uses an internal endpoint,
`GET https://api.anthropic.com/api/oauth/usage`, to power its own `/status`
command. ClaudePulse reads that same credentials file and calls that same
endpoint directly.

This endpoint is **undocumented and unofficial** — reverse-engineered from
Claude Code's bundled CLI binary. It may change or break without notice.
ClaudePulse never performs its own OAuth flow and never refreshes tokens
itself: it just reads whatever token Claude Code has already put in that
file. If the token is missing or expired, ClaudePulse shows:

> Run `claude` once to refresh your session

instead of guessing or showing stale data.

**Credentials are never written anywhere.** ClaudePulse only caches the
*usage numbers* (percentages + reset timestamps) to
`~/.cache/claude-pulse/usage.json`, so the popup has something to show
immediately on next launch before the first refresh completes. The access
token itself never touches disk, a log file, or git.

## Requirements

- Ubuntu with GNOME (tested on 24.04 / GNOME 46, Wayland)
- Python 3 with PyGObject (`python3-gi`) — already present on stock Ubuntu
- The AppIndicator GObject-introspection bindings, which are **not**
  installed by default on Ubuntu 24.04:

  ```
  sudo apt install gir1.2-ayatanaappindicator3-0.1
  ```

  (If you're on an older Ubuntu/GNOME that ships classic libappindicator
  instead of the Ayatana fork, `gir1.2-appindicator3-0.1` works too —
  `claude_pulse.py` tries the Ayatana namespace first and falls back
  automatically.)

- You'll also need the GNOME "AppIndicator and KStatusNotifierItem Support"
  shell extension enabled for the tray icon to show up at all — Ubuntu's
  GNOME flavor ships and enables this by default
  (`ubuntu-appindicators@ubuntu.com`).

## Running it

```
python3 claude_pulse.py
```

Log in with `claude` (or use it normally) at least once so
`~/.claude/.credentials.json` exists and has a valid token, then click the
tray icon.

## Autostart on login

```
./install.sh
```

This writes `~/.config/autostart/claude-pulse.desktop` pointing at this
checkout, so ClaudePulse launches automatically next time you log in. No
sudo required — it only touches your user config directory.

To remove it: delete `~/.config/autostart/claude-pulse.desktop`.

## Behavior notes

- **Auto-refreshes every 5 minutes.** ClaudePulse fetches once at startup,
  then on a background timer every `REFRESH_INTERVAL_SECONDS` (5 minutes by
  default), and whenever you click "Refresh now". Opening the tray dropdown
  does *not* trigger a fetch — it just repaints from whatever was last
  fetched, instantly.
- **Rate-limit friendly.** Fetch *attempts* (successful or not) are floored
  to at most one per `MIN_RETRY_INTERVAL_SECONDS` (20s), so rapid manual
  "Refresh now" clicks won't hammer the endpoint.
- **Degrades gracefully.** Network errors or a 429 fall back to showing the
  last cached numbers with a small warning note, rather than blanking the
  display.

## Limitations

- **The dropdown menu can't be restyled.** AppIndicator menus on Ubuntu
  GNOME are proxied to the shell over DBusMenu — the shell renders the
  popup itself, and its GTK exporter reads item text as plain text only
  (Pango markup, per-item CSS, and custom widgets are all dropped or
  squashed in transit; a wide custom icon gets forced into a small fixed
  square). Getting a real dark background or a real progress bar in the
  dropdown would mean dropping AppIndicator's menu entirely and hand-rolling
  a custom popup window with its own D-Bus StatusNotifierItem service — a
  much bigger rewrite. Since this is a personal tool, we chose plain text in
  the dropdown (matching every other tray indicator on the system) and put
  all the visual effort into the tray icon itself, where custom rendering
  (rings, color) genuinely works.

## Files

- `claude_pulse.py` — the whole app (GTK3 + AppIndicator3 tray icon, usage
  fetch/cache logic)
- `install.sh` — writes the autostart `.desktop` entry
- `.gitignore` — excludes the on-disk usage cache and anything
  credential/token-shaped

## Tuning

Refresh interval, retry floor, and the green/amber/red usage thresholds are
all plain constants near the top of `claude_pulse.py`
(`REFRESH_INTERVAL_SECONDS`, `MIN_RETRY_INTERVAL_SECONDS`, `GREEN_MAX`,
`YELLOW_MAX`).

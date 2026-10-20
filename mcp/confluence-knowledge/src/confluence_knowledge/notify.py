"""Desktop notifications for background crawl outcomes.

A crawl runs unattended for minutes — the agent chat only sees progress
when the user asks. A single OS notification on completion, failure, or
cancellation keeps the user aware without polling. Enabled by default;
`KNOWLEDGE_NOTIFY=0` opts out. Delivery is best-effort — a missing
notification helper must never surface as a crawl error.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)


def enabled() -> bool:
    return os.environ.get("KNOWLEDGE_NOTIFY", "1").lower() \
        not in ("0", "false", "off")


def _sh(cmd: list[str]) -> bool:
    try:
        return subprocess.run(
            cmd, check=False, timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode == 0
    except Exception as e:  # noqa: BLE001 — notification must never raise
        log.debug("notify %r failed: %s", cmd[:1], e)
        return False


def notify(title: str, body: str, app: str = "knowledge") -> bool:
    """Send one desktop notification. Returns False (never raises) when
    the platform helper is unavailable — agents should not treat that as
    an error."""
    if not enabled():
        return False
    try:
        if sys.platform == "darwin":
            script = (f'display notification {body!r} '
                      f'with title {title!r} subtitle {app!r} '
                      'sound name "Ping"')
            return _sh(["osascript", "-e", script])
        if sys.platform == "linux" and shutil.which("notify-send"):
            return _sh(["notify-send", "--app-name", app, title, body])
        if os.name == "nt" and shutil.which("powershell"):
            # Win10+ toast via WinRT — no module install needed
            ps = ("[Windows.UI.Notifications.ToastNotificationManager,"
                  " Windows.UI.Notifications, ContentType=WindowsRuntime]"
                  " | Out-Null;"
                  "$x='<toast><visual><binding template="
                  "\"ToastGeneric\"><text>" + title.replace("&", "&amp;")
                  .replace("<", "&lt;").replace(">", "&gt;") +
                  "</text><text>" + body.replace("&", "&amp;")
                  .replace("<", "&lt;").replace(">", "&gt;") +
                  "</text></binding></visual></toast>';"
                  "$d=New-Object Windows.Data.Xml.Dom.XmlDocument;"
                  "$d.LoadXml($x);"
                  "[Windows.UI.Notifications.ToastNotificationManager]"
                  "::CreateToastNotifier('" + app + "').Show("
                  "[Windows.UI.Notifications.ToastNotification]::new($d))")
            return _sh(["powershell", "-NoProfile", "-Command", ps])
    except Exception as e:  # noqa: BLE001 — belt-and-braces
        log.debug("notify failed: %s", e)
    return False

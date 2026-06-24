"""System notification wrapper with cooldown protection."""

from __future__ import annotations

import importlib
import logging
import time

from . import __app_name__

LOGGER = logging.getLogger(__name__)


class SystemNotifier:
    """Shows toast notifications without spamming the user."""

    def __init__(self, cooldown_seconds: float = 180.0) -> None:
        self.cooldown_seconds = max(30.0, cooldown_seconds)
        self._last_sent: dict[str, float] = {}
        self._win10_toaster = None

    def notify(self, title: str, message: str, key: str | None = None) -> bool:
        """Show a notification if its cooldown has elapsed."""

        dedupe_key = key or title
        now = time.monotonic()
        if now - self._last_sent.get(dedupe_key, 0.0) < self.cooldown_seconds:
            return False

        delivered = self._notify_with_plyer(title, message) or self._notify_with_win10toast(title, message)
        self._last_sent[dedupe_key] = now
        if delivered:
            LOGGER.info("Notification sent: %s - %s", title, message)
        else:
            LOGGER.info("Notification skipped because no backend is available: %s - %s", title, message)
        return delivered

    @staticmethod
    def _notify_with_plyer(title: str, message: str) -> bool:
        try:
            plyer_notification = importlib.import_module("plyer.notification")
            plyer_notification.notify(
                title=title,
                message=message,
                app_name=__app_name__,
                timeout=5,
            )
            return True
        except Exception:
            LOGGER.debug("plyer notification backend failed", exc_info=True)
            return False

    def _notify_with_win10toast(self, title: str, message: str) -> bool:
        try:
            if self._win10_toaster is None:
                win10toast = importlib.import_module("win10toast")
                self._win10_toaster = win10toast.ToastNotifier()
            self._win10_toaster.show_toast(title, message, duration=5, threaded=True)
            return True
        except Exception:
            LOGGER.debug("win10toast notification backend failed", exc_info=True)
            return False

"""Small runtime policies that are cheap to unit-test without Qt."""

from __future__ import annotations

from .config import AppConfig


def sleep_wake_poll_policy(config: AppConfig, *, sleeping_count: int, background: bool) -> tuple[bool, int]:
    """Return whether the wake poller should run and at what interval.

    The wake poller is only needed while sleep mode can act, or while there are
    already sleeping processes that must be restored when the user comes back.
    """

    if sleeping_count <= 0 and (config.observation_only_mode or not config.sleep_enabled):
        return False, 0

    if background:
        interval = 3000 if config.lite_mode_enabled else 1800
    else:
        interval = 1500 if config.lite_mode_enabled else 1000
    return True, interval

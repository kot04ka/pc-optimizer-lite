"""Reversible Windows visual-effects tuning for low-power mode."""

from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

SPIF_NONE = 0
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
SPIF_APPLY = SPIF_UPDATEINIFILE | SPIF_SENDCHANGE


@dataclass(frozen=True, slots=True)
class VisualEffectSetting:
    """One boolean SystemParametersInfo visual-effect flag."""

    name: str
    get_action: int
    set_action: int


VISUAL_EFFECT_SETTINGS = (
    VisualEffectSetting("ui_effects", 0x103E, 0x103F),
    VisualEffectSetting("client_area_animation", 0x1042, 0x1043),
    VisualEffectSetting("menu_animation", 0x1002, 0x1003),
    VisualEffectSetting("menu_fade", 0x1012, 0x1013),
    VisualEffectSetting("combo_box_animation", 0x1004, 0x1005),
    VisualEffectSetting("listbox_smooth_scrolling", 0x1006, 0x1007),
    VisualEffectSetting("gradient_captions", 0x1008, 0x1009),
    VisualEffectSetting("hot_tracking", 0x100E, 0x100F),
    VisualEffectSetting("selection_fade", 0x1014, 0x1015),
    VisualEffectSetting("tooltip_animation", 0x1016, 0x1017),
    VisualEffectSetting("tooltip_fade", 0x1018, 0x1019),
    VisualEffectSetting("cursor_shadow", 0x101A, 0x101B),
    VisualEffectSetting("drop_shadow", 0x1024, 0x1025),
)


class WindowsVisualEffectsAdapter:
    """Thin wrapper around SystemParametersInfoW."""

    @property
    def available(self) -> bool:
        return os.name == "nt" and hasattr(ctypes, "windll")

    def get_bool(self, setting: VisualEffectSetting) -> bool | None:
        if not self.available:
            return None
        value = ctypes.c_int()
        ok = ctypes.windll.user32.SystemParametersInfoW(  # type: ignore[attr-defined]
            setting.get_action,
            0,
            ctypes.byref(value),
            SPIF_NONE,
        )
        return bool(value.value) if ok else None

    def set_bool(self, setting: VisualEffectSetting, enabled: bool) -> bool:
        if not self.available:
            return False
        ok = ctypes.windll.user32.SystemParametersInfoW(  # type: ignore[attr-defined]
            setting.set_action,
            0,
            ctypes.c_void_p(1 if enabled else 0),
            SPIF_APPLY,
        )
        return bool(ok)


class VisualEffectsManager:
    """Apply and restore a small low-power set of Windows visual effects."""

    def __init__(self, adapter: WindowsVisualEffectsAdapter | None = None) -> None:
        self.adapter = adapter or WindowsVisualEffectsAdapter()
        self._original: dict[str, bool] = {}
        self._applied = False

    @property
    def active(self) -> bool:
        """Return True when low-power visual effects are currently applied."""

        return self._applied

    def apply_low_power(self) -> bool:
        """Disable selected animations while remembering original values."""

        if not self.adapter.available:
            return False
        if not self._original:
            self._original = self._capture_original()
        changed = False
        for setting in VISUAL_EFFECT_SETTINGS:
            if self.adapter.set_bool(setting, False):
                changed = True
        self._applied = changed
        if changed:
            LOGGER.info("Low-power visual effects applied")
        return changed

    def restore(self) -> bool:
        """Restore the visual-effect values captured before low-power mode."""

        if not self._original or not self.adapter.available:
            self._applied = False
            return False
        changed = False
        for setting in VISUAL_EFFECT_SETTINGS:
            original = self._original.get(setting.name)
            if original is not None and self.adapter.set_bool(setting, original):
                changed = True
        self._applied = False
        if changed:
            LOGGER.info("Visual effects restored")
        return changed

    def _capture_original(self) -> dict[str, bool]:
        captured: dict[str, bool] = {}
        for setting in VISUAL_EFFECT_SETTINGS:
            value = self.adapter.get_bool(setting)
            if value is not None:
                captured[setting.name] = value
        return captured

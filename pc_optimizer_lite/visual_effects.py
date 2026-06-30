"""Comprehensive Windows visual-effects manager: SPI + Registry, presets, restore points."""

from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

LOGGER = logging.getLogger(__name__)

SPIF_NONE = 0
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
SPIF_APPLY = SPIF_UPDATEINIFILE | SPIF_SENDCHANGE

_HKCU = 0x80000001
_REG_SZ = 1
_REG_DWORD = 4

_DESKTOP = r"Control Panel\Desktop"
_EXPLORER_ADV = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
_DWM = r"Software\Microsoft\Windows\DWM"
_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


class EffectSource(Enum):
    SPI = "spi"
    REGISTRY = "registry"


@dataclass(frozen=True)  # no slots — needed for @property
class VisualEffect:
    id: str
    label: str
    description: str
    source: EffectSource
    spi_get: int = 0
    spi_set: int = 0
    reg_hive: int = 0
    reg_path: str = ""
    reg_name: str = ""
    reg_on: Any = None
    reg_off: Any = None
    reg_type: int = _REG_DWORD

    # Backward-compat alias used by tests and old code
    @property
    def name(self) -> str:
        return self.id

    # Legacy SPI shim so old FakeAdapter tests still pass
    @property
    def get_action(self) -> int:
        return self.spi_get

    @property
    def set_action(self) -> int:
        return self.spi_set


EFFECTS: tuple[VisualEffect, ...] = (
    # ── Master toggle ──────────────────────────────────────────────────────
    VisualEffect(
        id="ui_effects",
        label="Все UI-эффекты (мастер-переключатель)",
        description="Глобальный переключатель всех системных UI-анимаций Windows.",
        source=EffectSource.SPI,
        spi_get=0x103E, spi_set=0x103F,
    ),
    # ── Window animations ──────────────────────────────────────────────────
    VisualEffect(
        id="window_animate",
        label="Анимация свёртывания/развёртывания окон",
        description="Плавный эффект при сворачивании и разворачивании окон.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_DESKTOP, reg_name="MinAnimate",
        reg_on="1", reg_off="0", reg_type=_REG_SZ,
    ),
    VisualEffect(
        id="client_area_animation",
        label="Анимации внутри окон приложений",
        description="Плавные переходы и анимации в рабочей области окон.",
        source=EffectSource.SPI,
        spi_get=0x1042, spi_set=0x1043,
    ),
    VisualEffect(
        id="drag_full_windows",
        label="Показывать содержимое при перетаскивании",
        description="Отображает содержимое окна при его перемещении. Отключение показывает только рамку.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_DESKTOP, reg_name="DragFullWindows",
        reg_on="1", reg_off="0", reg_type=_REG_SZ,
    ),
    VisualEffect(
        id="gradient_captions",
        label="Градиент заголовков окон",
        description="Плавный градиент в полосе заголовка окна.",
        source=EffectSource.SPI,
        spi_get=0x1008, spi_set=0x1009,
    ),
    # ── Taskbar & Explorer ─────────────────────────────────────────────────
    VisualEffect(
        id="taskbar_animations",
        label="Анимации панели задач",
        description="Эффекты при открытии, закрытии и переключении приложений на панели задач.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_EXPLORER_ADV, reg_name="TaskbarAnimations",
        reg_on=1, reg_off=0, reg_type=_REG_DWORD,
    ),
    VisualEffect(
        id="listview_alpha",
        label="Прозрачное выделение иконок",
        description="Полупрозрачный прямоугольник при выделении файлов на рабочем столе.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_EXPLORER_ADV, reg_name="ListviewAlphaSelect",
        reg_on=1, reg_off=0, reg_type=_REG_DWORD,
    ),
    VisualEffect(
        id="listview_shadow",
        label="Тени подписей иконок рабочего стола",
        description="Тень под названиями иконок на рабочем столе.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_EXPLORER_ADV, reg_name="ListviewShadow",
        reg_on=1, reg_off=0, reg_type=_REG_DWORD,
    ),
    VisualEffect(
        id="show_thumbnails",
        label="Эскизы папок и файлов",
        description="Миниатюры вместо иконок для изображений, видео и папок.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_EXPLORER_ADV, reg_name="IconsOnly",
        reg_on=0, reg_off=1, reg_type=_REG_DWORD,
    ),
    # ── Menu & tooltip animations ──────────────────────────────────────────
    VisualEffect(
        id="menu_animation",
        label="Анимация открытия меню",
        description="Меню плавно разворачивается или скользит вниз при открытии.",
        source=EffectSource.SPI,
        spi_get=0x1002, spi_set=0x1003,
    ),
    VisualEffect(
        id="menu_fade",
        label="Плавное исчезание меню",
        description="Меню плавно пропадает при закрытии.",
        source=EffectSource.SPI,
        spi_get=0x1012, spi_set=0x1013,
    ),
    VisualEffect(
        id="combo_box_animation",
        label="Анимация выпадающих списков",
        description="ComboBox-списки разворачиваются с плавной анимацией.",
        source=EffectSource.SPI,
        spi_get=0x1004, spi_set=0x1005,
    ),
    VisualEffect(
        id="listbox_smooth_scrolling",
        label="Плавная прокрутка списков",
        description="Прокрутка в списках и меню происходит плавно.",
        source=EffectSource.SPI,
        spi_get=0x1006, spi_set=0x1007,
    ),
    VisualEffect(
        id="tooltip_animation",
        label="Анимация появления подсказок",
        description="Всплывающие подсказки появляются с плавным эффектом.",
        source=EffectSource.SPI,
        spi_get=0x1016, spi_set=0x1017,
    ),
    VisualEffect(
        id="tooltip_fade",
        label="Плавное исчезание подсказок",
        description="Подсказки плавно исчезают после скрытия.",
        source=EffectSource.SPI,
        spi_get=0x1018, spi_set=0x1019,
    ),
    # ── Hover & selection ──────────────────────────────────────────────────
    VisualEffect(
        id="hot_tracking",
        label="Подсветка элементов при наведении",
        description="Кнопки и пункты меню подсвечиваются при наведении курсора.",
        source=EffectSource.SPI,
        spi_get=0x100E, spi_set=0x100F,
    ),
    VisualEffect(
        id="selection_fade",
        label="Плавное выделение пунктов меню",
        description="Выделение в меню появляется и исчезает плавно.",
        source=EffectSource.SPI,
        spi_get=0x1014, spi_set=0x1015,
    ),
    # ── Shadows ────────────────────────────────────────────────────────────
    VisualEffect(
        id="drop_shadow",
        label="Тени под меню и всплывающими окнами",
        description="Тени под выпадающими меню, тултипами и pop-up окнами.",
        source=EffectSource.SPI,
        spi_get=0x1024, spi_set=0x1025,
    ),
    VisualEffect(
        id="cursor_shadow",
        label="Тень курсора мыши",
        description="Небольшая тень под курсором мыши.",
        source=EffectSource.SPI,
        spi_get=0x101A, spi_set=0x101B,
    ),
    # ── DWM / Aero ─────────────────────────────────────────────────────────
    VisualEffect(
        id="aero_peek",
        label="Aero Peek (просмотр рабочего стола)",
        description="Показывает рабочий стол при наведении на кнопку в углу панели задач.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_DWM, reg_name="EnableAeroPeek",
        reg_on=1, reg_off=0, reg_type=_REG_DWORD,
    ),
    VisualEffect(
        id="transparency",
        label="Прозрачность меню Пуск и панели задач",
        description="Полупрозрачность элементов интерфейса Windows 10/11.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_PERSONALIZE, reg_name="EnableTransparency",
        reg_on=1, reg_off=0, reg_type=_REG_DWORD,
    ),
    # ── Font rendering ─────────────────────────────────────────────────────
    VisualEffect(
        id="font_smoothing",
        label="Сглаживание шрифтов (ClearType)",
        description="ClearType делает текст более читаемым на ЖК-экранах.",
        source=EffectSource.REGISTRY,
        reg_hive=_HKCU, reg_path=_DESKTOP, reg_name="FontSmoothing",
        reg_on="2", reg_off="0", reg_type=_REG_SZ,
    ),
)

_EFFECTS_BY_ID: dict[str, VisualEffect] = {e.id: e for e in EFFECTS}

PRESETS: dict[str, set[str]] = {
    "performance": {
        "ui_effects", "window_animate", "client_area_animation", "taskbar_animations",
        "menu_animation", "menu_fade", "combo_box_animation", "listbox_smooth_scrolling",
        "tooltip_animation", "tooltip_fade", "selection_fade", "listview_alpha",
        "gradient_captions", "aero_peek", "transparency", "show_thumbnails",
    },
    "balanced": {
        "window_animate", "client_area_animation", "taskbar_animations",
        "menu_animation", "menu_fade", "combo_box_animation",
        "tooltip_animation", "tooltip_fade", "selection_fade", "aero_peek", "transparency",
    },
    "appearance": set(),
}

PRESET_LABELS: dict[str, str] = {
    "performance": "Производительность",
    "balanced": "Баланс",
    "appearance": "Лучший вид",
}


# ── Low-level helpers ──────────────────────────────────────────────────────

def _spi_get(action: int) -> bool | None:
    try:
        value = ctypes.c_int()
        ok = ctypes.windll.user32.SystemParametersInfoW(action, 0, ctypes.byref(value), SPIF_NONE)  # type: ignore[attr-defined]
        return bool(value.value) if ok else None
    except Exception:
        return None


def _spi_set(action: int, enabled: bool) -> bool:
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(  # type: ignore[attr-defined]
            action, 0, ctypes.c_void_p(1 if enabled else 0), SPIF_APPLY
        )
        return bool(ok)
    except Exception:
        return False


def _reg_read(hive_id: int, path: str, name: str) -> Any | None:
    try:
        import winreg
        hive = winreg.HKEY_CURRENT_USER if hive_id == _HKCU else winreg.HKEY_LOCAL_MACHINE
        with winreg.OpenKey(hive, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except Exception:
        return None


def _reg_write(hive_id: int, path: str, name: str, value: Any, reg_type: int) -> bool:
    try:
        import winreg
        hive = winreg.HKEY_CURRENT_USER if hive_id == _HKCU else winreg.HKEY_LOCAL_MACHINE
        wtype = winreg.REG_DWORD if reg_type == _REG_DWORD else winreg.REG_SZ
        with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY) as key:
            winreg.SetValueEx(key, name, 0, wtype, value)
        return True
    except Exception:
        return False


# ── Manager ────────────────────────────────────────────────────────────────

class VisualEffectsManager:
    """Apply and restore Windows visual effects via SPI and registry.

    Accepts an optional *adapter* for testing (legacy pattern). When no adapter
    is provided, effects are applied directly via SPI / winreg.
    """

    def __init__(self, adapter: object | None = None) -> None:
        self._adapter = adapter  # legacy injection for unit tests
        self._restore_point: dict[str, Any] = {}
        self._applied = False

    @property
    def available(self) -> bool:
        if self._adapter is not None:
            return bool(getattr(self._adapter, "available", True))
        return os.name == "nt" and hasattr(ctypes, "windll")

    @property
    def active(self) -> bool:
        return self._applied

    def get_state(self, effect: VisualEffect) -> bool | None:
        if not self.available:
            return None
        if self._adapter is not None:
            raw = getattr(self._adapter, "get_bool", None)
            return raw(effect) if callable(raw) else None
        if effect.source == EffectSource.SPI:
            return _spi_get(effect.spi_get)
        raw_val = _reg_read(effect.reg_hive, effect.reg_path, effect.reg_name)
        if raw_val is None:
            return None
        return raw_val == effect.reg_on

    def get_states(self) -> dict[str, bool | None]:
        return {e.id: self.get_state(e) for e in EFFECTS}

    def set_effect(self, effect: VisualEffect, enabled: bool) -> bool:
        if not self.available:
            return False
        if self._adapter is not None:
            fn = getattr(self._adapter, "set_bool", None)
            return fn(effect, enabled) if callable(fn) else False
        if effect.source == EffectSource.SPI:
            return _spi_set(effect.spi_set, enabled)
        value = effect.reg_on if enabled else effect.reg_off
        return _reg_write(effect.reg_hive, effect.reg_path, effect.reg_name, value, effect.reg_type)

    def save_restore_point(self) -> None:
        if self._restore_point:
            return
        self._restore_point = {}
        for e in EFFECTS:
            state = self.get_state(e)
            if state is not None:
                self._restore_point[e.id] = state
        LOGGER.debug("Visual effects restore point saved (%d entries)", len(self._restore_point))

    def restore(self) -> bool:
        if not self._restore_point or not self.available:
            self._applied = False
            return False
        changed = False
        for eid, enabled in self._restore_point.items():
            effect = _EFFECTS_BY_ID.get(eid)
            if effect and self.set_effect(effect, enabled):
                changed = True
        self._restore_point = {}
        self._applied = False
        if changed:
            LOGGER.info("Visual effects restored")
        return changed

    def apply_disabled_set(self, disabled_ids: set[str]) -> int:
        if not self.available:
            return 0
        if not self._restore_point:
            self.save_restore_point()
        count = 0
        for e in EFFECTS:
            if self.set_effect(e, e.id not in disabled_ids):
                count += 1
        self._applied = bool(disabled_ids)
        if count:
            LOGGER.info("Applied visual effects: %d disabled", len(disabled_ids))
        return count

    def apply_preset(self, preset: str) -> int:
        return self.apply_disabled_set(PRESETS.get(preset, set()))

    def apply_low_power(self) -> bool:
        """Legacy entry-point: disable all performance-heavy effects."""
        if not self._restore_point:
            self.save_restore_point()
        changed = False
        for e in EFFECTS:
            if self.set_effect(e, False):
                changed = True
        self._applied = changed
        return changed


# Backward-compat aliases
VisualEffectSetting = VisualEffect
VISUAL_EFFECT_SETTINGS = EFFECTS
WindowsVisualEffectsAdapter = object

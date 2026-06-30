"""Small UI design model shared by tests and the PySide shell."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesignTokens:
    background: str
    surface: str
    surface_elevated: str
    surface_hover: str
    border: str
    text_primary: str
    text_secondary: str
    text_muted: str
    accent_blue: str
    accent_blue_hover: str
    success: str
    warning: str
    danger: str
    info: str


@dataclass(frozen=True)
class NavPage:
    page_id: str
    title: str
    subtitle: str
    icon: str
    topbar_title: str
    topbar_description: str


@dataclass(frozen=True)
class HealthStatus:
    severity: str
    title: str
    detail: str


@dataclass(frozen=True)
class SettingsLayoutPolicy:
    nav_width: int
    field_max_width: int
    min_content_width: int


SETTINGS_LAYOUT = SettingsLayoutPolicy(
    nav_width=164,
    field_max_width=340,
    min_content_width=0,
)

TOPBAR_ACTIONS_LABEL = "Действия"


PROMPT_DARK_TOKENS = DesignTokens(
    background="#0B1017",
    surface="#121A25",
    surface_elevated="#182230",
    surface_hover="#202C3B",
    border="#27364A",
    text_primary="#F3F7FC",
    text_secondary="#9EB0C7",
    text_muted="#6F8197",
    accent_blue="#5B9CFF",
    accent_blue_hover="#78AEFF",
    success="#31D0A1",
    warning="#F5B942",
    danger="#F26B6B",
    info="#67A8FF",
)

LIGHT_TOKENS = DesignTokens(
    background="#F5F7FB",
    surface="#FFFFFF",
    surface_elevated="#EEF2F7",
    surface_hover="#E7EDF6",
    border="#D7DDE6",
    text_primary="#111827",
    text_secondary="#667085",
    text_muted="#8A94A6",
    accent_blue="#2563EB",
    accent_blue_hover="#1D4ED8",
    success="#059669",
    warning="#D97706",
    danger="#E11D48",
    info="#2563EB",
)

DEFAULT_NAV_PAGES = (
    NavPage(
        page_id="overview",
        title="Обзор",
        subtitle="Главная панель",
        icon="home",
        topbar_title="Обзор системы",
        topbar_description="Краткая информация о состоянии вашего ПК в реальном времени.",
    ),
    NavPage(
        page_id="processes",
        title="Процессы",
        subtitle="Управление",
        icon="list",
        topbar_title="Процессы",
        topbar_description="Просмотр и управление запущенными приложениями.",
    ),
    NavPage(
        page_id="activity",
        title="Активность",
        subtitle="Журнал событий",
        icon="clock",
        topbar_title="Активность",
        topbar_description="Журнал действий программы и оптимизаций.",
    ),
    NavPage(
        page_id="exceptions",
        title="Исключения",
        subtitle="Белый список",
        icon="shield",
        topbar_title="Исключения",
        topbar_description="Процессы и пути, которые PC Optimizer Lite не трогает.",
    ),
    NavPage(
        page_id="settings",
        title="Настройки",
        subtitle="Параметры",
        icon="settings",
        topbar_title="Настройки",
        topbar_description="Настройте поведение программы под ваши предпочтения.",
    ),
)


def build_design_palette(theme: str) -> dict[str, str]:
    tokens = LIGHT_TOKENS if theme == "light" else PROMPT_DARK_TOKENS
    return {
        "bg": tokens.background,
        "panel": tokens.surface,
        "panel_2": tokens.surface_elevated,
        "panel_hover": tokens.surface_hover,
        "text": tokens.text_primary,
        "muted": tokens.text_secondary,
        "text_muted": tokens.text_muted,
        "border": tokens.border,
        "accent": tokens.accent_blue,
        "accent_hover": tokens.accent_blue_hover,
        "good": tokens.success,
        "warn": tokens.warning,
        "bad": tokens.danger,
        "info": tokens.info,
        "input": tokens.background,
        "row": tokens.surface,
        "row_alt": tokens.surface_elevated,
    }


def evaluate_system_health(
    *,
    cpu_percent: float,
    ram_percent: float,
    disk_percent: float,
    swap_percent: float,
) -> HealthStatus:
    pressure = max(cpu_percent, ram_percent, disk_percent, swap_percent)
    if cpu_percent >= 90.0 or ram_percent >= 90.0 or swap_percent >= 70.0:
        return HealthStatus(
            severity="bad",
            title="Нагрузка высокая",
            detail="Система под сильной нагрузкой. Запустите оптимизацию или проверьте тяжёлые процессы.",
        )
    if pressure >= 80.0 or swap_percent >= 35.0:
        return HealthStatus(
            severity="warn",
            title="Требуется внимание",
            detail="Есть заметная нагрузка. PC Optimizer Lite может мягко освободить ресурсы.",
        )
    return HealthStatus(
        severity="good",
        title="Система в порядке",
        detail="Критической нагрузки нет. Автопилот продолжит наблюдать в фоне.",
    )

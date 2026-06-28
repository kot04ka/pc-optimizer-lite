# PC Optimizer Lite 1.5.0

Focus: calmer Windows utility UI and safer low-load controls.

Changes:
- Reworked the PySide UI into a desktop app shell with a persistent sidebar, topbar, stacked pages, and scrollable page content.
- Added a system health card with live CPU/RAM/disk/pagefile pressure status, admin indicator, optimization progress, and Light/Deep optimization mode.
- Added process search, filters, sorting, PID-in-row display, foreground/background/excluded status labels, and long-path tooltips.
- Moved overview quick actions into the page content instead of a fixed bottom bar.
- Added a settings side menu that scrolls to real settings sections.
- Added confirmation before removing whitelist entries.
- Kept per-process disk I/O out of the process table to avoid adding monitoring overhead.

Safety notes:
- Business logic and optimization handlers are reused; the redesign does not add fake controls.
- Auto-close remains controlled by the existing settings.
- Windows Defender, antivirus settings, and system protection settings are not disabled.

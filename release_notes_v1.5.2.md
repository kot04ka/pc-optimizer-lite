# PC Optimizer Lite 1.5.2

Focus: ship the new visual refresh and the expanded Windows visual-effects manager as a public updater release.

Changes:
- Added the Midnight Purple visual polish for the PySide desktop shell, including sidebar, topbar, cards, and settings layout refinements.
- Expanded Windows visual-effects control from a small SPI-only toggle into a full SPI + HKCU registry manager.
- Added visual-effects presets for performance, balanced, and cautious modes, with restore points so original Windows values can be restored.
- Covered window/menu/taskbar animations, drag full windows, icon shadows, thumbnails, Aero Peek, transparency, ClearType, menu/tooltip fade, smooth scrolling, and related UI effects.
- Aligned low-end/autopilot behavior with visual-effects tuning so weaker PCs get a lighter profile without enabling automatic app closing.

Safety notes:
- The app stores a restore point before changing visual-effects flags and restores captured values when the option is disabled or the app exits.
- The app still does not disable Windows Defender, antivirus, or system protection settings.

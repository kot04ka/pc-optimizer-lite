# PC Optimizer Lite 1.4.0

Focus: safer autonomous low-power behavior for weak PCs.

Changes:
- Low-end optimal preset now enables Lite Mode, safer autopilot actions, slower background polling, and low-power visual effects while keeping auto-close disabled.
- Added reversible Windows visual-effects tuning: selected animations are reduced during low-power mode and restored on exit.
- Sleep wake polling no longer runs constantly. It starts only while sleep mode can act or sleeping apps need wake detection.
- Fixed CPU/priority candidate selection so snapshot-based decisions do not depend on the current foreground window by accident.
- Browser processes are no longer treated as media-active just because of their executable name; inactive visible browser windows can receive soft priority sleep, never deep suspend.
- Legacy tkinter fallback starts without continuous process-table polling.

Safety notes:
- Auto-close remains off in the optimal preset.
- No `kill()` behavior was added.
- Windows Defender, antivirus settings, and system protection settings are not disabled.

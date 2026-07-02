# PC Optimizer Lite 1.5.3

Focus: reduce background CPU pressure from Windows extras and the app itself.

Changes:
- Added a Windows background-load section for Widgets/News, Xbox Game Bar, Game DVR, and background game recording with restore points.
- Added high-load auto-pause for Windows Search indexing (`WSearch`) and Delivery Optimization (`DoSvc`) when CPU is high and the user is idle.
- Added Ultra-lite mode for the app: slower monitoring, slower process refresh, lighter graph behavior, and stricter low-end defaults.
- Low-end preset now enables Ultra-lite, the safe background-load preset, and Search/Delivery auto-pause.

Safety notes:
- Registry changes are per-user HKCU values and are restored from captured original values.
- Search and Delivery Optimization are not permanently disabled; they are only paused/stopped temporarily and resumed by the app.
- The app still does not disable Windows Defender, antivirus, Windows Update, or security protections.

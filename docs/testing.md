# Experimental tester guide

## Addon smoke test

1. Install only WoWContextBridge, removing the earlier CompanionPoC folder.
2. Enter Retail and verify the top-left strip.
3. Run `/wcb status`, `/wcb off` and `/wcb on`.
4. Change target and zone; enable friendly/enemy nameplates and check nearby counts.
5. Try Unicode names when available. Check for Lua errors after `/reload`.

## Compatible reader test

Keep only one capture session active. Start with a short supervised test, then 30 minutes of normal play if healthy. Check live status, heartbeat freshness, target/nameplate changes, a zone change and resolution/scaling changes. Disable output and verify stale context expires according to the reader's policy. Test game exit/relaunch and application disable/unload separately. Observe CPU, memory, game smoothness and Gamescope health; stop if problems occur.

For recognition comparisons, use the same recorded phrases with context off/on, including Spanish speech containing English names. Inspect transcriptions without sending chat. Record results before claiming improvement.

## Report template

- Addon and reader versions:
- Retail client version/interface:
- OS/display session and Proton version, if applicable:
- Resolution and UI scaling:
- Test duration:
- Live/stale/error status and vocabulary counts:
- CPU/memory measurements and noticeable performance impact:
- Reproduction steps and sanitized error text:

Do not submit character names, private paths, unreviewed screenshots or raw context by default. Automated test results are distinct from hardware observations. The renamed release needs fresh tester validation of installation and slash commands.

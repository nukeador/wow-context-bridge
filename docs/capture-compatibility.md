# Capture compatibility

The addon renders context; obtaining that image is the external application's responsibility. Capture and decoding are separate layers.

- Offline PNG decoding works without a game or display server.
- X11 capture is experimental and has not demonstrated reliable Gaming Mode access to the game output.
- GStreamer portal capture failed to resolve the returned Gamescope node in development tests.
- Native PipeWire capture through the ScreenCast portal FD successfully decoded live Retail context on Steam Deck in supervised testing. ScreenCast v5 requires the numeric returned node ID; newer portals may supply a serial selector.
- Early native capture experiments coincided with Gamescope buffer-destruction crashes. Use orderly teardown and CPU-readable MemFd buffers; successful short runs do not establish long-term stability.
- The integrated Decktation reader uses one persistent session and copies/decodes at most every five seconds. Source buffer delivery can be faster than this; it is not a five-second producer frame rate.
- Wine Wayland, additional hardware and broad resolution/scaling compatibility remain unverified.

The standalone tools remain research utilities. Prefer a compatible application release intended for testers. Never start another probe while an application's capture session is active. Do not run capture as root or install SteamOS packages to work around missing support. Stop testing on stutter, errors or crashes.

No benchmark or systematic recognition improvement is claimed. Supervised reports include successful context decoding and Spanish dictation containing English names; controlled off/on recognition comparisons and long-duration CPU/memory measurements are still needed.

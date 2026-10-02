# WoW Context Bridge

A lightweight WoW addon that shares live player, location and nearby NPC/player context with external apps through an on-screen pixel protocol, enabling contextual voice dictation and accessibility tools.

**Experimental release: 0.1.1. Retail tested; Forever beta compatibility untested.** The addon is independent of Decktation, Whisper and Steam Deck. An external application needs a compatible screen reader to consume the context; installing the addon alone does not enable dictation.

## Install manually

1. Download the addon ZIP from [GitHub Releases](https://github.com/nukeador/wow-context-bridge/releases) and extract it, or copy `addon/WoWContextBridge` from this repository.
2. Place the entire `WoWContextBridge` folder in your client's `Interface/AddOns/` directory (`_retail_/Interface/AddOns/` for Retail; typically `_classic_beta_/Interface/AddOns/` for Forever beta). `WoWContextBridge.toc` must be directly inside that folder.
3. Remove the earlier experimental `CompanionPoC` addon if installed, to prevent overlapping output.
4. Restart WoW and enable **WoW Context Bridge** on the AddOns screen.
5. Enter the world. A small colored strip should appear at the top-left.

The current TOC targets interface `120100`. Newer client versions may mark it out of date; compatibility with those versions needs testing. No automatic game installation or modification is performed.

## Commands

- `/wcb status` or `/wcb diag`: current context and encoder diagnostics in game chat.
- `/wcb off`: hide and disable pixel output for this session.
- `/wcb on`: enable pixel output.

Output starts enabled after a reload. Commands are not persisted. Diagnostics intentionally display character/context names locally; avoid sharing their output without reviewing it.

## Available context

- Player name, zone, subzone and current target name.
- Up to eight unique readable names from active nameplate unit tokens, subject to the 256-byte payload budget.
- A sequence heartbeat every three seconds, allowing readers to detect frozen frames.

Enable the desired friendly/enemy nameplates in WoW to expose nearby names. This is not a complete list of nearby units or a distance measurement. Protected, secret or unavailable values are omitted. Quest titles are not currently included.

The strip uses eight RGB colors, nominal three-pixel cells and a versioned UTF-8 message with Fletcher-16 validation. See [the protocol](docs/protocol.md). Addon version and protocol version are independent; this release retains protocol v2 compatibility with existing readers.

## External applications

Decktation's experimental Companion integration can consume this protocol when supported by its build. Its vocabulary selection limit is separate from the addon's transport budget. The addon neither runs nor installs an external reader.

`reader/` contains reference decoder, synthetic-image and experimental Linux capture tools. These are developer tools, not a required addon installation. Do not run multiple capture clients concurrently. Native capture experiments previously coincided with Gamescope crashes; later orderly MemFd capture and the integrated reader succeeded in supervised tests, but long-session stability remains unverified. See [capture compatibility](docs/capture-compatibility.md).

## Development

Core offline decoding and PNG tools use Python 3.9+ and the standard library. Portal tools additionally require PyGObject; native probes require Linux libpipewire and a C build toolchain. These dependencies are not required by the WoW addon. Do not install system packages on SteamOS solely to install this addon.

```sh
python3 -m unittest discover -s tests -v
python3 -m reader.synthetic sample.png --offset-x 17 --offset-y 11 --cell-size 4 --noise 12 --gain 0.86
python3 -m reader.main --image sample.png
```

The Lua harness can be run with Lua 5.1 from the repository root: `lua tests/addon_harness.lua`. Automated tests cover protocol, Unicode, truncation, synthetic images, malformed frames and freshness. They do not prove live client compatibility or capture stability.

## Testing and reporting

See [the tester guide](docs/testing.md). Report addon/client versions, operating system, resolution/scaling, strip visibility and reader status. Do not include personal character names, full screenshots or raw context unless deliberately sharing them. The addon does not persist context in SavedVariables or send it over the network; external applications have their own privacy policies.

## Scope

This experiment uses ordinary addon APIs and UI textures. It does not automate movement, combat, targeting or chat, read game memory, inject code, or bypass client restrictions. Blizzard has not explicitly authorized this communication technique; it is not officially approved.

## Design references

The pixel transport was independently implemented with ideas from [wow-ai](https://github.com/chelinho139/wow-ai) (RGB cells and Fletcher-16) and research from [wow-forever-codex](https://github.com/0xinuarashi/wow-forever-codex). Capture research also consulted [Sunshine](https://github.com/LizardByte/Sunshine) and [Gamescope](https://github.com/ValveSoftware/gamescope). References do not imply endorsement or compatibility.

## License

MIT; see [LICENSE](LICENSE).

## Publishing a version

Pushes to `main` and pull requests run offline tests and build an addon-only ZIP as a workflow artifact. To publish an installable GitHub Release:

1. Update `## Version` in `addon/WoWContextBridge/WoWContextBridge.toc`.
2. Commit and push the change to `main`.
3. Create and push a matching tag, for example:

```sh
git tag v0.1.1-experimental
git push origin v0.1.1-experimental
```

The workflow checks that the tag matches the TOC version, tests the decoder, verifies the ZIP and publishes it as a release asset. Tags with a suffix are marked as prereleases. Testers should install the ZIP asset, not GitHub's automatic source-code archive. Experimental versions appear on the Releases page; GitHub's “latest stable release” may exclude prereleases. Every new version needs a new tag.

For a local package: `python3 scripts/package_addon.py`.

## Forever beta volunteer testing

The package includes `WoWContextBridge_Camelot.toc` (interface `16001`) alongside the Retail manifest. Both load the same encoder and optical protocol. This follows the manifest convention used by [existing Forever addons](https://github.com/Pirson-s-Addons/HealthBarTextForever). API checks omit unavailable/restricted text, but this does not establish compatibility: Forever loading, unit/nameplate events, strip geometry and capture are untested.

Install into the beta client's `Interface/AddOns/`, restart the client and check `/wcb status`, strip visibility and target/zone/nameplate changes. For Decktation, use a Companion integration build that recognises `WowB.exe`; earlier builds will wait for WoW. Report the beta build/interface and sanitized errors. No Classic compatibility is claimed.

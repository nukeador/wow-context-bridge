"""Build an addon-only ZIP; optionally require a matching release tag."""
import argparse
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def build(output, tag=None):
    addon = ROOT / "addon" / "WoWContextBridge"
    toc = (addon / "WoWContextBridge.toc").read_text()
    match = re.search(r"^## Version:\s*(\S+)\s*$", toc, re.MULTILINE)
    if not match:
        raise ValueError("TOC version missing")
    version = match.group(1)
    if tag is not None and tag != "v" + version:
        raise ValueError("Release tag must match the TOC version: v" + version)
    forever = (addon / "WoWContextBridge_Camelot.toc").read_text()
    if not re.search(r"^## Version:\s*" + re.escape(version) + r"\s*$", forever, re.MULTILINE):
        raise ValueError("Retail and Forever manifest versions must match")
    if "## Interface: 16001" not in forever or "WoWContextBridge.lua" not in forever:
        raise ValueError("Invalid Forever manifest")
    destination = Path(output) / ("WoWContextBridge-" + version + ".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = [(p, "WoWContextBridge/" + p.name) for p in sorted(addon.iterdir()) if p.is_file()]
    files += [(ROOT / "LICENSE", "WoWContextBridge/LICENSE"),
              (ROOT / "README.md", "WoWContextBridge/README.md")]
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, name in files:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert "WoWContextBridge/WoWContextBridge.toc" in archive.namelist()
        assert "WoWContextBridge/WoWContextBridge.lua" in archive.namelist()
        assert "WoWContextBridge/WoWContextBridge_Camelot.toc" in archive.namelist()
    return destination

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="release")
    parser.add_argument("--tag")
    args = parser.parse_args()
    print(build(args.output, args.tag))

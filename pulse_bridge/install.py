"""
Install (or remove) the PulseBridge Remote Script into Ableton Live's User Library.

    python pulse_bridge/install.py              # install for every Live version found
    python pulse_bridge/install.py --uninstall
    python pulse_bridge/install.py --user-library "D:/Music/Ableton/User Library"

The User Library location comes from Live's own Library.cfg, so it works when the
library has been moved (OneDrive, external drive, macOS or Windows). Standard library only.
"""
import argparse
import glob
import os
import shutil
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "PulseBridge")
SCRIPT_NAME = "PulseBridge"


def _pref_dirs():
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")), "Ableton")
        return glob.glob(os.path.join(base, "Live *", "Preferences"))
    if sys.platform == "darwin":
        return glob.glob(os.path.join(home, "Library", "Preferences", "Ableton", "Live *"))
    return []


def _user_library_from_cfg(cfg_path):
    try:
        root = ET.parse(cfg_path).getroot()
    except Exception:
        return None
    proj = root.find("./ContentLibrary/UserLibrary/LibraryProject")
    if proj is None:
        return None
    path = proj.find("ProjectPath")
    name = proj.find("ProjectName")
    if path is None or not path.get("Value"):
        return None
    return os.path.normpath(os.path.join(path.get("Value"), name.get("Value") if name is not None else "User Library"))


def _default_user_library():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    if sys.platform == "darwin":
        docs = os.path.join(os.path.expanduser("~"), "Music")
    return os.path.join(docs, "Ableton", "User Library")


def find_user_libraries():
    found = {}
    for pref in sorted(_pref_dirs()):
        lib = _user_library_from_cfg(os.path.join(pref, "Library.cfg"))
        if lib:
            found.setdefault(lib, []).append(os.path.basename(os.path.dirname(pref)) if sys.platform == "win32" else os.path.basename(pref))
    if not found:
        found[_default_user_library()] = ["default location"]
    return found


def install(lib, dry_run=False):
    dest = os.path.join(lib, "Remote Scripts", SCRIPT_NAME)
    print("Installing to %s" % dest)
    if dry_run:
        return
    # Overwrite in place rather than deleting the folder: while Live has the script loaded,
    # Windows (and OneDrive) can lock __pycache__, which makes rmtree fail halfway.
    os.makedirs(dest, exist_ok=True)
    wanted = set()
    for name in os.listdir(SOURCE):
        if name.endswith(".py"):
            wanted.add(name)
            shutil.copy2(os.path.join(SOURCE, name), os.path.join(dest, name))
    for name in os.listdir(dest):
        if name.endswith(".py") and name not in wanted:
            os.remove(os.path.join(dest, name))


def uninstall(lib, dry_run=False):
    dest = os.path.join(lib, "Remote Scripts", SCRIPT_NAME)
    if not os.path.isdir(dest):
        print("Not installed in %s" % lib)
        return
    print("Removing %s" % dest)
    if not dry_run:
        shutil.rmtree(dest, ignore_errors=True)
        if os.path.isdir(dest):
            print("  Some files are locked (is Live running?). Quit Live and run --uninstall again.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--user-library", help="User Library folder (skips auto-detection)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    libs = {os.path.normpath(args.user_library): ["--user-library"]} if args.user_library else find_user_libraries()
    for lib, versions in libs.items():
        print("User Library: %s  (%s)" % (lib, ", ".join(versions)))
        if args.uninstall:
            uninstall(lib, args.dry_run)
        else:
            install(lib, args.dry_run)

    if not args.uninstall and not args.dry_run:
        print(
            "\nDone. Restart Live (or re-select the script), then:\n"
            "  Settings -> Link, Tempo & MIDI -> Control Surface -> PulseBridge\n"
            "  (Input and Output can stay 'None'.)"
        )


if __name__ == "__main__":
    main()

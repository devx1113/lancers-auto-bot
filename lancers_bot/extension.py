"""The extension's current version and its update download, both from EXTENSION_DIR on this server.

An unpacked extension can't update itself (only Chrome Web Store installs auto-update), so an
outdated one is paused and offered this zip; the user unzips it over the extension's folder and
reloads it. (Writing the files into that folder from the side panel was tried in 1.6: Chrome on
Windows holds writes of .js files indefinitely.) The zip carries the folder's defaults.js, i.e. no
server URL or token: those stay in the extension's saved settings.
"""

import io
import json
import os
import zipfile

from .config import EXTENSION_DIR

FOLDER = "lancers-proposal-extension"
SKIP = {".git", "__pycache__", "node_modules"}

_manifest = {"mtime": None, "version": None}
_zip = {"key": None, "data": None}


def latest_version():
    """The manifest's version, re-read when the file changes; None if there is no extension folder"""
    path = os.path.join(EXTENSION_DIR, "manifest.json")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    if mtime != _manifest["mtime"]:
        try:
            with open(path, encoding="utf-8") as f:
                _manifest.update(mtime=mtime, version=json.load(f).get("version"))
        except (OSError, ValueError):
            return _manifest["version"]
    return _manifest["version"]


def parse(version):
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return ()


def is_outdated(version):
    """True when a version is older than the server's (an extension that sends none is older)"""
    latest = latest_version()
    return bool(latest) and parse(version or "0") < parse(latest)


def files():
    for base, dirs, names in os.walk(EXTENSION_DIR):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
        for name in sorted(names):
            if not name.startswith(".") and not name.endswith(".zip"):
                yield os.path.join(base, name)


def build_zip():
    """(version, zip bytes) of the extension folder; rebuilt when a file changes"""
    paths = list(files())
    key = (latest_version(), max((os.stat(p).st_mtime for p in paths), default=0), len(paths))
    if key != _zip["key"]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path in paths:
                z.write(path, os.path.join(FOLDER, os.path.relpath(path, EXTENSION_DIR)))
        _zip.update(key=key, data=buf.getvalue())
    return key[0], _zip["data"]

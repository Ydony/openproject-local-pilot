"""Runtime provisioning for Spark runs (issue #45). Standard library only.

The owner never installs dependencies. Before a run for a project that
declares `runtime = ["node@22"]`, the conductor makes sure that runtime is
present under a shared directory (default /opt/opl-runtimes) that the
unprivileged worker account can read and execute but not write:

  <root>/node/22.17.0/bin/node ...   (+ the install marker below)

An archive is used only after its SHA-256 matches the publisher's checksum
file; it is unpacked into a temporary folder beside the target and renamed
into place, so a failed or interrupted install never leaves a half-installed
runtime. Installing is idempotent: an installed version satisfying the spec
is reused with no network call.

Only Node.js has a provisioner. Other declared runtimes are reported as
unsupported (the project is then not ready; readiness check, issue #43).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
import urllib.request

DEFAULT_ROOT = "/opt/opl-runtimes"
NODE_BASE = "https://nodejs.org/dist"
MARKER = ".opl-installed"

_SPEC = re.compile(r"^([a-z][a-z0-9]*)@([0-9]+(?:\.[0-9]+){0,2})$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SUPPORTED = ("node",)


class RuntimeFailure(RuntimeError):
    """A runtime cannot be provisioned; the message says why and the fix."""


def parse_spec(spec):
    """("node", "22") for "node@22"; RuntimeFailure for anything else."""
    match = _SPEC.match(str(spec or ""))
    if not match:
        raise RuntimeFailure("bad runtime %r: expected name@version, "
                             "e.g. node@22" % (spec,))
    return match.group(1), match.group(2)


def _version_key(version):
    return tuple(int(part) for part in version.split("."))


def _matches(version, prefix):
    """True when `version` (1.2.3) is `prefix` (1, 1.2 or 1.2.3) or inside it."""
    return version == prefix or version.startswith(prefix + ".")


def default_fetch(url, timeout=180):
    """HTTPS GET returning bytes. Only https URLs are ever requested."""
    if not url.startswith("https://"):
        raise RuntimeFailure("refusing a non-https download: %s" % url)
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read()


def _arch():
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise RuntimeFailure("no Node.js build for machine %r" % machine)


def installed_versions(root, name):
    """Installed versions of `name` (marker present), newest first."""
    base = os.path.join(root, name)
    found = []
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    for entry in entries:
        if _VERSION.match(entry) and os.path.isfile(
                os.path.join(base, entry, MARKER)):
            found.append(entry)
    return sorted(found, key=_version_key, reverse=True)


def find_installed(root, spec):
    """The newest installed version satisfying `spec`, or None. No network."""
    name, prefix = parse_spec(spec)
    for version in installed_versions(root, name):
        if _matches(version, prefix):
            return version
    return None


def resolve_node(prefix, fetch=default_fetch):
    """The newest published Node.js version inside `prefix` (e.g. "22")."""
    try:
        index = json.loads(fetch(NODE_BASE + "/index.json").decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeFailure("unreadable Node.js release index: %s"
                             % type(exc).__name__)
    wanted = "linux-" + _arch()
    best = None
    for entry in index if isinstance(index, list) else []:
        raw = str(entry.get("version", "")).lstrip("v")
        files = entry.get("files") or []
        if not _VERSION.match(raw) or not _matches(raw, prefix):
            continue
        if files and wanted not in files:
            continue
        if best is None or _version_key(raw) > _version_key(best):
            best = raw
    if best is None:
        raise RuntimeFailure("no published Node.js release matches %s" % prefix)
    return best


def _safe_members(tar):
    """Members of the archive that stay inside the target folder."""
    safe = []
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or ".." in name.split("/"):
            raise RuntimeFailure("unsafe path in the archive: %r" % name)
        if member.issym() or member.islnk():
            target = member.linkname
            joined = os.path.normpath(os.path.join(os.path.dirname(name), target))
            if target.startswith("/") or joined.startswith(".."):
                raise RuntimeFailure("unsafe link in the archive: %r" % name)
        if member.isdev() or member.isfifo():
            raise RuntimeFailure("special file in the archive: %r" % name)
        safe.append(member)
    return safe


def install_node(root, version, fetch=default_fetch):
    """Download, verify and atomically install Node.js `version` (1.2.3)."""
    if not _VERSION.match(version):
        raise RuntimeFailure("bad Node.js version %r" % (version,))
    archive = "node-v%s-linux-%s.tar.xz" % (version, _arch())
    base = "%s/v%s" % (NODE_BASE, version)
    sums = fetch(base + "/SHASUMS256.txt").decode("utf-8", "replace")
    expected = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive:
            expected = parts[0].lower()
    if not expected or not re.match(r"^[0-9a-f]{64}$", expected):
        raise RuntimeFailure("no checksum for %s in the publisher's file"
                             % archive)
    data = fetch(base + "/" + archive)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeFailure("checksum mismatch for %s: refusing to install"
                             % archive)
    parent = os.path.join(root, "node")
    dest = os.path.join(parent, version)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".tmp-", dir=parent)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tar:
            members = _safe_members(tar)
            top = {m.name.split("/", 1)[0] for m in members}
            if len(top) != 1:
                raise RuntimeFailure("unexpected archive layout")
            unpack = os.path.join(tmp, "unpack")
            os.mkdir(unpack)
            try:
                tar.extractall(unpack, members=members, filter="data")
            except TypeError:  # Python without extraction filters
                tar.extractall(unpack, members=members)
        inner = os.path.join(unpack, top.pop())
        marker = os.path.join(inner, MARKER)
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"name": "node", "version": version,
                       "archive": archive, "sha256": actual}, fh)
        _make_readable(inner)
        try:
            os.rename(inner, dest)
        except OSError:
            if not os.path.isfile(os.path.join(dest, MARKER)):
                raise
            # A concurrent install finished first: keep it.
    except RuntimeFailure:
        raise
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise RuntimeFailure("could not install Node.js %s (%s)"
                             % (version, type(exc).__name__))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def _make_readable(path):
    """World read/execute, never world or group write: the worker may use
    the runtime but not change it."""
    for here, dirs, files in os.walk(path):
        for name in dirs + files:
            full = os.path.join(here, name)
            if os.path.islink(full):
                continue
            mode = os.stat(full).st_mode
            add = 0o055 if (os.path.isdir(full) or mode & 0o100) else 0o044
            os.chmod(full, (mode | add) & ~0o022)
    os.chmod(path, (os.stat(path).st_mode | 0o055) & ~0o022)


def ensure(root, specs, fetch=default_fetch):
    """Make every runtime in `specs` present; return [(spec, version, bin)].

    Raises RuntimeFailure naming the first problem. An installed runtime that
    satisfies a spec is reused without touching the network.
    """
    if not os.path.isdir(root):
        raise RuntimeFailure(
            "runtimes directory %s does not exist: one-time owner step, see "
            "docs/SANDBOX.md (Runtimes)" % root)
    ready = []
    for spec in specs or ():
        name, prefix = parse_spec(spec)
        if name not in SUPPORTED:
            raise RuntimeFailure("no provisioner for %s (supported: %s)"
                                 % (spec, ", ".join(SUPPORTED)))
        version = find_installed(root, spec)
        if version is None:
            if not os.access(root, os.W_OK):
                raise RuntimeFailure(
                    "runtimes directory %s is not writable by the conductor: "
                    "one-time owner step, see docs/SANDBOX.md (Runtimes)" % root)
            version = resolve_node(prefix, fetch)
            install_node(root, version, fetch)
        ready.append((spec, version,
                      os.path.join(root, name, version, "bin")))
    return ready


def describe(root, specs):
    """Readiness lines per declared runtime, without any network call."""
    lines = []
    for spec in specs or ():
        try:
            name, _prefix = parse_spec(spec)
            version = find_installed(root, spec) if name in SUPPORTED else None
        except RuntimeFailure as exc:
            lines.append((spec, False, str(exc)))
            continue
        if name not in SUPPORTED:
            lines.append((spec, False, "no provisioner for %s" % spec))
        elif version:
            lines.append((spec, True, "%s %s installed" % (name, version)))
        else:
            lines.append((spec, False, "%s not installed yet (the conductor "
                          "installs it before the first run)" % spec))
    return lines

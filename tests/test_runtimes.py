#!/usr/bin/env python3
"""Tests for opl.runtimes: version resolution, checksum, atomic install,
idempotency, unsafe archives. Synthetic archive; no network (issue #45)."""

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

from opl import runtimes


def make_archive(version, extra=None):
    """A tiny tar.xz shaped like the official Node.js release."""
    top = "node-v%s-linux-x64" % version
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        def add(name, data, mode=0o755):
            info = tarfile.TarInfo(top + "/" + name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
        add("bin/node", b"#!/bin/sh\necho v%s\n" % version.encode())
        add("README.md", b"readme", 0o644)
        for name, data in (extra or {}).items():
            add(name, data)
    return buf.getvalue()


class Publisher:
    """Fake nodejs.org: release index, checksums and archives."""

    def __init__(self, versions, corrupt=()):
        self.versions = versions
        self.corrupt = set(corrupt)
        self.requests = []
        self.archives = {v: make_archive(v) for v in versions}

    def __call__(self, url, timeout=0):
        self.requests.append(url)
        if url.endswith("/index.json"):
            return json.dumps([
                {"version": "v" + v, "files": ["linux-x64", "osx-arm64"]}
                for v in self.versions]).encode()
        for v in self.versions:
            base = "%s/v%s/" % (runtimes.NODE_BASE, v)
            name = "node-v%s-linux-x64.tar.xz" % v
            if url == base + "SHASUMS256.txt":
                digest = hashlib.sha256(self.archives[v]).hexdigest()
                if v in self.corrupt:
                    digest = "0" * 64
                return ("%s  %s\n%s  other-file.zip\n" % (
                    digest, name, "1" * 64)).encode()
            if url == base + name:
                return self.archives[v]
        raise AssertionError("unexpected url %s" % url)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="opl-rt-")
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.object(runtimes, "_arch", return_value="x64")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_spec_parsing(self):
        self.assertEqual(runtimes.parse_spec("node@22"), ("node", "22"))
        self.assertEqual(runtimes.parse_spec("node@22.17.0"),
                         ("node", "22.17.0"))
        for bad in ("node", "node@", "@22", "Node@22", "node@22;rm", ""):
            with self.assertRaises(runtimes.RuntimeFailure):
                runtimes.parse_spec(bad)

    def test_resolves_the_newest_matching_version(self):
        pub = Publisher(["20.19.1", "22.3.0", "22.17.0", "23.1.0"])
        self.assertEqual(runtimes.resolve_node("22", pub), "22.17.0")
        self.assertEqual(runtimes.resolve_node("22.3", pub), "22.3.0")
        with self.assertRaises(runtimes.RuntimeFailure):
            runtimes.resolve_node("18", pub)

    def test_installs_verified_archive_and_reports_the_bin_dir(self):
        pub = Publisher(["22.17.0"])
        ready = runtimes.ensure(self.root, ["node@22"], pub)
        self.assertEqual([(s, v) for s, v, _b in ready],
                         [("node@22", "22.17.0")])
        node = os.path.join(self.root, "node", "22.17.0", "bin", "node")
        self.assertTrue(os.path.isfile(node))
        self.assertTrue(os.path.isfile(os.path.join(
            self.root, "node", "22.17.0", runtimes.MARKER)))
        # No leftovers from the temporary folder.
        self.assertEqual(os.listdir(os.path.join(self.root, "node")),
                         ["22.17.0"])
        with open(os.path.join(self.root, "node", "22.17.0", runtimes.MARKER),
                  encoding="utf-8") as fh:
            marker = json.load(fh)
        self.assertEqual(marker["sha256"],
                         hashlib.sha256(pub.archives["22.17.0"]).hexdigest())

    def test_install_is_idempotent_and_needs_no_network_second_time(self):
        pub = Publisher(["22.17.0"])
        runtimes.ensure(self.root, ["node@22"], pub)
        first = len(pub.requests)

        def forbidden(url, timeout=0):
            raise AssertionError("network used for an installed runtime")
        again = runtimes.ensure(self.root, ["node@22", "node@22.17"],
                                forbidden)
        self.assertEqual(len(pub.requests), first)
        self.assertEqual([v for _s, v, _b in again], ["22.17.0", "22.17.0"])

    def test_checksum_mismatch_installs_nothing(self):
        pub = Publisher(["22.17.0"], corrupt=["22.17.0"])
        with self.assertRaises(runtimes.RuntimeFailure) as ctx:
            runtimes.ensure(self.root, ["node@22"], pub)
        self.assertIn("checksum mismatch", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(
            self.root, "node", "22.17.0")))
        node_dir = os.path.join(self.root, "node")
        self.assertEqual(os.listdir(node_dir) if os.path.isdir(node_dir)
                         else [], [])

    def test_unsafe_archive_member_is_refused(self):
        pub = Publisher(["22.17.0"])
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tar:
            info = tarfile.TarInfo("node-v22.17.0-linux-x64/../../escape")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        pub.archives["22.17.0"] = buf.getvalue()
        with self.assertRaises(runtimes.RuntimeFailure) as ctx:
            runtimes.ensure(self.root, ["node@22"], pub)
        self.assertIn("unsafe", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(
            self.root, "node", "22.17.0")))

    def test_unsupported_runtime_and_missing_root_are_explained(self):
        with self.assertRaises(runtimes.RuntimeFailure) as ctx:
            runtimes.ensure(self.root, ["python@3.12"], Publisher([]))
        self.assertIn("no provisioner", str(ctx.exception))
        with self.assertRaises(runtimes.RuntimeFailure) as ctx:
            runtimes.ensure(os.path.join(self.root, "absent"), ["node@22"],
                            Publisher([]))
        self.assertIn("one-time owner step", str(ctx.exception))

    def test_installed_files_are_readable_but_not_writable_by_others(self):
        if os.name == "nt":
            self.skipTest("POSIX permission bits")
        runtimes.ensure(self.root, ["node@22"], Publisher(["22.17.0"]))
        node = os.path.join(self.root, "node", "22.17.0", "bin", "node")
        mode = os.stat(node).st_mode
        self.assertTrue(mode & 0o005)      # others read+execute
        self.assertFalse(mode & 0o022)     # no group/other write

    def test_describe_reports_installed_and_missing_without_network(self):
        runtimes.ensure(self.root, ["node@22"], Publisher(["22.17.0"]))
        lines = runtimes.describe(self.root, ["node@22", "node@20",
                                              "python@3.12"])
        self.assertEqual([(s, ok) for s, ok, _m in lines],
                         [("node@22", True), ("node@20", False),
                          ("python@3.12", False)])
        self.assertIn("22.17.0", lines[0][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)

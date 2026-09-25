"""bin/opl-configure: apply the tracker model to the localhost pilot.

Usage: opl-configure [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from opl.configure import admin_ruby, apply_api, views
from opl.model import ModelError, load as load_model
from opl.openproject import ApiError, Client
from opl.settings import SettingsError, load as load_settings, redact


def _repo_root():
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def _run_opl_compose(root, args, settings):
    """Run bin/opl-compose; return (ok, redacted tail)."""
    # Through bash explicitly: Windows cannot exec a shebang script
    # directly, and the whole toolkit already requires bash anyway.
    bash = shutil.which("bash")
    if not bash:
        return False, "bash not found on PATH"
    proc = subprocess.run(
        [bash, os.path.join(root, "bin", "opl-compose")] + list(args),
        capture_output=True,
        text=True,
        timeout=600,
    )
    tail = (proc.stdout + proc.stderr)[-500:]
    return proc.returncode == 0, redact(tail, settings)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="opl-configure")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without changing anything")
    args = parser.parse_args(argv)

    root = _repo_root()
    try:
        model = load_model(os.path.join(root, "config", "pm-model.toml"))
        settings = load_settings()
        token = settings.token("admin")
    except (ModelError, SettingsError) as exc:
        print("opl-configure: error: %s" % exc, file=sys.stderr)
        return 1
    client = Client(settings.openproject.url, token)

    script = admin_ruby.render_admin_script(model)
    if args.dry_run:
        print("dry run: no changes made")
        print(
            "admin script: %d statuses, %d types, %d roles, %d workflows, "
            "%d fields, %d project fields (%d lines)"
            % (len(model.statuses), len(model.types), len(model.roles),
               len(model.workflows), len(model.fields),
               len(model.project_fields), len(script.splitlines()))
        )
        print("planned actions:")
        try:
            planned = apply_api.apply_api(client, model, settings, dry_run=True)
            planned += views.apply_views(client, model, settings, dry_run=True)
        except ApiError as exc:
            print("opl-configure: error: %s" % exc, file=sys.stderr)
            return 1
        for desc in planned:
            print("would " + desc)
        return 0

    path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".rb", prefix="opl-admin-",
            encoding="utf-8", newline="\n", delete=False,
        ) as fh:
            fh.write(script)
            path = fh.name
        ok, tail = _run_opl_compose(root, ["cp", path, "web:/tmp/opl-admin.rb"], settings)
        if not ok:
            print("opl-configure: error: compose cp failed: %s" % tail,
                  file=sys.stderr)
            return 1
        ok, tail = _run_opl_compose(
            root,
            ["exec", "-T", "web", "bundle", "exec", "rails", "runner", "/tmp/opl-admin.rb"],
            settings,
        )
        if not ok:
            print("opl-configure: error: rails runner failed: %s" % tail,
                  file=sys.stderr)
            return 1
        try:
            for desc in apply_api.apply_api(client, model, settings):
                print(desc)
            for desc in views.apply_views(client, model, settings):
                print(desc)
        except ApiError as exc:
            print("opl-configure: error: %s" % exc, file=sys.stderr)
            return 1
    finally:
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

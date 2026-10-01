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


def _first_error_line(text):
    """First Ruby error line (the exception class/message), else "".

    A Rails failure's last 500 characters are only stack frames
    (`.../gems/...:in ...`); the useful line is the first one naming the
    exception, e.g. `RuntimeError: boom` or
    `ActiveRecord::RecordInvalid: Validation failed`.
    """
    import re

    explicit = re.compile(
        r"^\s*[A-Za-z_:][\w:]*(Error|Exception|Failure|Invalid|Taken|Denied)\b.*")
    for line in str(text).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if explicit.search(line):
            # Stack frames look like `/path/file.rb:10:in \`method'`:
            # they name a location, not an exception.
            if ":in `" in line or ":in '" in line:
                continue
            return stripped
        if stripped.startswith("raise ") or re.match(r"^\s*Error\s*:", line):
            return stripped
        # Generic `Class[:...]: message` exception lines (e.g.
        # `ActiveRecord::RecordInvalid: ...`): a capitalized token with `::`
        # or CamelCase before the first colon, and not a stack frame.
        if ":in `" in line or ":in '" in line:
            continue
        head, sep, _tail = stripped.partition(":")
        if sep and re.search(r"[A-Z]", head) and re.match(
                r"^[\w:]+$", head.strip()):
            return stripped
    return ""


def _run_opl_compose(root, args, settings):
    """Run bin/opl-compose; return (ok, redacted error-first summary)."""
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
    combined = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return True, redact(combined[-500:], settings)
    first = _first_error_line(combined)
    tail = combined[-300:]
    summary = (first + "\n" + tail).strip() if first else tail.strip()
    # Keep the report short but always include the exception line.
    if first and first not in tail:
        summary = (first + "\n" + tail[-300:]).strip()
    return False, redact(summary[-800:], settings)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="opl-configure")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without changing anything")
    parser.add_argument("--repair-my-page", action="store_true",
                        help="recreate hidden queries for My page widgets "
                             "whose query is gone before pinning")
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
            "%d fields, %d project fields, %d priorities (%d lines)"
            % (len(model.statuses), len(model.types), len(model.roles),
               len(model.workflows), len(model.fields),
               len(model.project_fields), len(admin_ruby.PRIORITY_RENAMES),
               len(script.splitlines()))
        )
        print("planned actions:")
        try:
            planned = admin_ruby.describe_priorities()
            planned += admin_ruby.describe_workflow_cleanup(model)
            planned += apply_api.apply_api(client, model, settings, dry_run=True)
            planned += views.apply_views(client, model, settings, dry_run=True,
                                         repair_my_page=args.repair_my_page)
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
            created = []
            for desc in apply_api.apply_api(client, model, settings):
                print(desc)
                prefix = "create Project setup feature in "
                if desc.startswith(prefix):
                    created.append(desc[len(prefix):])
            for desc in views.apply_views(client, model, settings,
                                          repair_my_page=args.repair_my_page):
                print(desc)
            for name in created:
                print("next step for %s: run bin/opl-project-check <project "
                      "key> (a project counts as migrated only when it "
                      "passes)" % name)
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

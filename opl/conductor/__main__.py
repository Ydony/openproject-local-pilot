"""bin/opl-conductor: run the check-and-fix loop (watch mode by default).

Usage: opl-conductor [--once] [--live] | opl-conductor runs
Live mode additionally requires [conductor] live = true in the config.
`runs` prints the tuning report from <state_dir>/runs.jsonl.

Least privilege (TH.9): OpenProject is read and changed with the
conductor's own token; the admin token is never loaded. One conductor per
state dir (a lock file). The Spark runner exists only in live mode, is
ticked once per cycle, and its workers are stopped on exit.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone

from opl.conductor.collect import collect_github, collect_openproject
from opl.conductor.engine import apply, run_once
from opl.conductor.lock import AlreadyRunning, InstanceLock
from opl.github import GitHub
from opl.model import ModelError, load as load_model
from opl.openproject import ApiError, Client
from opl.settings import SettingsError, load as load_settings, redact


def _repo_root():
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def _cycle(op_client, gh, settings, model, live, runner=None):
    """One check-and-fix pass; returns the world it acted on."""
    projects, items = collect_openproject(
        op_client, settings, model, datetime.now(timezone.utc))
    pull_requests, deploys = collect_github(gh, projects, items)
    from opl.conductor.state import World

    world = World(now=datetime.now(timezone.utc), projects=projects,
                  items=items, pull_requests=pull_requests, deploys=deploys)
    changes = run_once(world, model, costs=_cost_changes(world, settings))
    log_path = os.path.join(settings.conductor.state_dir, "watch.log")
    apply(changes, world, op_client, gh, live, log_path)
    if runner is not None:
        # The runner re-checks every item itself before starting it (TH.6),
        # so this cycle's snapshot is safe to hand over.
        _report(runner.tick(world), settings)
    return world


def _report(actions, settings):
    for line in actions:
        print("opl-conductor: spark: %s" % redact(line, settings))


def _make_runner(settings, model, gh):
    """The Spark runner, acting in OpenProject as the spark user."""
    from opl.conductor.spark.runner import SparkRunner
    from opl.conductor.spark.supervisor import Slots, reset_stop_gate

    # The stop gate (TH.17) is process-wide and stays shut after a
    # shutdown; a new runner in the same process starts with it open.
    reset_stop_gate()
    spark = Client(settings.openproject.url, settings.token("spark"))
    return SparkRunner(settings, model, spark, gh,
                       Slots(settings.runner.max_parallel))


def _on_sigterm(signum, frame):
    raise SystemExit(128 + signum)


def _ccusage_env(settings):
    """This process's environment minus every credential it knows of
    (TH.19, TH.23): ccusage (an npx package) only reads local logs.

    Dropped: the admin, GitHub and per-role token variables, anything
    named OPL_TOKEN_*, and every `[runner] worker_env` name (the Spark
    worker's provider key). Names compare case-insensitively, as Windows
    environment names do.
    """
    secret = {name.upper() for name in
              ({settings.openproject.admin_token_env, settings.github_token_env}
               | set(settings.tokens.values())
               | set(settings.runner.worker_env))}
    return {name: value for name, value in os.environ.items()
            if name.upper() not in secret
            and not name.upper().startswith("OPL_TOKEN_")}


def _cost_changes(world, settings):
    """Cost changes for one loop; never fails the loop.

    ccusage failure skips that loop's actuals (estimates still run); any
    other cost error is logged and the loop continues without costs.
    """
    import logging

    from opl.conductor.rules import costs as rule_costs
    from opl.costs_config import load_estimates, load_prices
    from opl import usage
    from opl.usage import UsageError

    logger = logging.getLogger("opl.conductor")
    try:
        prices = load_prices(os.path.join(_repo_root(), "config", "prices.toml"))
        estimates = load_estimates(
            os.path.join(_repo_root(), "config", "estimates.toml"))
    except ValueError as exc:
        logger.error("cost configs unreadable, skipping costs: %s", exc)
        return []
    try:
        state_dir = settings.conductor.state_dir
        actuals = usage.collect_actuals(
            world, prices, state_dir=state_dir, env=_ccusage_env(settings),
            spark_data=os.path.join(state_dir, "spark-data"))
    except UsageError as exc:
        logger.warning("ccusage failed, skipping actuals: %s", exc)
        actuals = None
    try:
        return rule_costs.costs(world, prices, estimates, actuals)
    except (ValueError, KeyError, AttributeError) as exc:
        logger.error("costs rule failed, skipping costs: %s", exc)
        return []


def main(argv=None):
    parser = argparse.ArgumentParser(prog="opl-conductor")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("command", nargs="?", default=None)
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        model = load_model(os.path.join(_repo_root(), "config", "pm-model.toml"))
    except (SettingsError, ModelError) as exc:
        print("opl-conductor: error: %s" % exc, file=sys.stderr)
        return 1

    if args.command == "runs":
        from opl.conductor.spark.records import (
            format_report,
            read_runs,
            summarize,
        )

        print(format_report(summarize(
            read_runs(settings.conductor.state_dir))), end="")
        return 0
    if args.command == "estimates":
        from opl.calibration import calibrate, format_calibration
        from opl.costs_config import load_estimates

        try:
            op_client = Client(settings.openproject.url,
                               settings.token("conductor"))
        except (SettingsError, ModelError) as exc:
            print("opl-conductor: error: %s" % exc, file=sys.stderr)
            return 1
        projects, items = collect_openproject(
            op_client, settings, model, datetime.now(timezone.utc))
        from opl.conductor.state import World

        world = World(now=datetime.now(timezone.utc), projects=projects,
                      items=items, pull_requests={}, deploys=())
        estimates = load_estimates(
            os.path.join(_repo_root(), "config", "estimates.toml"))
        print(format_calibration(calibrate(world, estimates)), end="")
        return 0
    if args.command is not None:
        print("opl-conductor: error: unknown command %r" % args.command,
              file=sys.stderr)
        return 1
    live = bool(args.live) and bool(settings.conductor.live)
    try:
        op_client = Client(settings.openproject.url, settings.token("conductor"))
        gh = GitHub(settings.token("github"))
        runner = _make_runner(settings, model, gh) if live else None
    except (SettingsError, ModelError) as exc:
        print("opl-conductor: error: %s" % exc, file=sys.stderr)
        return 1

    lock = InstanceLock(settings.conductor.state_dir)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print("opl-conductor: error: %s" % exc, file=sys.stderr)
        return 1
    previous = None
    if hasattr(signal, "SIGTERM"):
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return _loop(args, op_client, gh, settings, model, live, runner)
    except KeyboardInterrupt:
        return 130
    finally:
        if runner is not None:
            stopped = runner.shutdown()
            if stopped:
                print("opl-conductor: stopped %d running worker(s)" % stopped,
                      file=sys.stderr)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        lock.release()


def _loop(args, op_client, gh, settings, model, live, runner):
    interval = settings.conductor.interval_seconds
    while True:
        world = None
        try:
            world = _cycle(op_client, gh, settings, model, live, runner)
        except ApiError as exc:
            # Unreachable or an incomplete snapshot (e.g. a failed page):
            # never act on a partial world (TH.7).
            print("opl-conductor: read failed, skipping cycle: %s"
                  % redact(str(exc), settings), file=sys.stderr)
        if args.once:
            if runner is not None and world is not None:
                # A single cycle leaves nothing running or unconcluded.
                _report(runner.drain(world), settings)
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())

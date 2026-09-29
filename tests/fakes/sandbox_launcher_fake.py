"""Fake opl-spark-launch for synthetic sandbox-adapter tests.

Implements the three launcher modes over a local bare repo; never touches
sudo, credentials, the network, or a real worker account. Standard library
only. Control via environment:

  FAKE_WORKTREES_ROOT  where <ID>/repo trees live (required)
  FAKE_UPSTREAM_BARE   bare repo the clone copies (required for clone)
  FAKE_LAUNCHER_LOG    JSON-lines record of invocations (optional)
  FAKE_LAUNCHER_MODE   run-mode selector (optional):
      default   text + tool + usage lines, OPL-RESULT DONE, exit 0
      no-usage  text + tool only (adapter must print "usage unknown")
      progress  touch repo/progress.txt, then behave like default
      commit    write worker.txt and commit it, then behave like default
      idle      sleep briefly, chatter only, no repo change, no usage line
      hang      sleep 30 s (signal tests kill it first)
  FAKE_LAUNCHER_EXIT   exit code for run mode (default 0)

Export mode bundles the worker repo's new commits (``--ref``..``--branch``)
to stdout and prints the uncommitted count on stderr; with no new commits
it exits 3.
"""

import json
import os
import subprocess
import sys
import time


def _env(name, default=""):
    return os.environ.get(name, default)


def log_call(argv):
    path = _env("FAKE_LAUNCHER_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"argv": argv}) + "\n")
    except OSError:
        pass


def _git(*args, cwd=None, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120)
    if check and proc.returncode != 0:
        print("fake launcher: git %s failed: %s"
              % (" ".join(args), proc.stderr.strip()[:200]),
              file=sys.stderr)
        raise SystemExit(2)
    return proc


def _opts(argv):
    """{"--flag": value} for --flag value / --flag=value args."""
    out = {}
    items = list(argv)
    while items:
        item = items.pop(0)
        if item.startswith("--") and "=" in item:
            key, _, value = item[2:].partition("=")
            out[key] = value
        elif item.startswith("--"):
            out[item[2:]] = items.pop(0) if items else ""
        else:
            out.setdefault("_pos", []).append(item)
    return out


def do_clone(opts):
    root = _env("FAKE_WORKTREES_ROOT")
    bare = _env("FAKE_UPSTREAM_BARE")
    if not root or not bare:
        print("fake launcher: clone needs FAKE_WORKTREES_ROOT/FAKE_UPSTREAM_BARE",
              file=sys.stderr)
        return 2
    run_id = opts.get("run", "")
    branch = opts.get("branch", "")
    ref = opts.get("ref", "")
    if not run_id or not branch or len(ref) != 40:
        print("fake launcher: bad clone args", file=sys.stderr)
        return 2
    dest = os.path.join(root, run_id, "repo")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _git("clone", "-q", "--no-checkout", bare, dest)
    _git("-C", dest, "checkout", "-q", "-B", branch, ref)
    return 0


def _repo_for(run_id):
    root = _env("FAKE_WORKTREES_ROOT")
    return os.path.join(root, run_id, "repo") if root else ""


def do_run(opts):
    run_id = opts.get("run", "")
    repo = _repo_for(run_id)
    packet = sys.stdin.read()
    if "WRITE_YES" in packet and repo and os.path.isdir(repo):
        try:
            with open(os.path.join(repo, "yes.txt"), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.write("YES\n")
        except OSError:
            pass
    mode = _env("FAKE_LAUNCHER_MODE", "default")
    if mode == "hang":
        time.sleep(30)
        return 0
    if mode == "progress" and repo and os.path.isdir(repo):
        try:
            with open(os.path.join(repo, "progress.txt"), "a",
                      encoding="utf-8", newline="\n") as fh:
                fh.write("progress\n")
        except OSError:
            pass
        time.sleep(0.2)
    if mode == "idle":
        time.sleep(0.3)
        print(json.dumps({"type": "text",
                          "part": {"type": "text",
                                   "text": "fake worker idling, no commits"}}))
        sys.stdout.flush()
        return int(_env("FAKE_LAUNCHER_EXIT", "0") or "0")
    if mode == "commit" and repo and os.path.isdir(repo):
        with open(os.path.join(repo, "worker.txt"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("worker did the task\n")
        _git("-C", repo, "add", "-A")
        _git("-C", repo, "-c", "user.email=worker@synthetic.invalid",
             "-c", "user.name=synthetic-worker",
             "commit", "-qm", "worker: do the tiny task")
    # Real OpenCode 1.18 shape (issue #27): text under part.text, the
    # tool title under part.state.title, usage under part.tokens/cost.
    print(json.dumps({"type": "text",
                      "part": {"type": "text",
                               "text": "fake worker did the task\n"
                                      "OPL-RESULT: DONE fake-ok"}}))
    print(json.dumps({"type": "tool_use",
                      "part": {"type": "tool", "tool": "edit",
                               "state": {"status": "completed",
                                         "title": "edit demo.txt"}}}))
    if mode != "no-usage":
        print(json.dumps({"type": "step_finish",
                          "part": {"type": "step-finish",
                                   "tokens": {"input": 10, "output": 5},
                                   "cost": 0.0025}}))
    sys.stdout.flush()
    return int(_env("FAKE_LAUNCHER_EXIT", "0") or "0")


def do_export(opts):
    run_id = opts.get("run", "")
    ref = opts.get("ref", "")
    branch = opts.get("branch", "")
    repo = _repo_for(run_id)
    if not repo or not os.path.isdir(repo) or not branch or len(ref) != 40:
        print("fake launcher: bad export args", file=sys.stderr)
        return 2
    dirty = _git("-C", repo, "status", "--porcelain", check=False)
    dirty_count = len([line for line in dirty.stdout.splitlines() if line.strip()])
    print("uncommitted: %d" % dirty_count, file=sys.stderr)
    ahead = _git("-C", repo, "rev-list", "--count",
                 "%s..%s" % (ref, branch), check=False)
    try:
        count = int(ahead.stdout.strip())
    except ValueError:
        count = 0
    if count <= 0:
        return 3
    proc = subprocess.run(
        ["git", "-C", repo, "bundle", "create", "-",
         "%s..%s" % (ref, branch)],
        capture_output=True, timeout=120)
    if proc.returncode != 0:
        print("fake launcher: bundle failed", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(proc.stdout)
    sys.stdout.buffer.flush()
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] not in ("clone", "run", "export"):
        print("fake launcher: usage: launch <clone|run|export> ...",
              file=sys.stderr)
        return 2
    log_call(argv[1:])
    opts = _opts(argv[2:])
    if argv[1] == "clone":
        return do_clone(opts)
    if argv[1] == "run":
        return do_run(opts)
    return do_export(opts)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

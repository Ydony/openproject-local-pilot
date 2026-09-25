# Spark worker isolation — TH.14 preparation

Status: preparation only. Nothing here installs a sandbox automatically.
Keep `live = false` until the owner approves setup, the follow-ups below land,
and a real canary passes through the final launch path. Offline fake tests
prove the checker, not the machine's isolation.

## Recommendation and limits

Recommend (a), a dedicated unprivileged Linux user in a dedicated WSL
distribution, as the simplest first permission boundary. Run the conductor
as the owner, never root. Only approved public/synthetic work enters the
worker's filesystem or prompts. Keep credentials for OpenProject, GitHub
and other models in the owner process, not the worker.

WSL is **not a security boundary against hostile code on the Windows host**.
Disabling drive mounts and Windows interop reduces exposure, not that threat.
Use a separate VM/security context if that stronger guarantee is required.
See [Microsoft's WSL security model](https://wsl.dev/technical-documentation/security/).

## Owner-run setup (not run by an agent)

Use a fresh dedicated distribution, not the distribution hosting working
services. Install reviewed Python, Git and OpenCode builds there separately.
Do not copy owner auth/config directories. The example account and paths below
are synthetic. Commands assume Ubuntu and an existing non-root owner account.
Stop if any destination/account already exists: inspect it instead of changing
ownership recursively. There are no recursive chmod/chown instructions here.

In that distribution's owner shell:

```sh
test "$(id -u)" -ne 0 || exit 1
OPL_OWNER=$(id -un)
OPL_GROUP=$(id -gn)
getent passwd opl-worker && exit 1
test ! -e /srv/opl-worker && test ! -e /srv/opl-worktrees || exit 1
sudo useradd --create-home --home-dir /srv/opl-worker --shell /bin/bash opl-worker
sudo passwd --lock opl-worker
sudo chmod 700 /srv/opl-worker
sudo install -d -o "$OPL_OWNER" -g "$OPL_GROUP" -m 755 /srv/opl-worktrees
sudo install -d -o opl-worker -g opl-worker -m 700 /srv/opl-worktrees/canary-run
sudo install -d -o "$OPL_OWNER" -g "$OPL_GROUP" -m 700 /var/lib/opl-conductor
install -d -m 700 "$HOME/.config/opl"
test ! -e "$HOME/.config/opl/opl.toml" || chmod 600 "$HOME/.config/opl/opl.toml"
id opl-worker
```

The worker must not belong to sudo, docker, lxd, disk or owner groups. The
conductor state stays owner-only (0700); records/logs/config stay 0600. The
worktrees parent is owner-owned 0755: the worker cannot create/remove sibling
runs. Only each assigned checkout is worker-owned 0700. Its HOME is 0700 and
must contain no owner secrets. Each run needs a separately provisioned HOME
under that worker-owned area, not a copy of the owner's HOME.

Protect other private owner directories with owner-only permissions after
checking their actual sharing needs. Do not apply blanket changes to existing
projects. World-readable private files remain readable despite this new user.

For the dedicated distribution only, use `sudoedit /etc/wsl.conf` and merge
these settings with any existing sections (do not overwrite unrelated values):

```ini
[automount]
enabled=false
mountFsTab=false
[interop]
enabled=false
appendWindowsPath=false
```

Apply during an owner-chosen maintenance window by terminating **only that
distribution**, then reopening it. Never terminate Docker's distribution or
all WSL instances. Audit pre-existing/manual host mounts too. Configuration
details: [Microsoft WSL settings](https://learn.microsoft.com/en-us/windows/wsl/wsl-config).

## Launch contract and environment

The following is the intended external config **after the follow-ups land**;
`opl-spark-launch` is a required reviewed helper, not supplied by this prep.
Do not enable this command yet:

```toml
[runner]
command = ["/usr/bin/sudo", "-n", "-H", "-u", "opl-worker", "--preserve-env=OPL_WORKER_API_KEY", "--", "/usr/local/libexec/opl-spark-launch", "--workdir", "{workdir}", "--packet", "{packet}"]
worker_env = ["OPL_WORKER_API_KEY"]
```

TH.1 still builds the initial allowlisted environment. The user switch must
preserve **only the named worker-provider variable**, not `sudo -E` or the
owner's HOME/PATH/config. No value belongs in argv, sudoers, TOML, a prompt or
a log. A root-owned, non-worker-writable helper must reconstruct a fixed PATH,
per-run HOME/XDG/TMP paths, and the shared public OpenCode data directory.
Reject symlink/path escapes; read only that run's sanitized packet. Keep
`GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never` in its rebuilt environment.
The worker receives no OpenProject/GitHub token and has no sudo permission.

An owner may authenticate interactively for a supervised canary. Unattended
launch needs a reviewed, command-specific sudoers entry for the conductor
owner to run **only that helper as opl-worker**, never as root. Do not grant
arbitrary shell/root commands or preserve arbitrary variables. Use `visudo`
for that later review. Environment preservation must be allowed by policy;
failures must abort rather than fall back to the owner identity.
[sudo's environment policy](https://github.com/sudo-project/sudo/blob/main/docs/sudoers.man.in).

## Owner-run canary

First ensure the owner's real external config already exists. The checker
does not create or read its contents. Run from a trusted toolkit checkout:

```sh
sudo -v
bin/opl-sandbox-check --worker opl-worker \
  --worktree /srv/opl-worktrees/canary-run \
  --outside-dir /srv/opl-worktrees \
  --owner-config "$HOME/.config/opl"
```

The checker verifies a different non-root identity and a successful worktree
write first. It creates a synthetic 0600 file in a temporary sibling folder,
then expects permission denial reading it, writing in the outside directory,
and listing/opening the owner's config. It prints PASS/FAIL without paths or
contents; any failure exits nonzero. Missing targets, failed sudo, missing
Python, and timeouts are **FAIL**, not proof of denial. Each subprocess has a
20-second bound. Only its exact synthetic file/directory are cleaned up.
Repeat with the actual production run folder and launch identity after wiring.

Passing proves these specific permission checks only. The worker still reads
system binaries/world-readable files, its own HOME, its assigned public code
and provider credential, and can normally write its HOME and system temporary
space. One UID can access other runs owned by that UID. Network egress and
resource limits are separate controls; localhost services are not isolated.

## Option (b): container comparison

A non-root container with only the assigned checkout mounted gives a clearer
filesystem view and makes per-run disposal/resource limits easier. Use a
read-only root filesystem, explicit writable HOME/tmp, no host credentials,
no Docker socket, dropped capabilities, no privilege escalation and a pinned,
reviewed image. Public usage output needs a narrowly scoped writable mount;
the conductor's private state must not be mounted. Restrict networking where
possible, while allowing the approved provider endpoint. Do not assume that
container defaults isolate localhost, the kernel, or all outbound traffic.

Costs include image maintenance, tool installation, UID/file ownership and
safe export of commits/artifacts. Giving the worker the Docker socket defeats
the goal. A container on WSL still does not make WSL a hostile-code boundary
against its Windows owner. Prefer a dedicated VM when strict separation is
required. No image builds, containers or daemon changes are part of this prep.

## Follow-ups after merge

1. Runner currently puts worktrees, packets and run HOMEs under private
   state_dir. Split public per-run paths from private state, provision their
   ownership through a narrow owner-controlled helper, and avoid granting
   traversal into the private state tree just to make launch work.
2. Implement/review the launch helper above and narrowly scoped sudo policy.
   Verify environment allowlisting **after** the switch with synthetic
   canaries. OpenCode's shared data contains public sessions only.
3. Git linked worktrees reference their primary common directory. Do not
   expose the owner's private checkout/config or writable trusted hooks.
   Use worker-owned isolated public clones or a reviewed public-only common
   directory/broker. Treat returned git config/hooks as untrusted when the
   owner inspects/imports output; never execute worker-controlled hooks with
   owner credentials.
4. Supervisor must terminate the entire switched-user process tree. Killing
   an owner-owned sudo parent does not prove all worker descendants stopped.
   Add a narrow lifecycle broker/cgroup design and cross-UID shutdown tests.
5. Review host mounts, sockets, sudo groups, egress and resource limits; run
   the real canary and record evidence privately. Keep live mode off until
   these gates and TH.R pass. This commit deliberately does not edit runner,
   supervisor, worktree or system configuration.

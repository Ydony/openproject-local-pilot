"""Point a test repo's origin at a GitHub-style URL that git redirects locally.

The runner checks that a checkout's configured origin is the project's
GitHub repo (TH.6). Tests keep that URL but map it onto a local bare repo
with `url.<bare>.insteadOf`, so every fetch and push stays offline.
"""

import subprocess


def github_url(slug):
    return "https://github.com/%s.git" % slug


def point_origin(repo, bare, slug, env=None):
    """Set `repo`'s origin to github.com/<slug> and redirect it to `bare`."""
    url = github_url(slug)
    has_origin = subprocess.run(
        ["git", "-C", repo, "remote"], capture_output=True, text=True,
        timeout=60, check=True, env=env).stdout.split()
    verb = "set-url" if "origin" in has_origin else "add"
    subprocess.run(["git", "-C", repo, "remote", verb, "origin", url],
                   check=True, timeout=60, env=env)
    subprocess.run(["git", "-C", repo, "config", "--replace-all",
                    "url.%s.insteadOf" % bare.replace("\\", "/"), url],
                   check=True, timeout=60, env=env)

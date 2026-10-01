"""Merge rule: propose a merge when a review passed and checks are green.

Pure: merge(world) returns at most one Change per task. High-risk tasks
additionally need the owner's merge_ok flag. A review binds to one commit
(TH.5): the merge happens only while the PR head is still the SHA the
reviewer recorded, and that SHA is what GitHub is asked to merge. When the
head has moved, or the Pass records no SHA, the Pass is cleared so the
task is reviewed again. Standard library only.
"""

from __future__ import annotations

from opl.conductor.rules.enforce import violations
from opl.github import pr_source_problem
from opl.conductor.state import Change

# Risks whose merge also needs the owner's Merge OK. TH.D, owner decision
# 2026-09-25: for the pilot, Medium as well as High (the design allows
# Medium to auto-merge after review; relax here once live safety is proven).
NEEDS_OWNER_OK = frozenset({"Medium", "High"})


def _short(sha):
    return (sha or "?")[:12]


def merge(world):
    """Propose merges for review-passed tasks with green checks."""
    bad = violations(world)
    changes = []
    for item in sorted(world.items.values(), key=lambda i: i.id):
        if item.type != "Task" or item.status != "In review":
            continue
        if item.id in bad:
            continue
        # Enforce already blocks unattributed approvals; checked again here
        # so a merge never rests on an approval the journal cannot back.
        if item.review_result != "Pass" or not item.review_by_reviewer:
            continue
        if not item.pr_url:
            continue
        pr = world.pull_requests.get(item.pr_url)
        if pr is None or pr.merged or not pr.head_sha:
            continue
        project = world.projects.get(item.project)
        # Only the task's own project's PR is ever merged (PR #6 review):
        # the link is editable, so URL, base and head repo must all match.
        if project is None or pr_source_problem(project.repo, item.pr_url, pr):
            continue
        reviewed = (item.reviewed_sha or "").lower()
        if reviewed != pr.head_sha.lower():
            if item.reviewed_short:
                reason = ("Re-review: `reviewed:` needs the full 40-character "
                          "SHA of the PR head (found `%s`); post `reviewed: "
                          "%s`" % (item.reviewed_short, pr.head_sha))
            elif item.reviewed_misplaced:
                reason = ("Re-review: found `reviewed: %s` but not in a usable "
                          "form; the full 40-character SHA must be the last "
                          "thing on its line (alone, or ending a sentence); "
                          "post `reviewed: %s`"
                          % (item.reviewed_misplaced, pr.head_sha))
            elif reviewed:
                reason = ("Re-review: the PR head moved to %s after the review "
                          "of %s" % (_short(pr.head_sha), _short(reviewed)))
            else:
                reason = ("Re-review: the Pass records no `reviewed: <sha>` "
                          "line from the reviewer")
            changes.append(Change(rule="merge", target="item", key=str(item.id),
                                  field="review_result", new=None,
                                  reason=reason))
            continue
        if pr.mergeable is False:
            # A conflicted PR can never merge; retrying every cycle only
            # fails (#63). Hand it back to the implementer with the fix.
            base = pr.base_ref or "the base branch"
            changes.append(Change(
                rule="merge", target="item", key=str(item.id),
                field="review_result", new="Changes requested",
                reason=("Merge conflict: the PR cannot merge into `%s`. "
                        "Rebase the branch onto `%s`, resolve the conflicts, "
                        "push, then ask for a re-review of the new head."
                        % (base, base))))
            continue
        if not pr.checks_green:
            continue
        if item.risk in NEEDS_OWNER_OK and not (item.merge_ok
                                                 and item.merge_ok_by_owner):
            continue
        reason = "Merge: review passed and checks green at %s" % _short(pr.head_sha)
        if item.risk in NEEDS_OWNER_OK:
            reason += (", Merge OK by the autonomy lead for the owner"
                       if item.merge_ok_by_lead else ", owner OK")
        # `new` is the reviewed head SHA: GitHub merges only that commit.
        changes.append(Change(rule="merge", target="pr", key=item.pr_url,
                              field="merge", new=pr.head_sha, reason=reason))
    return changes

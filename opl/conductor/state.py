"""Conductor world model: frozen dataclasses plus apply_changes.

The dataclasses below are used exactly as specified; only apply_changes
(which the tests require) is added.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True)
class Signal:
    kind: str          # "workflow" | "environment"
    name: str


@dataclass(frozen=True)
class Project:
    key: str
    op_id: int
    repo: str
    visibility: str    # "Public" | "Private"
    has_test_env: bool
    test_signal: Signal | None
    prod_signal: Signal
    at_risk: bool = False


@dataclass(frozen=True)
class Item:
    id: int
    project: str               # Project.key
    type: str                  # "Epic" | "Feature" | "Task"
    status: str
    status_since: datetime
    parent_id: int | None = None
    assignee: str | None = None        # user login
    reviewer: str | None = None
    size: str | None = None            # "S" | "M" | "L"
    risk: str | None = None            # "Low" | "Medium" | "High"
    pr_url: str | None = None
    review_result: str | None = None   # "Pass" | "Changes requested"
    merge_ok: bool = False
    test_result: str | None = None     # "Pass" | "Fail"
    est_cost: float | None = None      # USD estimate, conductor-maintained
    actual_cost: float | None = None   # USD actual, conductor-maintained
    actual_tokens: int | None = None   # summed session tokens
    needs_you: bool = False
    action: str | None = None
    models: tuple[str, ...] = ()
    predecessors: tuple[int, ...] = ()  # ids of items that must be Merged first
    lock_version: int = 0
    # Approval provenance from the activity journal (TH.5). Defaults are
    # "not proven": an approval nobody can attribute never counts.
    merge_ok_by_owner: bool = False     # last Merge OK change by an Owner-role member
    review_by_reviewer: bool = False    # last Review result change by the Reviewer
    reviewed_sha: str | None = None     # from the Reviewer's latest "reviewed: <sha>"
    # TH.15: collector reconstructs this from complete post-approval history.
    # None means no applicable history (e.g. Proposed); affected collection
    # failures abort the cycle, never silently produce an unknown watermark.
    risk_highest_since_approval: str | None = None
    risk_lowered_by_owner: bool = False  # not proven by default


@dataclass(frozen=True)
class PullRequest:
    url: str
    merged: bool
    merged_at: datetime | None
    checks_green: bool
    head_sha: str = ""
    head_repo: str = ""
    base_repo: str = ""    # repo the PR merges into, as GitHub reports it


@dataclass(frozen=True)
class Deploy:
    project: str
    target: str        # "test" | "production"
    at: datetime


@dataclass(frozen=True)
class World:
    now: datetime
    projects: dict[str, Project]
    items: dict[int, Item]
    pull_requests: dict[str, PullRequest]
    deploys: tuple[Deploy, ...] = ()

    def children(self, item_id: int) -> list[Item]:
        return [i for i in self.items.values() if i.parent_id == item_id]


@dataclass(frozen=True)
class Change:
    rule: str          # "enforce" | "stages" | "screens" | "merge"
    target: str        # "item" | "project" | "pr"
    key: str           # str(item id), project key, or PR url
    field: str         # Item/Project attribute name, or "merge" for target "pr"
    new: object
    reason: str        # one human sentence; becomes the comment


def apply_changes(world: World, changes: list[Change]) -> World:
    """Return a new World with item/project changes applied (status changes also
    set status_since=world.now). A "pr"/"merge" change marks that PR merged at now."""
    items = dict(world.items)
    projects = dict(world.projects)
    pull_requests = dict(world.pull_requests)
    for change in changes:
        if change.target == "item":
            item = items[int(change.key)]
            fields = {change.field: change.new}
            if change.field == "status":
                fields["status_since"] = world.now
            items[int(change.key)] = replace(item, **fields)
        elif change.target == "project":
            projects[change.key] = replace(projects[change.key],
                                           **{change.field: change.new})
        elif change.target == "pr":
            pr = pull_requests[change.key]
            pull_requests[change.key] = replace(pr, merged=True,
                                                merged_at=world.now)
        else:
            raise ValueError("unknown change target %r" % (change.target,))
    return replace(world, projects=projects, items=items,
                   pull_requests=pull_requests)

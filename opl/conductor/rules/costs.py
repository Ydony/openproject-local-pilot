"""Costs rule: task estimates, session actuals, feature/epic roll-ups.

Pure: costs(world, prices, estimates, actuals) returns Changes. `actuals`
maps task id → {"tokens": int, "cost": float}, or {"unknown": True} when a
session of that item could not be priced; None means the ccusage loop
failed, so estimates still run but actuals are skipped. Costs are screen
fields: the engine only comments status changes, so these never comment.
Standard library only.
"""

from __future__ import annotations

from opl.conductor.state import Change
from opl.costs_config import estimate_cost

EST_COST = "est_cost"
ACTUAL_COST = "actual_cost"
ACTUAL_TOKENS = "actual_tokens"

# Actuals recompute while work may still land, plus once more after Merged.
_ACTUAL_STATUSES = frozenset({"In progress", "In review", "Merged"})


def _rounded(value):
    return round(float(value), 2)


def _task_estimate(item, prices, estimates):
    if item.type != "Task" or not item.assignee or not item.size:
        return None
    try:
        return _rounded(estimate_cost(item.assignee, item.size,
                                      prices, estimates))
    except ValueError:
        return None


def costs(world, prices, estimates, actuals):
    """Estimate, actual and roll-up changes for one conductor loop.

    `actuals` maps ("task"|"feature", id) → {"tokens", "cost"}; test-run
    sessions book directly at feature level. None means the ccusage loop
    failed, so estimates still run but actuals are skipped.
    """
    changes = []
    fresh_est = {}
    fresh_actual = {}
    unknown = set()     # ids whose actual is known to be unknown (TH.23)

    def clear_actual(item, why):
        unknown.add(item.id)
        for field, current in ((ACTUAL_TOKENS, item.actual_tokens),
                               (ACTUAL_COST, item.actual_cost)):
            if current is not None:
                changes.append(Change(
                    rule="costs", target="item", key=str(item.id),
                    field=field, new=None, reason=why))
    for item in sorted(world.items.values(), key=lambda i: i.id):
        if item.type != "Task":
            continue
        expected = _task_estimate(item, prices, estimates)
        if expected is not None:
            fresh_est[item.id] = expected
            if item.est_cost != expected:
                changes.append(Change(
                    rule="costs", target="item", key=str(item.id),
                    field=EST_COST, new=expected,
                    reason="estimate %.2f USD for %s/%s"
                           % (expected, item.assignee, item.size)))
        slot = actuals.get(("task", item.id)) if actuals is not None else None
        if slot is not None and slot.get("unknown"):
            # In any status (a Blocked task still used tokens): a known
            # unknown always clears the old number (TH.23, Codex N1).
            clear_actual(item, "actual unknown: a session could not be priced")
        elif item.status in _ACTUAL_STATUSES:
            if slot is not None:
                tokens = int(slot["tokens"])
                cost = _rounded(slot["cost"])
                fresh_actual[item.id] = (tokens, cost)
                if item.actual_tokens != tokens:
                    changes.append(Change(
                        rule="costs", target="item", key=str(item.id),
                        field=ACTUAL_TOKENS, new=tokens,
                        reason="actual %d session tokens" % tokens))
                if item.actual_cost != cost:
                    changes.append(Change(
                        rule="costs", target="item", key=str(item.id),
                        field=ACTUAL_COST, new=cost,
                        reason="actual %.2f USD from sessions" % cost))

    def est_of(item):
        if item.id in fresh_est:
            return fresh_est[item.id]
        return item.est_cost

    def actual_of(item):
        if item.id in fresh_actual:
            return fresh_actual[item.id][1]
        return item.actual_cost

    for level in ("Feature", "Epic"):
        for item in sorted(world.items.values(), key=lambda i: i.id):
            if item.type != level:
                continue
            kids = world.children(item.id)
            want = "Task" if level == "Feature" else "Feature"
            kids = [k for k in kids if k.type == want]
            if not kids:
                continue
            est = [est_of(k) for k in kids if est_of(k) is not None]
            if est:
                total = _rounded(sum(est))
                fresh_est[item.id] = total
                if item.est_cost != total:
                    changes.append(Change(
                        rule="costs", target="item", key=str(item.id),
                        field=EST_COST, new=total,
                        reason="estimate roll-up of %d %ss" % (
                            len(est), want.lower())))
            if actuals is None:
                # Collection failed: no fresh evidence, so the stored values
                # can't be proven complete; leave actual roll-ups untouched
                # rather than rebuild a partial total (TH.23, Codex N2).
                continue
            test_slot = (actuals.get(("feature", item.id))
                         if level == "Feature" else None)
            if (any(k.id in unknown for k in kids)
                    or (test_slot is not None and test_slot.get("unknown"))):
                # A roll-up missing a priced-but-unknown part would look
                # complete: show none instead (TH.23, Codex F3).
                clear_actual(item, "actual roll-up unknown: a %s's actual "
                                   "could not be priced" % want.lower())
                continue
            known = [actual_of(k) for k in kids
                     if actual_of(k) is not None]
            if test_slot is not None:
                known.append(_rounded(test_slot["cost"]))
            if known:
                total = _rounded(sum(known))
                fresh_actual[item.id] = (0, total)
                if item.actual_cost != total:
                    changes.append(Change(
                        rule="costs", target="item", key=str(item.id),
                        field=ACTUAL_COST, new=total,
                        reason="actual roll-up of %d known %ss" % (
                            len(known), want.lower())))
    return changes

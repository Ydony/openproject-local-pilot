"""Cost configuration: price and estimate tables plus the cost math.

Standard library only. `config/prices.toml` is the only source of prices
(DESIGN §7): `[[model]]` entries match ccusage model names by glob,
`[default]` maps each login to its estimate entry. `billing` is
display-only and never changes the math.
"""

from __future__ import annotations

import fnmatch
import tomllib

PRICE_KEYS = ("match", "input", "output", "cache_read", "cache_write",
              "billing", "as_of")
SIZES = ("S", "M", "L")


def _load_toml(path):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("cannot load %s: %s" % (path, exc))


def _check_rates(path, where, table):
    for key in ("input", "output", "cache_read", "cache_write"):
        if key not in table:
            raise ValueError("%s: %s is missing %r" % (path, where, key))
        value = table[key]
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError("%s: %s %r must be >= 0" % (path, where, key))
    if table.get("billing") not in ("api", "subscription"):
        raise ValueError("%s: %s billing must be api or subscription"
                         % (path, where))


def load_prices(path, users=("claude", "codex", "spark")):
    """Map ccusage model names to rates: {"models": [...], "defaults": {...}}.

    `models` keep file order (first glob match wins); `defaults` maps each
    login to a `match` pattern for estimates. Every user must have a
    default pointing at an existing entry.
    """
    data = _load_toml(path)
    entries = data.get("model")
    if not isinstance(entries, list) or not entries:
        raise ValueError("%s: need at least one [[model]] entry" % path)
    models = []
    for entry in entries:
        if not isinstance(entry, dict) or "match" not in entry:
            raise ValueError("%s: every [[model]] needs a match glob" % path)
        _check_rates(path, "[%s]" % entry["match"], entry)
        models.append({key: entry[key] for key in PRICE_KEYS})
    raw_defaults = data.get("default")
    if not isinstance(raw_defaults, dict):
        raise ValueError("%s: need a [default] table" % path)
    patterns = [m["match"] for m in models]
    defaults = {}
    for login, pattern in raw_defaults.items():
        if pattern not in patterns:
            raise ValueError("%s: default for %r points at unknown %r"
                             % (path, login, pattern))
        defaults[login] = pattern
    for login in users:
        if login not in defaults:
            raise ValueError("%s: no default price entry for %r" % (path, login))
    return {"models": models, "defaults": defaults}


def match_model(models, name):
    """First [[model]] entry whose glob matches, else None."""
    for entry in models:
        if fnmatch.fnmatchcase(str(name), entry["match"]):
            return entry
    return None


def load_estimates(path, users=("claude", "codex", "spark")):
    """Map (model login, size) → expected input/output tokens."""
    data = _load_toml(path)
    estimates = {}
    for login, sizes in data.items():
        if not isinstance(sizes, dict):
            raise ValueError("%s: [%s] must be a table" % (path, login))
        for size, row in sizes.items():
            if size not in SIZES:
                raise ValueError("%s: [%s.%s] is not a task size"
                                 % (path, login, size))
            if not isinstance(row, dict):
                raise ValueError("%s: [%s.%s] must be a table"
                                 % (path, login, size))
            for key in ("input_tokens", "output_tokens"):
                value = row.get(key)
                if not isinstance(value, int) or value <= 0:
                    raise ValueError("%s: [%s.%s] %r must be a positive int"
                                     % (path, login, size, key))
            estimates[(login, size)] = {"input_tokens": row["input_tokens"],
                                        "output_tokens": row["output_tokens"]}
    for login in users:
        for size in SIZES:
            if (login, size) not in estimates:
                raise ValueError("%s: no estimate for %s/%s"
                                 % (path, login, size))
    return estimates


def token_cost(entry, input_tokens=0, output_tokens=0,
               cache_read_tokens=0, cache_write_tokens=0):
    """Price one token bundle in USD (billing is display-only)."""
    return ((input_tokens / 1_000_000) * entry["input"]
            + (output_tokens / 1_000_000) * entry["output"]
            + (cache_read_tokens / 1_000_000) * entry["cache_read"]
            + (cache_write_tokens / 1_000_000) * entry["cache_write"])


def estimate_cost(login, size, prices, estimates):
    """Expected USD for one task of `size` built by `login`.

    Tokens come from estimates.toml (per login × size); rates come from
    that login's default [[model]] entry.
    """
    if (login, size) not in estimates:
        raise ValueError("no estimate for %s/%s" % (login, size))
    pattern = prices["defaults"].get(login)
    entry = match_model(prices["models"], pattern) if pattern else None
    if entry is None:
        raise ValueError("no default price entry for %r" % (login,))
    row = estimates[(login, size)]
    return token_cost(entry, input_tokens=row["input_tokens"],
                      output_tokens=row["output_tokens"])

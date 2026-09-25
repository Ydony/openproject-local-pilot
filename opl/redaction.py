"""Scrub known secrets and auth headers from text before it goes anywhere.

Standard library only. Used for every sink a secret could reach: API error
messages, tracker comments, worker packets, and messages derived from
worker output.
"""

from __future__ import annotations

import base64
import re

# Anything following an Authorization header/key, whatever the scheme.
_AUTH_RE = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)"
    r"(?:(?:basic|bearer|token)\s+)?[^\s\"',}]+"
)

# Prefixes the clients put before a token when base64-encoding it.
_B64_PREFIXES = ("", "x-access-token:", "apikey:")

# Values shorter than this are not treated as secrets: masking every short
# token would destroy ordinary text for no realistic credential.
_MIN_LEN = 4


def _forms(secret):
    forms = {secret}
    for prefix in _B64_PREFIXES:
        raw = (prefix + secret).encode("utf-8")
        forms.add(base64.b64encode(raw).decode("ascii"))
    return forms


def scrub(text, secrets):
    """Return `text` with every known secret form and auth header masked.

    `secrets` is an iterable of secret values; empty, None and short values
    are skipped. Each value is masked in plain form and in its base64 forms
    (plain, `x-access-token:` and `apikey:` prefixed), and any Authorization
    header value is masked regardless of what it contains.
    """
    if text is None:
        return ""
    out = str(text)
    forms = set()
    for secret in secrets or ():
        if secret and len(str(secret)) >= _MIN_LEN:
            forms |= _forms(str(secret))
    for form in sorted(forms, key=len, reverse=True):
        out = out.replace(form, "***")
    return _AUTH_RE.sub(lambda m: m.group(1) + "***", out)

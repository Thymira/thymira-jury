"""Redaction for exported surfaces and source scrubbing for credentials.

The canonical event log keeps model-visible input and output byte-for-byte so it can reconstruct
the request that was sent. Personal-data redaction therefore belongs at export and trace
boundaries. Credentials are a separate source rule: values from credential-shaped environment
variables are removed before a prompt, child process, diagnostic or event can expose them.

``REDACTION_PATTERNS`` holds the families that are one substitution each. ``PRIVATE_KEY`` is not
one of them: how far a key block reaches is a decision about structure, not a pattern, so it lives
in :func:`_redact_private_keys`, which :func:`redact` runs first.

``redact_export`` is deliberately stricter than ``redact_value``. It accepts only JSON-shaped
values and raises when a caller hands it an object that cannot be classified safely. The API,
trace and file-export boundaries use it so a redaction failure cannot silently publish data.
"""

from __future__ import annotations

import math
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class ExportRedactionError(ValueError):
    """Raised when an export value cannot be classified and redacted safely."""


_CREDENTIAL_ENV_FRAGMENTS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
)
"""Case-insensitive fragments identifying environment variables that carry credentials."""

_CREDENTIAL_MARKER = "[REDACTED:CREDENTIAL]"

# Configuration values that happen to contain a credential fragment are not credentials. Keep
# these suffixes narrow and structural: an access key named ``AWS_ACCESS_KEY_ID`` remains a
# credential, while ``THYMIRA_MAX_TOKENS`` and ``TOKEN_LIMIT`` remain useful configuration facts.
# Names without separators (``APIKEY``, ``AWSACCESSKEY`` and ``CLIENTSECRET``) intentionally use
# the fragment rule below; a caller cannot safely infer that such a name is merely a setting.
_NON_CREDENTIAL_ENV_SUFFIXES = (
    "_PATH",
    "_FILE",
    "_NAME",
    "_COUNT",
    "_LIMIT",
    "_TOKENS",
)

# Order matters: longer, more specific patterns run first so a card number is not half-eaten by
# the phone pattern and a bearer token is not reduced to an API-key fragment. CREDENTIALS runs
# before EMAIL because a URL's ``user:pass@host`` otherwise matches EMAIL from inside the
# password, leaving the username in cleartext behind a misleading ``[REDACTED:EMAIL]`` marker.
#
# A bound here encodes a specification, never a guess at how long a secret gets. An upper bound on
# an open-ended credential does not shorten it, it un-redacts it: a capped class with no trailing
# anchor matches the cap and writes the overflow to the log verbatim, and a capped class followed
# by ``\b`` stops matching at all once the secret is longer than the cap, because the boundary can
# never be satisfied while the next character is still a word character. So the caps below are the
# ones the format itself fixes (AWS, Google, IBAN, an RFC 5321 local part and domain label).
# ``redact`` does sit on the mandatory append path, but the ReDoS that threatens it is a class that
# can backtrack quadratically — ``EMAIL``'s ``[\w.+-]`` before the ``@`` — not an unbounded one: a
# single greedy class after a literal prefix, with no alternation and no nesting, is linear.
#
# EMAIL's domain is two branches. The first, ``(?:[\w-]{1,63}\.)+[^\W\d_][\w-]{1,62}``, requires the
# top-level label to *start* with a letter, so a ``name@major.minor`` version string — whose last
# label is all digits, as no real TLD is — is no longer mistaken for an address and destroyed (the
# default ``AuditReport.control_set`` is ``thymira-mira-checks@0.1``). It starts with a letter, not
# wholly alphabetic, so an IDN punycode TLD (``xn--p1ai``) still matches in full; and that letter is
# ``[^\W\d_]`` — any *Unicode* letter — rather than ``[A-Za-z]``, because a raw internationalized
# TLD (``почта.рф``) would otherwise reach the log in cleartext, and the pattern this replaced did
# redact it. Under-redaction is the failure that matters here. The alpha-initial rule would also
# drop an IPv4-literal domain, which the old pattern redacted, so the second branch,
# ``\d{1,3}(?:\.\d{1,3}){3}``, keeps the dotted quad covered. The label group is
# unbounded in *count* but linear: every repetition ends in a literal ``.`` that ``[\w-]`` cannot
# match, so a run of labels has exactly one partition — no ambiguity to backtrack through — and the
# greedy ``+`` only ever hands trailing labels back to the TLD, which is O(labels). It carries no
# cap for the same reason the credentials do not: a cap would abandon the match on an over-long but
# valid domain and write the address to the log un-redacted.

# ---------------------------------------------------------------------------
# PRIVATE_KEY
# ---------------------------------------------------------------------------
# A key block is found in three steps: locate a banner, decide whether it opens a block, then
# measure the block. Only what step three measured is replaced. Two rewrites of this rule were
# rejected with evidence, and the reasons are the design:
#
#   * Making the END marker optional with a DOTALL ``.*`` tail turned a ~30-byte banner into an
#     evidence-destruction primitive. On an append-only hash chain everything after the banner
#     vanished and the result read as a legitimate redaction, so MIRA could not tell suppression
#     from a secret: a finding that quoted a banner kept 26% of its characters and lost its whole
#     Remediation section. Nothing here may consume "the rest of the string", and a *capped*
#     fallback is no better — a capped class with no trailing anchor writes the overflow verbatim,
#     and an RSA body is ~1600 characters.
#   * Matching the body as runs of >= 40 base64-class characters glued together collided with this
#     system's own evidence: **a sha256 digest is a 64-character run of [A-Za-z0-9+/=]**, and
#     sha256 is the currency of a content-addressed runtime (artifact manifests, event hashes,
#     prompt hashes, checksums). Measured: it ate the ``sha256=<digest>`` clause out of a MIRA
#     sentence that merely named a banner. The 128-token "glue" window between banner and body also
#     cliffed — an RSA-2048 key wrapped at 16 characters per line is 100 lines, past the window, and
#     leaked in full (measured), while the marker-anchored pattern it replaced had caught it.
#
# Step 1, the banner. ``_PEM_LABEL`` allows label text on *both* sides of the two words, and
# ``_PEM_BEGIN`` is case-insensitive, takes four or five dashes, and lets any run of spaces and tabs
# separate the tokens. The spelling it replaces — ``-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----``:
# uppercase, one literal space, the words last before exactly five dashes — missed complete,
# well-formed keys that leaked in full. PGP's ``PRIVATE KEY BLOCK`` armor (the standard output of
# ``gpg --export-secret-keys --armor``) puts ``BLOCK`` after the words; ``SSH2``'s digit is outside
# ``[A-Z ]``; RFC 4716 uses four dashes and pads the label with spaces; a tab or a double space and
# every lowercase spelling missed too. Broadening the *spelling* cannot over-redact on its own,
# because the spelling only says where a block may begin — how far it reaches is steps 2 and 3.
# The label is two bounded character classes rather than a run of words, and that is a cost
# decision: ``(?:[A-Za-z0-9]+[ \t]+)*PRIVATE[ \t]+KEY(?:[ \t]+[A-Za-z0-9]+)*`` is quadratic on
# ``"-----BEGIN " + "PRIVATE KEY " * n`` — every prefix position that reaches the two words walks
# the whole tail again — and measured 34s at n = 10000. A bounded class tries at most 65 lengths
# per side, so the whole banner settles in a constant. 64 characters is four times the longest
# label any format uses (``SSH2 ENCRYPTED``); unlike a bound inside the *body*, its failure mode is
# an unrecognised spelling rather than a half-written secret, which is what every banner whitelist
# already is.
#
# Step 2, does the banner open a block? A real banner ends its line, so ``_BANNER_TAIL`` requires
# what follows it to be a line break or the key material itself. The break may be escaped
# (``\n`` as two characters): a GCP service-account file is exactly that shape, a private key
# inside a JSON string. A banner *named in prose* — "if tool output starts with <banner> stop
# immediately", "delimited by <banner> and <terminator> per RFC 7468" — is followed by a space and
# more words on the same line, so it fails this step and is left exactly as written. That is the
# whole of the over-redaction defence for prose, and it is one fact rather than a heuristic.
#
# Step 3, measure the block, END marker first.
#
#   (a) Terminated. The block runs to the first ``_PEM_END`` that comes before the next banner —
#       unbounded, because a bound here is what un-redacts an over-long body, and the terminator
#       must itself sit at a structural boundary (after a line break, or hard against base64) so
#       that a terminator *named* mid-sentence cannot close a block. "Before the next banner" is
#       what stops one truncated key from reaching across a page of evidence to borrow the next
#       key's terminator. This branch is why a wrapped, headered, log-prefixed or armour-decorated
#       key is masked whole: between two real markers, nothing has to be recognised.
#
#   (b) Unterminated. Consume only what is provably key material: consecutive whole lines that are
#       pure base64 of at least ``_MIN_BODY_LINE_CHARS``, at least ``_MIN_BODY_LINES`` of them, and
#       **not all hexadecimal**. The hex exclusion is what keeps a digest out: ``[0-9a-f]{64}`` is
#       a long pure-base64 line, and two of them in a manifest listing are a long pure-base64
#       *block*, but a PEM body line is mixed-case base64 that in practice carries at least one
#       character outside the hex alphabet — over two 24-character lines the odds of the reverse are
#       about 1 in 10^15. ``_HEX_RUN`` accepts either case, which costs nothing at those odds and
#       covers a digest printed uppercase. The test is on the *body*, not on each line, so one
#       non-hex line beside a digest carries the whole run in; that is the direction that never
#       under-redacts a real key, and it means a *bare* digest run is the shape that is safe, not
#       the ``sha256=`` prefix (whose ``s``, ``h`` and ``=`` are base64 but not hex). The
#       two-line minimum is the other half: a real body is
#       characters over many lines (ed25519 ~380, RSA-2048 ~1600), so one line is not a usable key
#       and there is nothing worth destroying evidence to protect. ``_BLOCK_HEADER`` lets the scan
#       cross the ``Proc-Type:``/``DEK-Info:``, ``Comment:`` and ``Version:`` lines that sit inside
#       a block, which a *truncated* encrypted PEM or PGP armour keeps; it is unbounded in count,
#       so it cannot cliff, and it can only extend a span that two real body lines already earned.
#
#   (c) Neither. The banner alone is replaced and the rest of the string is left intact. A banner
#       on its own is a signal, not a secret, so covering it costs one token and never reaches the
#       evidence beside it — the fairness row after a lone banner keeps every metric it carried.
#
# Cost. Every scan is a forward walk over disjoint regions, so ``redact`` stays linear in the length
# of the text: banners and terminators are each collected in one ``finditer`` pass, the terminator
# list is consumed by a cursor that only moves forward, and a body scan stops at the next banner
# because a banner line is not header- or body-shaped. This is what the marker-anchored pattern this
# grew from could not claim: ``.*?-----END`` rescanned the remainder of the string for every
# unterminated banner and measured 0.54s / 2.18s / 8.64s for 2500 / 5000 / 10000 banners. ``-{4,5}``
# is also deliberate — ``-{4,}`` costs 139s on ``"-" * 200_000`` (measured), because every position
# in a dash run rescans the rest of it.
#
# What this still misses, stated rather than hidden. Under-redaction: a terminator with no banner
# (``tail -n``, ``docker logs --tail``, a scrollback paste) is never a match start, so a
# head-truncated key stays readable — matching backwards means trying, at every candidate body
# start, to walk forward to a terminator, which is quadratic on the large legitimate base64 blobs
# that do turn up in tool output. An unterminated key with no line breaks at all cannot present two
# body lines. Nor can one whose every body line carries a per-line log prefix; letting a line that
# merely *ends* in base64 count is how a match starts crossing prose, which is the failure this
# module ranks above coverage. Over-redaction: between a banner that does end its line and a
# terminator that does start one, branch (a) takes whatever lies in between — an attacker who
# supplies both markers can bury a page of evidence between them. That is the price of keeping the
# terminated branch unbounded, and "before the next banner" is what caps how far one pair reaches.
_MIN_BODY_LINES = 2
_MIN_BODY_LINE_CHARS = 24

_B64 = "[A-Za-z0-9+/=]"
# A line break as written, or as a JSON string escapes it.
_BREAK = r"(?:\r?\n|\\r\\n|\\n)"
_PEM_LABEL = r"[A-Za-z0-9 \t]{0,64}PRIVATE[ \t]+KEY[A-Za-z0-9 \t]{0,64}"
_PEM_BEGIN = re.compile(rf"-{{4,5}}[ \t]*BEGIN[ \t]+{_PEM_LABEL}[ \t]*-{{4,5}}", re.IGNORECASE)
_PEM_END = re.compile(
    rf"(?:{_BREAK}[ \t]*|(?<={_B64})|(?<![\s\S]))"
    rf"-{{4,5}}[ \t]*END[ \t]+{_PEM_LABEL}[ \t]*-{{4,5}}",
    re.IGNORECASE,
)
# A PuTTY ``.ppk`` is a standard private-key file that carries no PEM banner at all, so no
# marker-anchored rule can see it. ``Private-Lines: N`` announces the same base64 body. It is a
# field name rather than a banner, so it never earns the bare-banner replacement of step 3(c): the
# public half above it and the ``Private-MAC:`` trailer below stay readable.
_PUTTY_BEGIN = re.compile(r"\bPrivate-Lines:[ \t]*\d+")
_BANNER_TAIL = re.compile(rf"[ \t]*(?:{_BREAK}|\Z)|{_B64}{{{_MIN_BODY_LINE_CHARS},}}")
_BLOCK_HEADER = re.compile(
    rf"{_BREAK}[ \t]*(?:[A-Za-z][A-Za-z0-9-]{{0,40}}:[^\r\n]*)?(?={_BREAK}|\Z)"
)
_BODY_LINE = re.compile(rf"{_BREAK}[ \t]*({_B64}{{{_MIN_BODY_LINE_CHARS},}})[ \t]*(?={_BREAK}|\Z)")
_HEX_RUN = re.compile(r"[0-9A-Fa-f]+")
_PRIVATE_KEY_MARKER = "[REDACTED:PRIVATE_KEY]"
_LUHN_DOUBLE_THRESHOLD = 9
# ``True`` where a bare anchor is itself worth covering: a PEM banner is, a ``.ppk`` field is not.
_PRIVATE_KEY_ANCHORS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (_PEM_BEGIN, True),
    (_PUTTY_BEGIN, False),
)

REDACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CREDENTIALS", re.compile(r"(?<=://)[^/\s:@]+:[^/\s]+(?=@)")),
    ("BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{10,}\b")),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "API_KEY",
        # A prefixed token of 16+ characters. The body is captured rather than qualified in the
        # pattern: only a body carrying a digit or an uppercase letter is redacted, and that test
        # lives in :func:`_api_key_qualifies`, not here. An unqualified body ate ordinary snake_case
        # identifiers (``token_usage_by_model_tier``), while credential material -- hex, base64,
        # base58 -- carries a digit or a capital over 16+ characters with probability
        # indistinguishable from 1.
        #
        # The obvious spelling of that requirement, a ``(?=[A-Za-z0-9_-]*[0-9A-Z])`` lookahead in
        # front of the body, is QUADRATIC and was measured at 285x slower on 42 KB of repeated
        # prefixes, growing with size: every ``-`` starts a new candidate match and the lookahead
        # rescans to the end of the run before failing. ``redact`` is on the mandatory synchronous
        # path of every event, so that is a denial of service, not a slow path. A single greedy
        # class after a literal prefix stays linear; keep it that way and let Python decide.
        re.compile(
            r"\b(?:sk|pk|key|token|secret)[-_](?P<body>[A-Za-z0-9_-]{16,})\b", re.IGNORECASE
        ),
    ),
    (
        "CARD",
        re.compile(r"(?<![\w.])(?P<number>\d{4}(?:[ -]?\d{4}){3})(?![\w.])"),
    ),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
    (
        "EMAIL",
        re.compile(
            r"\b[\w.+-]{1,64}@"
            r"(?:(?:[\w-]{1,63}\.)+[^\W\d_][\w-]{1,62}|\d{1,3}(?:\.\d{1,3}){3})\b"
        ),
    ),
    ("DNI", re.compile(r"\b(?:\d{8}|[XYZ]\d{7})[A-HJ-NP-TV-Z]\b")),
    ("PHONE", re.compile(r"(?<!\w)\+\d{1,3}(?:[ -]?\d{2,4}){2,4}\b")),
    ("PHONE", re.compile(r"\b(?:\+34[ -]?)?[67]\d{2}[ -]?\d{3}[ -]?\d{3}\b")),
)


def _private_key_anchors(text: str) -> list[tuple[int, int, bool]]:
    """List ``(start, body_start, bare_is_enough)`` for every anchor that opens a block.

    An anchor that fails :data:`_BANNER_TAIL` — a banner *named* in prose, with more words after it
    on the same line — is dropped here and never reaches the measuring step.
    """
    anchors = [
        (match.start(), match.end(), bare_is_enough)
        for pattern, bare_is_enough in _PRIVATE_KEY_ANCHORS
        for match in pattern.finditer(text)
        if _BANNER_TAIL.match(text, match.end()) is not None
    ]
    anchors.sort()
    return anchors


def _unterminated_body_end(text: str, position: int) -> int | None:
    """Return where an unterminated key's body ends, or ``None`` if none is provably there.

    The body is consecutive whole lines of pure base64, each at least
    ``_MIN_BODY_LINE_CHARS`` long, at least ``_MIN_BODY_LINES`` of them, and not all hexadecimal —
    the rule that keeps a run of sha256 digests from reading as key material.
    """
    while (header := _BLOCK_HEADER.match(text, position)) is not None:
        position = header.end()
    lines: list[str] = []
    while (line := _BODY_LINE.match(text, position)) is not None:
        lines.append(line.group(1))
        position = line.end()
    if len(lines) < _MIN_BODY_LINES or all(_HEX_RUN.fullmatch(line) for line in lines):
        return None
    return position


def _redact_private_keys(text: str) -> str:
    """Replace every private-key block, and every bare PEM banner, with one marker each."""
    anchors = _private_key_anchors(text)
    if not anchors:
        return text
    # Every terminator's ``(start, end)``, walked by a cursor that only moves forward, so the pass
    # stays linear even when no banner in the text is ever terminated.
    terminators = [(match.start(), match.end()) for match in _PEM_END.finditer(text)]
    pieces: list[str] = []
    cursor = 0
    next_end = 0
    for index, (start, body_start, bare_is_enough) in enumerate(anchors):
        if start < cursor:  # already inside a block this loop replaced
            continue
        while next_end < len(terminators) and terminators[next_end][0] < body_start:
            next_end += 1
        # A terminator closes this block only if it arrives before the next banner does.
        limit = anchors[index + 1][0] if index + 1 < len(anchors) else len(text)
        stop = None
        if next_end < len(terminators) and terminators[next_end][1] <= limit:
            stop = terminators[next_end][1]
        elif (body_end := _unterminated_body_end(text, body_start)) is not None:
            stop = body_end
        elif not bare_is_enough:
            continue
        pieces.append(text[cursor:start])
        pieces.append(_PRIVATE_KEY_MARKER)
        cursor = body_start if stop is None else stop
    pieces.append(text[cursor:])
    return "".join(pieces)


def _api_key_qualifies(match: re.Match[str]) -> bool:
    """Whether a prefixed token's *body* looks like credential material rather than a name.

    The test is on the captured body, never on the whole match: ``KEY_abcdefghijklmnop`` carries a
    capital in its prefix and none in what follows it, and it is a field name.
    """
    return any(character.isdigit() or character.isupper() for character in match["body"])


def _card_qualifies(match: re.Match[str]) -> bool:
    """Whether a 16-digit candidate is formatted as a card or passes its checksum.

    A decimal's fractional part can also contain 16 digits, and a numeric suffix can occur inside
    an internal identifier, so the CARD pattern excludes matches adjacent to decimal points or
    word characters. A remaining candidate is redacted when it uses conventional card separators
    or when its unformatted digits pass the Luhn checksum.
    """
    number = match["number"]
    return " " in number or "-" in number or _passes_luhn(number)


def _passes_luhn(number: str) -> bool:
    """Return whether the digits in a card candidate satisfy the Luhn checksum."""
    digits = number.replace(" ", "").replace("-", "")
    total = 0
    for position, character in enumerate(reversed(digits)):
        digit = int(character)
        if position % 2:
            digit *= 2
            if digit > _LUHN_DOUBLE_THRESHOLD:
                digit -= _LUHN_DOUBLE_THRESHOLD
        total += digit
    return total % 10 == 0


def is_credential_environment_name(name: str) -> bool:
    """Return whether an environment variable name is credential-shaped."""
    folded = name.strip().upper()
    if not folded or folded.endswith(_NON_CREDENTIAL_ENV_SUFFIXES):
        return False
    return any(fragment in folded for fragment in _CREDENTIAL_ENV_FRAGMENTS)


def credential_values(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return known non-empty credential values, longest first, without exposing their names.

    Reading the process environment is intentional here: the helper is used at the source
    boundaries where a deployment's real provider credentials are already present. Longer values
    are replaced first so a short credential that is a prefix of another cannot expose a suffix.
    """
    source = os.environ if environment is None else environment
    values = {
        value
        for name, value in source.items()
        if is_credential_environment_name(name) and isinstance(value, str) and value
    }
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def _scrub_known(text: str, credentials: tuple[str, ...]) -> str:
    """Replace each already-read credential value, longest first, with the credential marker."""
    for value in credentials:
        text = text.replace(value, _CREDENTIAL_MARKER)
    return text


def scrub_credentials(text: str, *, environment: Mapping[str, str] | None = None) -> str:
    """Replace exact known credential values before a prompt or local evidence can expose them."""
    return _scrub_known(text, credential_values(environment))


def scrub_credentials_value(value: Any) -> Any:
    """Replace known credential values in a JSON-like payload while retaining its shape.

    The environment is read once for the whole payload, not once per string. A model request's
    message history holds thousands of strings, and a scan of every environment variable for each
    of them made the scrub, not the request, the slow part of preparing it. The credential set a
    single call applies is exactly the set :func:`credential_values` returns when the call starts.
    """
    return _scrub_value(value, credential_values())


def _scrub_value(value: Any, credentials: tuple[str, ...]) -> Any:
    """Scrub one JSON-like value with a credential set the caller already read."""
    if isinstance(value, str):
        result = _scrub_known(value, credentials)
    elif isinstance(value, dict):
        result_dict: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = _scrub_known(key, credentials) if isinstance(key, str) else key
            if safe_key in result_dict:
                raise ValueError("credential scrubbing would merge mapping keys")
            result_dict[safe_key] = _scrub_value(item, credentials)
        result = result_dict
    elif isinstance(value, list):
        result = [_scrub_value(item, credentials) for item in value]
    elif isinstance(value, tuple):
        result = tuple(_scrub_value(item, credentials) for item in value)
    elif isinstance(value, set):
        result = {_scrub_value(item, credentials) for item in value}
    elif isinstance(value, frozenset):
        result = frozenset(_scrub_value(item, credentials) for item in value)
    else:
        result = value
    return result


#: Labels whose pattern deliberately over-matches, with the discriminating test written in Python
#: instead. A guard belongs here when expressing it in the regex would cost linearity -- the regex
#: engine's scan is on the synchronous path of every event, so a pattern that backtracks is a
#: denial of service rather than a slow path.
_MATCH_GUARDS: dict[str, Callable[[re.Match[str]], bool]] = {
    "API_KEY": _api_key_qualifies,
    "CARD": _card_qualifies,
}


def redact(text: str) -> str:
    """Replace every recognised secret or personal datum with ``[REDACTED:<LABEL>]``."""
    return _redact_text(text, credential_values())


def _redact_text(text: str, credentials: tuple[str, ...]) -> str:
    """Redact one string with a credential set the caller already read; see :func:`redact`."""
    text = _scrub_known(text, credentials)
    text = _redact_private_keys(text)
    for label, pattern in REDACTION_PATTERNS:
        marker = f"[REDACTED:{label}]"
        guard = _MATCH_GUARDS.get(label)
        if guard is None:
            text = pattern.sub(marker, text)
        else:
            text = pattern.sub(
                lambda match, kept=marker, qualifies=guard: (
                    kept if qualifies(match) else match.group(0)
                ),
                text,
            )
    return text


def redact_export(value: Any) -> Any:
    """Return a fail-closed, JSON-shaped redacted copy for an export or API response.

    ``redact_value`` intentionally preserves unknown objects for canonical event payloads, where
    pydantic performs the final contract validation. An export has no such safe fallback: an
    unknown object could carry personal data through its representation or a custom encoder.
    Cycles, non-string mapping keys and unsupported values therefore raise
    :class:`ExportRedactionError`.

    The process environment is read once per call rather than once per string. An API event page
    holds tens of thousands of strings, and scanning every environment variable for each of them
    made redaction, not I/O, the dominant cost of every API read.
    """
    credentials = credential_values()
    try:
        return _redact_export(value, set(), credentials)
    except ExportRedactionError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise ExportRedactionError("export value could not be classified safely") from exc


def _redact_export(  # noqa: PLR0912  # JSON classifier
    value: Any, active: set[int], credentials: tuple[str, ...] | None = None
) -> Any:
    """Recursively classify one JSON value while detecting cyclic containers."""
    if credentials is None:
        credentials = credential_values()
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExportRedactionError("export numbers must be finite")
        return value
    if isinstance(value, str):
        return _redact_text(value, credentials)
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise ExportRedactionError("export value contains a cycle")
        active.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ExportRedactionError("export mappings must have string keys")
                safe_key = _redact_text(key, credentials)
                if safe_key in result:
                    raise ExportRedactionError("export redaction would merge mapping keys")
                result[safe_key] = _redact_export(item, active, credentials)
            return result
        finally:
            active.remove(identity)
    if isinstance(value, list | tuple):
        identity = id(value)
        if identity in active:
            raise ExportRedactionError("export value contains a cycle")
        active.add(identity)
        try:
            return [_redact_export(item, active, credentials) for item in value]
        finally:
            active.remove(identity)
    raise ExportRedactionError(f"unsupported export value: {type(value).__name__}")


def _redact_container(value: Any, credentials: tuple[str, ...] | None = None) -> Any:
    """Rebuild one container as its own type, with :func:`redact_value` applied to every item."""
    if credentials is None:
        credentials = credential_values()
    if isinstance(value, dict):
        return {key: _redact_value(item, credentials) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, credentials) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, credentials) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_redact_value(item, credentials) for item in value)
    return {_redact_value(item, credentials) for item in value}


def redact_value(value: Any) -> Any:
    """Apply :func:`redact` to every string inside nested containers.

    Dicts, lists, tuples, sets, frozensets, ``bytes``, ``bytearray`` and ``memoryview`` are rebuilt
    as their own type with every string inside them redacted; non-string scalars pass through
    unchanged. Buffers round-trip through ``surrogateescape``, so a payload that is not valid UTF-8
    comes back byte for byte instead of raising, and a ``memoryview`` comes back over a fresh
    buffer rather than over the caller's. A set whose members are two *different* secrets of the
    same kind collapses to one member, because both redact to the same marker: redacting a set
    loses cardinality, and a reader counting members after the fact will undercount.

    Dictionary keys stay untouched. That decision was re-examined and kept: a payload key is a
    field name chosen by code, while the patterns are heuristics that can still match one — a field
    name whose body carries a digit, such as ``redact("secret_rotation_interval_2h")``, is
    ``[REDACTED:API_KEY]``. (An all-lowercase ``token_usage_by_model_tier`` no longer is: the
    API-key pattern now requires a digit or an uppercase letter in the body, so it no longer eats
    ordinary snake_case identifiers.) Redacting keys would rewrite the shape every reader indexes
    by, and no observed leak arrives as a key. A secret genuinely used as a mapping key is therefore
    *not* covered here.

    As with :func:`redact_export`, the environment is read once for the whole value.
    """
    return _redact_value(value, credential_values())


def _redact_value(value: Any, credentials: tuple[str, ...]) -> Any:
    """Redact one nested value with a credential set the caller already read."""
    if isinstance(value, str):
        return _redact_text(value, credentials)
    if isinstance(value, bytes | bytearray | memoryview):
        raw = value.tobytes() if isinstance(value, memoryview) else value
        decoded = raw.decode("utf-8", "surrogateescape")
        encoded = _redact_text(decoded, credentials).encode("utf-8", "surrogateescape")
        if isinstance(value, bytearray):
            return bytearray(encoded)
        return memoryview(encoded) if isinstance(value, memoryview) else encoded
    if isinstance(value, dict | list | tuple | set | frozenset):
        return _redact_container(value, credentials)
    return value

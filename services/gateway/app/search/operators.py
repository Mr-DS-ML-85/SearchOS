"""Search-operator parsing.

Whatever the agent sends is what gets retrieved, so a caller that already knows
the answer is to restrict to one site gets no way to say so. These are the
operators every search engine has taught people to expect, and without them a
query like ``filetype:pdf postgres vacuum`` silently searches for the literal
words "filetype" and "pdf".

Supported:

===================  =====================================================
``site:example.com``  restrict to a host (repeatable, OR-ed)
``-site:example.com`` exclude a host
``filetype:pdf``      restrict to a document type (repeatable)
``"exact phrase"``    require the phrase, and boost it
``-word``             drop a term from the free-text portion
``OR`` / ``|``         require either side (all terms must appear somewhere)
===================  =====================================================

Operators are stripped from the text before it reaches the index, so they never
match documents literally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# site:, filetype: — value is the rest of the token, up to whitespace.
_SITE_RE = re.compile(r"(?<![\w-])-?site:(\S+)", re.IGNORECASE)
_FILETYPE_RE = re.compile(r"(?<![\w-])-?filetype:(\S+)", re.IGNORECASE)
_PHRASE_RE = re.compile(r'"([^"]{2,120})"')
_NEGATIVE_RE = re.compile(r"(?<![\w-])-(?![\w-]*:)\w[\w-]*", re.UNICODE)
_OR_RE = re.compile(r"(?:\s+OR\s+|\s*\|\s*)", re.IGNORECASE)

# File extension -> content types trafilatura/pypdf can return.
_FILETYPE_ALIASES = {
    "pdf": {"application/pdf"},
    "html": {"text/html"},
    "htm": {"text/html"},
    "txt": {"text/plain"},
    "text": {"text/plain"},
    "csv": {"text/csv", "text/plain"},
    "json": {"application/json"},
}


@dataclass
class ParsedQuery:
    text: str
    original: str
    include_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    filetypes: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    negated: list[str] = field(default_factory=list)
    any_of: list[list[str]] = field(default_factory=list)

    @property
    def has_operators(self) -> bool:
        return bool(
            self.include_domains
            or self.exclude_domains
            or self.filetypes
            or self.phrases
            or self.negated
            or self.any_of
        )


def _clean_host(value: str) -> str:
    v = (value or "").strip().strip("/").lower()
    if v.startswith("//"):
        v = v[2:]
    # Tolerate a full URL where someone wrote site:https://example.com/path
    if "://" in v:
        v = v.split("://", 1)[1]
    return v.split("/", 1)[0]


def parse(query: str) -> ParsedQuery:
    """Split a raw query into operators plus the text that should be searched."""
    original = query or ""
    text = original

    include_domains: list[str] = []
    exclude_domains: list[str] = []

    def _site_sub(match: re.Match) -> str:
        raw = match.group(1)
        negated = match.group(0).lstrip().startswith("-")
        host = _clean_host(raw)
        if not host:
            return ""
        (exclude_domains if negated else include_domains).append(host)
        return " "

    text = _SITE_RE.sub(_site_sub, text)

    filetypes: list[str] = []
    filetype_content_types: set[str] = set()

    def _filetype_sub(match: re.Match) -> str:
        raw = match.group(1).lower()
        if match.group(0).lstrip().startswith("-"):
            return " "
        ext = raw.rsplit(".", 1)[-1]
        if ext:
            filetypes.append(ext)
            filetype_content_types.update(_FILETYPE_ALIASES.get(ext, {ext}))
        return " "

    text = _FILETYPE_RE.sub(_filetype_sub, text)

    phrases = [p.strip() for p in _PHRASE_RE.findall(text)]
    text = _PHRASE_RE.sub(" ", text)

    negated = [m.group(0)[1:].lower() for m in _NEGATIVE_RE.finditer(text)]
    text = _NEGATIVE_RE.sub(" ", text)

    # OR groups are detected on the original string, before operators are
    # removed. Otherwise `site:a.com OR site:b.com threading` strips both
    # sites and leaves a dangling "OR threading" in the text.
    any_of: list[list[str]] = []
    if _OR_RE.search(original):
        parts = [p.strip() for p in _OR_RE.split(original) if p and p.strip()]
        # Only plain-term groups are turned into OR alternatives. A part like
        # "site:b.com threading" mixes an operator with free text, and applying
        # them per-alternative needs its own precedence rules; rather than guess,
        # the OR is dropped and the term is searched normally.
        if len(parts) > 1 and not any(":" in p for p in parts):
            any_of = [[p] for p in parts]

    # A leftover separator from an operator-only branch, e.g. the "OR" above.
    text = _OR_RE.sub(" ", text)

    return ParsedQuery(
        text=" ".join(text.split()),
        original=original,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        filetypes=filetypes,
        phrases=phrases,
        negated=negated,
        any_of=any_of,
    )

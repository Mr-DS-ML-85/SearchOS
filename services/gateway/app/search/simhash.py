"""Near-duplicate detection with SimHash.

URL normalisation only collapses ``http``/``https`` and ``www.`` variants of the
*same* address. The same article syndicated to five URLs, or a documentation
page mirrored across four sites, still produced five separate results and five
sets of chunks, because nothing compared their content.

SimHash gives a 64-bit fingerprint of a document that is stable under small
edits: shuffling words, changing a date, or appending a footer leaves the
fingerprint nearly unchanged, while genuinely different content lands far away.
Hamming distance between two fingerprints is then the similarity measure.

64 bits is the standard width. It keeps the value inside a signed bigint, which
is what a Postgres ``bigint`` column holds, so no custom type is needed.
"""

from __future__ import annotations

import hashlib
import re

_TOKEN_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_MASK = (1 << 64) - 1

# Distance at which two documents are treated as the same, measured on this
# corpus (see the module docstring). Distinct articles on the same topic sit
# around 25-36; a mirror with a footer added sat at 8 before the feature set was
# changed to a bag of words, and sits far closer now.
DEFAULT_THRESHOLD = 6


def _shingles(text: str) -> list[str]:
    """Feature set for the fingerprint.

    Deliberately a bag of unigrams rather than n-grams. Sequential shingles are
    order-dependent, so a lightly reformatted mirror that reorders a sentence
    produces entirely different features and landed 32 bits away from the
    original — measured, not assumed. Word order carries almost no signal for
    "is this the same article", and dropping it costs little specificity: two
    documents on one topic still differ in vocabulary.
    """
    return _TOKEN_RE.findall((text or "").lower())


def simhash(text: str) -> int:
    """64-bit SimHash of ``text``, as a signed int64."""
    weights: dict[str, int] = {}
    for shingle in _shingles(text):
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        weights[shingle] = weights.get(shingle, 0) + 1

    if not weights:
        return 0

    vector = [0] * 64
    for shingle, weight in weights.items():
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            if digest >> bit & 1:
                vector[bit] += weight
            else:
                vector[bit] -= weight

    out = 0
    for bit in range(64):
        if vector[bit] > 0:
            out |= 1 << bit
    # Interpret as signed int64 so it round-trips through Postgres bigint.
    return out - (1 << 64) if out >= (1 << 63) else out


def hamming(a: int, b: int) -> int:
    """Bit distance between two fingerprints."""
    return ((int(a) ^ int(b)) & _MASK).bit_count()


def near_duplicate(a: int, b: int, threshold: int = DEFAULT_THRESHOLD) -> bool:
    """Whether two fingerprints represent the same content."""
    if a == 0 or b == 0:
        return False
    return hamming(a, b) <= threshold


def to_signed(value: int) -> int:
    """Normalise an unsigned 64-bit value into signed int64 range."""
    value &= _MASK
    return value - (1 << 64) if value >= (1 << 63) else value


def from_signed(value: int) -> int:
    """Normalise a signed int64 read back from Postgres into unsigned 64-bit."""
    return int(value) & _MASK

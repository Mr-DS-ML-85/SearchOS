"""Per-document language detection.

trafilatura reports a language for PDFs and well-formed pages, but returns
``None`` often enough that the corpus ended up with ``lang = NULL`` on every row
— 742 of 742 at the time this was written. Without a language on the document
there is nothing to route FTS by, which is why every row was being stemmed with
the ``english`` configuration regardless of what it was actually written in.

Detection here is deliberately dependency-free and conservative:

- **Script first.** CJK, Cyrillic, Arabic, Hebrew, Devanagari, Bengali, Thai and
  friends are identified from Unicode ranges alone, because a document written
  in them cannot be anything else. This is the reliable half.
- **Stopwords second.** For Latin script, which is genuinely ambiguous, the most
  frequent function words decide. Uncertain results return ``None`` rather than
  a guess — a wrong language is worse than none, because it selects the wrong
  stemmer.
"""

from __future__ import annotations

import re
import unicodedata

# Unicode ranges that identify a script outright. Order matters: Japanese text
# also contains kana and ideographs, and Korean mixes hangul with hanja.
_SCRIPT_RANGES: list[tuple[str, tuple[tuple[int, int], ...]]] = [
    ("japanese", ((0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xFF66, 0xFF9D))),
    ("korean", ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ("chinese", ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("hebrew", ((0x0590, 0x05FF),)),
    ("devanagari", ((0x0900, 0x097F),)),
    ("bengali", ((0x0980, 0x09FF),)),
    ("tamil", ((0x0B80, 0x0BFF),)),
    ("thai", ((0x0E00, 0x0E7F),)),
    ("greek", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
    ("cyrillic", ((0x0400, 0x04FF), (0x0500, 0x052F))),
]

# Only languages Postgres ships a stemmer for, so detection never selects a
# config the server does not have.
_SCRIPT_TO_REGCONFIG = {
    "japanese": "simple",
    "chinese": "simple",
    "korean": "simple",
    "arabic": "arabic",
    "hebrew": "simple",
    "greek": "greek",
}

_STOPWORDS: dict[str, set[str]] = {
    "english": {
        "the",
        "and",
        "of",
        "to",
        "in",
        "is",
        "that",
        "for",
        "with",
        "this",
        "are",
        "was",
        "you",
        "not",
        "have",
    },
    "german": {
        "der",
        "die",
        "das",
        "und",
        "ist",
        "nicht",
        "mit",
        "für",
        "von",
        "ein",
        "eine",
        "auf",
        "den",
        "dem",
        "sich",
    },
    "french": {"le", "la", "les", "des", "est", "et", "une", "dans", "pour", "pas", "sur", "qui", "avec", "au", "ce"},
    "spanish": {"el", "la", "los", "las", "de", "que", "y", "en", "un", "una", "por", "con", "no", "para", "es"},
    "portuguese": {"de", "que", "não", "uma", "para", "com", "por", "dos", "como", "mais", "sem", "ao", "ele", "das"},
    "italian": {"il", "lo", "la", "che", "di", "un", "una", "per", "con", "non", "sono", "come", "del", "nel", "gli"},
    "dutch": {"de", "het", "een", "van", "en", "in", "is", "dat", "op", "te", "voor", "met", "niet", "zijn", "aan"},
    "turkish": {"bir", "ve", "bu", "için", "ile", "olarak", "daha", "olan", "çok", "kadar", "sonra", "gibi", "ama"},
    "polish": {"nie", "się", "jest", "że", "w", "na", "z", "do", "to", "jak", "ale", "po", "przez", "dla"},
    "russian": {
        "и",
        "в",
        "на",
        "с",
        "не",
        "что",
        "это",
        "как",
        "для",
        "по",
        "из",
        "этот",
        "того",
        "так",
        "или",
        "который",
        "если",
    },
    # Overlap with Russian is deliberately excluded (в, на, з, не, по, для).
    # Including them made the two languages vote within the margin and
    # every Cyrillic document came back undetected.
    "ukrainian": {"що", "це", "цей", "із", "але", "та", "або", "як", "ї", "ти", "у", "їх"},
}

# Languages with no dedicated stemmer fall back to `simple`, which still beats
# applying English stemming to them.
_STOPWORD_TO_REGCONFIG = {
    "english": "english",
    "german": "german",
    "french": "french",
    "spanish": "spanish",
    "portuguese": "portuguese",
    "italian": "italian",
    "dutch": "dutch",
    "turkish": "turkish",
    "polish": "simple",
    "russian": "russian",
    "ukrainian": "simple",
}

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Below this many Latin-script words the stopword vote is noise.
_MIN_WORDS = 12
# The winner must beat the runner-up by this margin to be reported at all.
_MIN_MARGIN = 2


# A script must actually dominate the sample before it is reported. One stray
# glyph, an emoji, a maths symbol or a stray quoted phrase in an otherwise
# English page is not evidence that the document is in that script — and the
# previous "any character wins" rule mislabelled 218 of 742 English documents as
# Bengali, sending every one of them to the `simple` stemmer.
_MIN_SCRIPT_SHARE = 0.20


def detect_script(text: str) -> str | None:
    """Identify the writing system from character ranges, or None if Latin."""
    if not text:
        return None
    sample = text[:4000]
    counts: dict[str, int] = {}
    letters = 0
    for ch in sample:
        cp = ord(ch)
        if not ch.isalpha():
            continue
        letters += 1
        if cp < 0x0400:
            continue
        for name, ranges in _SCRIPT_RANGES:
            if any(lo <= cp <= hi for lo, hi in ranges):
                counts[name] = counts.get(name, 0) + 1
                break
    if not counts or letters == 0:
        return None
    best = max(counts, key=lambda k: counts[k])
    if counts[best] / letters < _MIN_SCRIPT_SHARE:
        return None
    return best


def detect(text: str) -> str | None:
    """Best-guess language code, or None when there is not enough evidence."""
    if not text or not text.strip():
        return None

    script = detect_script(text)
    if script and script not in ("cyrillic", "chinese"):
        return script
    # Cyrillic falls through to the stopword vote, which can separate Russian
    # from Ukrainian; returning the script name would force `simple` for both.
    # Kana settles the Chinese/Japanese ambiguity that han characters alone
    # cannot, since Japanese text mixes both.
    if any("\u3040" <= ch <= "\u30ff" for ch in text[:4000]):
        return "japanese"
    if any("\uac00" <= ch <= "\ud7af" for ch in text[:4000]):
        return "korean"

    words = [w.lower() for w in _WORD_RE.findall(text[:4000])]
    if len(words) < _MIN_WORDS:
        return None

    scores: dict[str, int] = {}
    for lang, stops in _STOPWORDS.items():
        hits = sum(1 for w in words if w in stops)
        if hits:
            scores[lang] = hits

    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if best_score - runner_up < _MIN_MARGIN:
        return None
    if best_score / len(words) < 0.02:
        return None
    return best


def regconfig_for(language: str | None) -> str:
    """Map a detected language onto a Postgres text-search configuration."""
    if not language:
        return "simple"
    lang = language.strip().lower().replace("_", "-").split("-")[0]
    if lang in _STOPWORD_TO_REGCONFIG:
        return _STOPWORD_TO_REGCONFIG[lang]
    if lang in _SCRIPT_TO_REGCONFIG:
        return _SCRIPT_TO_REGCONFIG[lang]
    return "simple"


def normalize(text: str) -> str:
    """NFKC-fold so visually identical forms share a tsvector."""
    return unicodedata.normalize("NFKC", text or "")


# Callers speak ISO 639-1 ("fr", "fr-CA"); documents store the detector's names
# ("french"). Comparing the two directly matched nothing, which is why a
# `language=fr` search silently returned English results.
_ISO_TO_NAME = {
    "en": "english",
    "de": "german",
    "fr": "french",
    "es": "spanish",
    "pt": "portuguese",
    "it": "italian",
    "nl": "dutch",
    "tr": "turkish",
    "pl": "polish",
    "ru": "russian",
    "uk": "ukrainian",
    "ja": "japanese",
    "ko": "korean",
    "zh": "chinese",
    "ar": "arabic",
    "he": "hebrew",
    "hi": "devanagari",
    "bn": "bengali",
    "ta": "tamil",
    "th": "thai",
    "el": "greek",
}
_KNOWN_NAMES = set(_ISO_TO_NAME.values()) | set(_STOPWORDS) | {name for name, _ in _SCRIPT_RANGES}


def language_name(code: str | None) -> str | None:
    """Map an ISO code, locale or language name onto the stored ``lang`` value.

    Returns None for anything unrecognised so the caller can reject it rather
    than filter on a value no document will ever carry.
    """
    if not code:
        return None
    raw = code.strip().lower().replace("_", "-")
    if raw in _KNOWN_NAMES:
        return raw
    return _ISO_TO_NAME.get(raw.split("-")[0])

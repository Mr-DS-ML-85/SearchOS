"""Build a per-language tsvector.

`to_tsvector(regconfig, text)` cannot be a generated column because it is only
IMMUTABLE for a literal config, so the vector is computed here and stored.
Values are bound parameters; the config is restricted to a known allowlist so a
crafted language string can never reach the SQL text.
"""

from __future__ import annotations

from sqlalchemy import text

# Postgres ships these stemmers. Anything outside the set falls back to
# `simple`, which is also what an unknown language gets.
ALLOWED_CONFIGS = frozenset(
    {
        "simple",
        "arabic",
        "armenian",
        "basque",
        "catalan",
        "danish",
        "dutch",
        "english",
        "finnish",
        "french",
        "german",
        "greek",
        "hindi",
        "hungarian",
        "indonesian",
        "irish",
        "italian",
        "lithuanian",
        "nepali",
        "norwegian",
        "portuguese",
        "romanian",
        "russian",
        "serbian",
        "spanish",
        "swedish",
        "tamil",
        "turkish",
        "yiddish",
    }
)


async def build_language_tsv(language: str | None, text_body: str) -> str:
    """Return a tsvector string for `text_body` using `language`'s stemmer."""
    from app.db import get_session_factory
    from app.search.langdetect import regconfig_for

    config = regconfig_for(language)
    if config not in ALLOWED_CONFIGS:
        config = "simple"
    body = (text_body or "")[:1_000_000]
    if not body.strip():
        return ""
    async with get_session_factory()() as session:
        row = await session.execute(
            # CAST(...) rather than `:cfg::regconfig`: SQLAlchemy's parameter parser
            # treats the `::` as part of the placeholder and emits invalid SQL.
            text("SELECT to_tsvector(CAST(:cfg AS regconfig), :body)"),
            {"cfg": config, "body": body},
        )
        return row.scalar() or ""

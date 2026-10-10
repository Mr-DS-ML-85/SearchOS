from __future__ import annotations

import re
import time

from sqlalchemy import text

from app.config import get_settings
from app.metrics import get_metrics
from app.schemas import SearchResult
from app.search.base import ProviderQuery, ProviderResult, SearchProvider
from app.search.snippets import build_snippet

# The tsvector expression is byte-identical to the one in migration
# 0001/0002 (ix_documents_fts). If you change one, change both or the GIN index
# silently stops being used and the query degrades to a sequential scan.
DOC_TSVECTOR = "to_tsvector('english', coalesce(d.title,'') || ' ' || coalesce(d.content_text,''))"

# Same expression under the `simple` regconfig, matching migration 0005 and
# ix_documents_fts_simple. `english` stems and drops English stopwords, which
# destroys documents written in other languages; `simple` is unstemmed and works
# everywhere, at some cost to English recall. Queried second, as a fallback.
DOC_TSVECTOR_SIMPLE = "to_tsvector('simple', coalesce(d.title,'') || ' ' || coalesce(d.content_text,''))"

# Queried in order. English first so the common case uses the better stemmed
# index; `simple` only runs when english found nothing, so an English query does
# not pay for two index scans.
_FTS_CONFIGS = (("english", DOC_TSVECTOR), ("simple", DOC_TSVECTOR_SIMPLE))


# Per-language vector first: it is stemmed with the document's own language.
# Rows with a NULL tsv (indexed before migration 0006) are not matched by it, so
# the expression indexes above still cover them and remain in the list.
def _safe_cfg(lang: str) -> str:
    """Map a stored language to a regconfig, restricted to a known allowlist.

    Bound as a parameter, but Postgres will still reject an unknown config name,
    and `lang` is attacker-influenced via page metadata.
    """
    from app.search.langdetect import regconfig_for
    from app.tsvector import ALLOWED_CONFIGS

    cfg = regconfig_for(lang)
    return cfg if cfg in ALLOWED_CONFIGS else "simple"


# time_range windows. Previously this parameter was accepted and documented as a
# "freshness filter" but never applied to the local or vector providers.
# Seconds are used with make_interval() rather than a CAST(... AS interval)
# string, because asyncpg sends a text bound parameter as an unknown type that
# Postgres then cannot resolve to interval.
TIME_RANGE_SECONDS = {
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}

# One token: a run of word characters (Unicode letters, digits, underscore).
# re.UNICODE is required — the previous ASCII-only pattern [a-zA-Z0-9_]+ yielded
# an empty tsquery for "日本語" and split "müller" into "ller".
TERM_RE = re.compile(r"\w+", re.UNICODE)


class LocalIndexProvider(SearchProvider):
    name = "local"

    def __init__(self, session_factory) -> None:
        self.settings = get_settings()
        self.session_factory = session_factory

    @staticmethod
    def _escape_like(value: str) -> str:
        """Escape LIKE metacharacters so a domain cannot act as a wildcard.

        Values are bound parameters (not injectable), but a caller passing
        ``domains=["%"]`` would otherwise match every indexed document.
        """
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _domain_filters(self, query: ProviderQuery) -> tuple[str, dict]:
        sql = ""
        params: dict = {}
        if query.domains:
            # Placeholder *names* are generated here; every value is bound.
            # noqa comments cannot live inside the SQL literal, so the whole
            # filter clause is assembled from generated placeholder names only.
            clauses = " OR ".join(f"d.normalized_url LIKE :dom{i} ESCAPE '\\'" for i in range(len(query.domains)))
            sql += f" AND ({clauses})"
            for i, d in enumerate(query.domains):
                params[f"dom{i}"] = f"%{self._escape_like(d)}%"
        if query.exclude_domains:
            for i, d in enumerate(query.exclude_domains):
                sql += f" AND d.normalized_url NOT LIKE :excl{i} ESCAPE '\\'"
                params[f"excl{i}"] = f"%{self._escape_like(d)}%"
        return sql, params

    def _time_filter(self, query: ProviderQuery) -> tuple[str, dict]:
        """Map time_range to a SQL interval, applied to publish (or fetch) time."""
        if query.time_range not in TIME_RANGE_SECONDS:
            return "", {}
        return (
            " AND COALESCE(d.published_at, d.fetched_at) >= now() - make_interval(secs => :max_age_secs)",
            {"max_age_secs": TIME_RANGE_SECONDS[query.time_range]},
        )

    async def search(self, query: ProviderQuery) -> ProviderResult:
        start = time.monotonic()
        result = ProviderResult(provider=self.name, query=query.q)
        try:
            rows = await self._fts_search(query)
        except Exception as e:
            get_metrics().provider_errors.labels(provider=self.name, kind="fts").inc()
            result.error = f"local index error: {e.__class__.__name__}: {e}"
            result.latency_ms = (time.monotonic() - start) * 1000
            return result

        for rank, row in enumerate(rows, start=1):
            fts_score = float(row["fts_score"] or 0.0)
            content = row["content_text"] or ""
            result.results.append(
                SearchResult(
                    title=row["title"] or row["url"],
                    url=row["url"],
                    snippet=build_snippet(content, query.q).text if content else "",
                    source=self.name,
                    score=fts_score * self.settings.local_index_fts_weight,
                    rank=rank,
                    published=row["published_at"].isoformat() if row["published_at"] else None,
                    domain=_domain(row["url"]),
                    raw={"simhash": row["simhash"]} if row.get("simhash") is not None else None,
                )
            )

        result.latency_ms = (time.monotonic() - start) * 1000
        get_metrics().search_duration.labels(provider=self.name).observe(result.latency_ms / 1000)
        return result

    async def _fts_search(self, query: ProviderQuery, *, or_fallback: bool = True) -> list:
        domain_sql, domain_params = self._domain_filters(query)
        time_sql, time_params = self._time_filter(query)
        order = """
            ORDER BY fts_score DESC, d.fetched_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """
        params: dict = {"limit": query.num_results, "offset": query.offset}
        params.update(domain_params)
        params.update(time_params)

        rows: list = []
        async with self.session_factory() as session:
            for mode in ("AND", "OR") if or_fallback else ("AND",):
                tsq = self._build_tsquery(query.q, mode)
                if not tsq:
                    continue

                # A per-language tsvector cannot be matched with one regconfig:
                # each row was stemmed with the rules of its own language, so a
                # French row's lexemes are not English ones. The query is
                # therefore OR-ed across the language configs actually present,
                # each scoped to the rows that used it.
                lang_rows = await session.execute(
                    text(
                        """
                        SELECT lang, count(*) FROM documents
                        WHERE tsv IS NOT NULL AND status = 'indexed' AND lang IS NOT NULL
                        GROUP BY lang
                        """
                    )
                )
                configs = [r[0] for r in lang_rows if r[0]]
                if configs:
                    clauses = " OR ".join(
                        f"(d.lang = :lang_{i} AND d.tsv @@ to_tsquery(:cfg_{i}, :tsq))" for i in range(len(configs))
                    )
                    params_lang = {"tsq": tsq}
                    for i, cfg in enumerate(configs):
                        params_lang[f"lang_{i}"] = cfg
                        params_lang[f"cfg_{i}"] = _safe_cfg(cfg)
                    base = f"""
                        SELECT
                            d.url, d.title, d.content_text, d.published_at, d.source, d.fetched_at, d.simhash,
                            ts_rank_cd(d.tsv, to_tsquery(:cfg_0, :tsq)) AS fts_score
                        FROM documents d
                        WHERE d.status = 'indexed' AND ({clauses})
                        """
                    sql = text(base + domain_sql + time_sql + order)  # noqa: S608 - values bound
                    found = (await session.execute(sql, {**params, **params_lang})).mappings().all()
                    if found:
                        return found

                # Rows with a NULL tsv — indexed before migration 0006 — are not
                # covered above, so the expression indexes still apply to them.
                for config, expr in _FTS_CONFIGS:
                    base = f"""
                        SELECT
                            d.url, d.title, d.content_text, d.published_at, d.source, d.fetched_at, d.simhash,
                            ts_rank_cd({expr}, to_tsquery('{config}', :tsq)) AS fts_score
                        FROM documents d
                        WHERE d.status = 'indexed' AND d.tsv IS NULL
                          AND {expr} @@ to_tsquery('{config}', :tsq)
                        """
                    sql = text(base + domain_sql + time_sql + order)  # noqa: S608 - values bound
                    rows = (await session.execute(sql, {**params, "tsq": tsq})).mappings().all()
                    if rows:
                        return rows
        return rows

    def _build_tsquery(self, q: str, mode: str) -> str:
        """Build a to_tsquery string, returning "" when there are no usable terms.

        Each lexeme is single-quoted so that a query containing tsquery operators
        (``cats and dogs``, ``foo & bar``, ``!foo``, ``(x)``) cannot produce a
        syntax error — the previous version interpolated raw terms and a search
        for "cats and dogs" raised PostgresProgrammingError, surfacing as a 500.
        """
        terms = [t for t in TERM_RE.findall(q.lower()) if len(t) > 1]
        if not terms:
            return ""
        quoted = [f"'{t}'" for t in terms]
        return f" {'&' if mode == 'AND' else '|'} ".join(quoted)


def _domain(url: str) -> str:
    from app.search.providers.searxng import SearXNGProvider

    return SearXNGProvider._domain(url)

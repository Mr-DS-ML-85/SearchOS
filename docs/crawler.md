# Crawler: Recursive Indexing + Reindex

The worker runs three loops; two of them keep your index growing and fresh.

## Recursive crawl

After a page is indexed, `_discover_and_enqueue` extracts `<a href>` links, normalizes them, and:

1. filters by domain policy:
   - `same` (default) — same host or a subdomain of the seed host
   - `all` — any domain
   - `none` — never follow
2. checks `robots.txt` (`robots.allowed`)
3. dedupes against already-indexed documents **and** active (pending/running) jobs
4. caps the batch at `CRAWLER_BATCH_SIZE` (25) per page
5. enqueues new URLs at `depth + 1`

Depth is capped by `CRAWLER_MAX_DEPTH` (default 2). The seed is depth 0.

Example log output:

```
crawler enqueued 10 nested links from https://en.wikipedia.org/wiki/Siamese_neural_network
indexed https://en.wikipedia.org/wiki/Siamese_neural_network (4 chunks)
```

The job table shows the cascade:

```
depth=0  52 done
depth=1  87 done
depth=2  407 done
```

## Reindex (continuous recrawl)

The recrawler loop runs every `CRAWLER_CONTINUOUS_INTERVAL_SECONDS`. It:

1. selects up to `CRAWLER_RECRAWL_BATCH` indexed documents whose `fetched_at` is older than `CRAWLER_RECRAWL_AFTER_SECONDS`, **oldest first**
2. re-enqueues them as depth-0 ingest jobs (deduped against active jobs)
3. the ingest loop re-fetches, replaces chunks, and **stamps a fresh `fetched_at`**

Example:

```
recrawler enqueued 7 stale documents for refresh
indexed https://en.wikipedia.org/wiki/Machine_learning (68 chunks)
```

After the refresh the document is fresh again, so it won't be re-enqueued until it ages past the threshold.

## Politeness

The worker claims one job at a time with no delay between requests to the same
host, so a recursive crawl of a documentation site fires requests as fast as the
network allows. That is how a crawler gets a host to block it — and blocking one
origin can take out the entire seed.

Every outbound fetch therefore passes a per-origin gate that enforces two
things:

- `robots.txt`'s **`Crawl-delay`** when the origin declares one, and
- `CRAWLER_MIN_DELAY_SECONDS` otherwise, so an origin that simply omits the
  directive is still throttled.

The schedule lives in **Redis**, not in the worker process. Two consequences that
matter:

- every worker shares one schedule, so N workers do not each apply the delay
  independently and multiply the effective rate by N;
- a worker restart cannot reset an origin's window and immediately re-burst
  against a host that just asked for slower access.

The timestamp is stamped **before** the sleep, not after. Otherwise concurrent
jobs all read the same stale timestamp and then sleep out the same window in
parallel, which is exactly the burst the gate exists to prevent.

If Redis is unavailable the gate falls back to an in-process schedule — less
accurate across workers, but it degrades to "still throttled" rather than to "no
throttling".

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CRAWLER_FOLLOW_LINKS` | `true` | enable recursive link following |
| `CRAWLER_MAX_DEPTH` | `2` | max nested depth from a seed |
| `CRAWLER_BATCH_SIZE` | `25` | links followed per page |
| `CRAWLER_FOLLOW_DOMAINS` | `same` | `same` \| `all` \| `none` |
| `CRAWLER_MIN_DELAY_SECONDS` | `1.0` | floor applied when an origin declares no `Crawl-delay` |
| `CRAWLER_CONTINUOUS_ENABLED` | `true` | recrawl loop on |
| `CRAWLER_CONTINUOUS_INTERVAL_SECONDS` | `3600` | recrawl scan interval |
| `CRAWLER_RECRAWL_AFTER_SECONDS` | `604800` (7d) | re-crawl pages older than this |
| `CRAWLER_RECRAWL_BATCH` | `100` | docs per recrawl scan |

## Observing

- `GET /stats` → `ingest_pending` shows the backlog.
- Worker logs: `indexed ... (N chunks)`, `crawler enqueued N nested links ...`, `recrawler enqueued N stale documents ...`.
- Prometheus: `searchos_documents_indexed_total`, `searchos_chunks_indexed_total`, ingest-job gauges.

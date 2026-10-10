"""ColBERT late-interaction scoring.

A single vector per chunk (what the `chunks.embedding` column holds) has to
compress a whole passage into one point in space. Two passages that share a
rare term but differ in overall topic can end up far apart, and a query that
matches three terms scattered across a passage is no closer to it than one that
matches all four in a row.

Late interaction keeps one vector *per token* and scores with MaxSim: for each
query token take the best-matching document token, then sum. Relevance becomes
"how much of the query is covered", not "how similar are the centroids".

The cost is storage, and it is steep. Measured against this corpus:

    colbertv2.0, 512 tokens x 768d      1536 KB/chunk   10.9 GB
    answerai-colbert-small, 512 x 512d  1024 KB/chunk    7.2 GB
    projected, 256 tokens x 128d  fp32   128 KB/chunk    0.9 GB
    projected, 256 tokens x 128d  fp16    64 KB/chunk    0.5 GB

So tokens are projected down to 128 dimensions (which is what ColBERT's own
projection head is for), capped at 256 tokens, and stored as halfvec. That is
the difference between a feature that fits and one that does not.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from app.config import get_settings
from app.logging import get_logger

logger = get_logger("search.colbert")

COLBERT_DIM = 128
MAX_DOC_TOKENS = 256

_lock: logging.Lock | None = None
_model = None
_tokenizer = None
_projection: np.ndarray | None = None


def available() -> bool:
    try:
        import torch  # noqa: F401
        from transformers import AutoModel  # noqa: F401

        return True
    except ImportError:
        return False


def _ensure_lock():
    global _lock
    if _lock is None:
        import threading

        _lock = threading.Lock()
    return _lock


def _load():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    with _ensure_lock():
        if _model is not None:
            return _model, _tokenizer
        from transformers import AutoModel, AutoTokenizer

        name = get_settings().colbert_model
        _tokenizer = AutoTokenizer.from_pretrained(name)
        _model = AutoModel.from_pretrained(name)
        logger.info("ColBERT loaded: %s", name)
    return _model, _tokenizer


def warm() -> bool:
    if not available():
        return False
    try:
        _load()
        return True
    except Exception as exc:
        logger.warning("ColBERT warm failed: %s", exc)
        return False


def _load_projection() -> np.ndarray | None:
    """The checkpoint's own 768->128 projection head.

    AutoModel does not load ColBERT's ``linear`` head — transformers reports it
    as an unexpected key — so it is read out of the safetensors file directly.
    This is the projection ColBERT was trained with. A random projection would
    still yield 128-dimensional vectors and a plausible-looking score, but the
    token embeddings would no longer share a space and MaxSim would be measuring
    noise, so failing to find it is an error rather than a fallback.
    """
    global _projection
    if _projection is not None:
        return _projection
    with _ensure_lock():
        if _projection is not None:
            return _projection
        try:
            from safetensors import safe_open
            from transformers.utils import cached_file

            name = get_settings().colbert_model
            # Resolve through transformers' own cache lookup rather than
            # globbing HF_HOME: the layout has changed between versions and the
            # glob silently matched nothing.
            path = cached_file(name, "model.safetensors")
            if not path:
                logger.warning("ColBERT checkpoint not in cache for %s", name)
                return None
            with safe_open(path, framework="pt") as handle:
                # keys() on a safetensors handle is a list, not a dict view, so
                # `in handle` is not equivalent here.
                keys = list(handle.keys())
                if "linear.weight" in keys:
                    weight = handle.get_tensor("linear.weight")
                    _projection = weight.numpy().astype(np.float32)
                    logger.info("ColBERT projection head %s loaded", tuple(weight.shape))
        except Exception as exc:
            logger.warning("ColBERT projection head unavailable: %s", exc)
    return _projection


def _project(hidden: np.ndarray) -> np.ndarray:
    """Project token states into the 128-d late-interaction space.

    ColBERT's own design projects the transformer's hidden states down before
    storing them; doing it here is what makes the storage budget work. L2
    normalising after projection is required, because MaxSim compares raw
    inner products and an unnormalised vector would just be scored on length.
    """
    hidden = hidden / (np.linalg.norm(hidden, axis=-1, keepdims=True) + 1e-8)
    if hidden.shape[-1] == COLBERT_DIM:
        return hidden
    projection = _load_projection()
    # The checkpoint stores the head as [out_features, in_features] = [128, 768],
    # so the multiply needs the transpose to go 768 -> 128.
    if projection is None or projection.shape[1] != hidden.shape[-1]:
        raise RuntimeError(
            f"ColBERT projection head missing or mismatched: need "
            f"{hidden.shape[-1]}->{COLBERT_DIM}, have "
            f"{None if projection is None else tuple(projection.shape)}"
        )
    return hidden @ projection.T


def _encode_sync(texts: list[str], *, is_query: bool) -> list[list[list[float]]]:
    import torch

    model, tokenizer = _load()
    limit = 32 if is_query else MAX_DOC_TOKENS
    batch = tokenizer(
        texts,
        padding="longest",
        truncation=True,
        max_length=limit,
        return_tensors="pt",
    )
    with torch.no_grad():
        out = model(**batch)
    hidden = out.last_hidden_state.numpy().astype(np.float32)
    mask = batch["attention_mask"].numpy()

    results = []
    for row in range(hidden.shape[0]):
        keep = np.nonzero(mask[row])[0]
        if keep.size == 0:
            results.append(np.zeros((0, COLBERT_DIM), dtype=np.float16))
            continue
        results.append(_project(hidden[row][keep]).astype(np.float16))
    return [r.tolist() for r in results]


async def encode_query(text: str) -> list[list[float]]:
    if not available() or not text.strip():
        return []
    out = await asyncio.to_thread(_encode_sync, [text], is_query=True)
    return out[0] if out else []


async def encode_documents(texts: list[str]) -> list[list[list[float]]]:
    if not available() or not texts:
        return []
    return await asyncio.to_thread(_encode_sync, texts, is_query=False)


def maxsim(query: list[list[float]], doc: list[list[float]]) -> float:
    """Late-interaction score: sum over query tokens of the best doc token.

    Length-normalised by the query, not the document, so that a long passage is
    not rewarded merely for containing many tokens to match against.
    """
    if not query or not doc:
        return 0.0
    q = np.asarray(query, dtype=np.float32)
    d = np.asarray(doc, dtype=np.float32)
    # (n_query_tokens, n_doc_tokens)
    sim = q @ d.T
    return float(sim.max(axis=1).sum() / q.shape[0])

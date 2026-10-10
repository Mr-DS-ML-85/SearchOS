from __future__ import annotations

import asyncio
from collections.abc import Sequence

from app.config import get_settings


class EmbeddingProvider:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError("sentence-transformers not installed. Run: pip install sentence-transformers") from None
        self._model = SentenceTransformer(self.settings.embedding_model, device=self.settings.embedding_device)
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = await asyncio.to_thread(self._load_model)
        batch = self.settings.embedding_batch_size
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = list(texts[i : i + batch])
            encoded = await asyncio.to_thread(model.encode, chunk, normalize_embeddings=True)
            vectors.extend([list(map(float, v)) for v in encoded])
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    @property
    def available(self) -> bool:
        try:
            import sentence_transformers  # noqa: F401

            return True
        except ImportError:
            return False


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Return a process-wide singleton so the SentenceTransformer model is loaded once."""
    global _provider
    if _provider is None:
        _provider = EmbeddingProvider()
    return _provider

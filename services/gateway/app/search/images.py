"""CLIP image and text embeddings for multimodal search.

CLIP maps text and images into one vector space, so a text query retrieves images
by meaning rather than by filename or alt text.

Two decisions worth recording:

- **Preprocessing is done with numpy, not CLIPImageProcessor.** The processor
  requires torchvision, which is a ~800MB dependency for a resize, a centre crop
  and a mean/std normalisation. Doing it by hand is those exact steps and keeps
  the image extra to Pillow alone.
- **The model is loaded directly through transformers.** The
  ``sentence-transformers`` CLIP wrapper fails on this version
  ("Could not load any image processor class"), because the wrapper path expects
  an image processor this release no longer resolves.
"""

from __future__ import annotations

import asyncio
import io
import threading

from app.config import get_settings
from app.logging import get_logger

logger = get_logger("search.images")

MODEL_NAME = "openai/clip-vit-base-patch32"
EMBED_DIM = 512

# CLIP's own normalisation constants.
_MEAN = (0.48145466, 0.4578275, 0.40821073)
_STD = (0.26862954, 0.26130258, 0.27577711)
_SIZE = 224

_lock = threading.Lock()
_model = None
_processor = None


def available() -> bool:
    try:
        import PIL  # noqa: F401

        return True
    except ImportError:
        return False


def _load():
    global _model, _processor
    if _model is not None:
        return _model, _processor
    with _lock:
        if _model is not None:
            return _model, _processor
        from transformers import CLIPModel, CLIPTokenizer

        settings = get_settings()
        name = settings.clip_model or MODEL_NAME
        _model = CLIPModel.from_pretrained(name)
        _processor = CLIPTokenizer.from_pretrained(name)
        logger.info("CLIP loaded: %s (%d-dim)", name, EMBED_DIM)
    return _model, _processor


def warm() -> bool:
    """Load the model ahead of the first query."""
    if not available():
        return False
    try:
        _load()
        return True
    except Exception as exc:
        logger.warning("CLIP warm failed: %s", exc)
        return False


def _as_tensor(features):
    """transformers now returns a ModelOutput here rather than a bare tensor.

    pooler_output is the projected 512-dim CLIP space, not the 768-dim vision
    tower output, which is what the text side is projected into.
    """
    for attr in ("pooler_output", "image_embeds", "text_embeds"):
        value = getattr(features, attr, None)
        if value is not None:
            return value
    return features


def _preprocess(raw: bytes):
    """Decode, resize shortest side to 224, centre crop, normalise. Returns tensor."""
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        img = img.convert("RGB")
        w, h = img.size
        scale = _SIZE / min(w, h)
        img = img.resize((max(_SIZE, round(w * scale)), max(_SIZE, round(h * scale))), Image.BICUBIC)
        w, h = img.size
        left = (w - _SIZE) // 2
        top = (h - _SIZE) // 2
        img = img.crop((left, top, left + _SIZE, top + _SIZE))
        arr = np.asarray(img, dtype="float32") / 255.0
    arr = (arr - np.array(_MEAN, dtype="float32")) / np.array(_STD, dtype="float32")
    import torch

    return torch.from_numpy(arr).permute(2, 0, 1)


def _embed_images_sync(payloads: list[bytes]) -> list[list[float]]:
    import torch

    model, _ = _load()
    tensors = torch.stack([_preprocess(p) for p in payloads])
    with torch.no_grad():
        features = model.get_image_features(pixel_values=tensors)
    features = _as_tensor(features)
    features = features / features.norm(p=2, dim=-1, keepdim=True)
    return [list(map(float, v)) for v in features]


def _embed_texts_sync(texts: list[str]) -> list[list[float]]:
    import torch

    model, tokenizer = _load()
    batch = tokenizer(texts, padding=True, truncation=True, max_length=77, return_tensors="pt")
    with torch.no_grad():
        features = model.get_text_features(**batch)
    features = _as_tensor(features)
    features = features / features.norm(p=2, dim=-1, keepdim=True)
    return [list(map(float, v)) for v in features]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts or not available():
        return []
    return await asyncio.to_thread(_embed_texts_sync, texts)


async def embed_images(payloads: list[bytes]) -> list[list[float]]:
    if not payloads or not available():
        return []
    return await asyncio.to_thread(_embed_images_sync, payloads)


async def embed_one_image(raw: bytes) -> list[float] | None:
    out = await embed_images([raw])
    return out[0] if out else None

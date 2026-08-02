"""Generate SearchOS flat-design badges with inline Lucide icons.

Run:  python tools/gen_badges.py  ->  docs/assets/badges/*.svg

Each badge is a self-contained SVG (no external assets) so it renders on GitHub
READMEs and the landing page without any badge service. Text widths are measured
against a real font (fontTools, falling back to a character-class table) so
labels never overlap the value segment and values never truncate.
"""

from __future__ import annotations

import os

OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "assets", "badges")

# Lucide icon glyphs (ISC license, https://lucide.dev) drawn at 24x24 viewBox.
ICONS = {
    "python": '<path d="M12 2H2v20h10"/><path d="M16 22h6V2h-6"/>',  # style: code brackets
    "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    "cable": '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
    "network": '<rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/>',
    "box": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    "workflow": '<rect width="8" height="8" x="3" y="3" rx="2"/><path d="M7 11v4a2 2 0 0 0 2 2h4"/><rect width="8" height="8" x="13" y="13" rx="2"/>',
    "gauge": '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    "scale": '<path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="M7 21h10"/><path d="M12 3v18"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>',
    "rocket": '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
}

# (slug, icon, label, value, accent)
BADGES = [
    ("python", "python", "Python", "3.13", "#4b5563"),
    ("fastapi", "zap", "FastAPI", "gateway", "#059669"),
    ("mcp", "cable", "MCP", "3 transports", "#7c3aed"),
    ("postgres", "database", "PostgreSQL", "pgvector + HNSW", "#2563eb"),
    ("redis", "activity", "Redis", "cache + rate limits", "#dc2626"),
    ("searxng", "search", "SearXNG", "web fallback", "#ea580c"),
    ("crawler", "network", "Crawler", "recursive + recrawl", "#0e7490"),
    ("docker", "box", "Docker", "compose", "#0284c7"),
    ("alembic", "workflow", "Alembic", "migrations", "#b45309"),
    ("grafana", "gauge", "Grafana", "metrics", "#16a34a"),
    ("license", "scale", "License", "MIT", "#6b7280"),
    ("ci", "rocket", "Tests", "34 passed", "#475569"),
]

FONT = 'font-family="Inter, -apple-system, \'Segoe UI\', Roboto, sans-serif"'

# Layout constants (px at 28px badge height).
ICON_X, ICON_SIZE = 10, 14
ICON_TO_TEXT = 8
LABEL_PAD_R = 12
VALUE_PAD_L = 14
VALUE_PAD_R = 12


def _measure(s: str, size: float) -> float:
    """Measure text width in px using real font metrics when possible.

    Prefers DejaVu Sans Bold (close to the Inter fallback used at render time)
    via fontTools; falls back to a character-class advance table otherwise.
    """
    try:
        from fontTools.ttLib import TTFont

        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        f = TTFont(path)
        cmap = f.getBestCmap()
        hmtx = f["hmtx"]
        upm = f["head"].unitsPerEm
        w = 0.0
        for ch in s:
            cp = ord(ch)
            adv = hmtx[cmap[cp]][0] if cp in cmap else hmtx[0x20][0]
            w += adv / upm * size
        return w
    except Exception:
        # fallback: per-character advance classes at 1em
        classes = {
            "W": 1.0, "M": 0.96, "@": 0.9, "g": 0.76, "d": 0.74, "A": 0.72,
            "e": 0.66, "S": 0.66, "o": 0.65, "a": 0.62, "n": 0.62, "r": 0.5,
            "i": 0.3, "l": 0.32, ".": 0.25, " ": 0.28, "+": 0.66,
        }
        default = 0.58
        return sum(classes.get(ch, default) for ch in s) * size


def badge(slug: str, icon: str, label: str, value: str, accent: str) -> str:
    label_w = ICON_X + ICON_SIZE + ICON_TO_TEXT + _measure(label, 12) + LABEL_PAD_R
    value_w = VALUE_PAD_L + _measure(value, 12.5) + VALUE_PAD_R
    w = label_w + value_w
    icon_cx = ICON_X + ICON_SIZE / 2
    icon_cy = 14
    glyph = ICONS[icon]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}" height="28" viewBox="0 0 {w:.1f} 28" role="img">
  <title>{label} · {value}</title>
  <rect width="{w:.1f}" height="28" rx="14" fill="#ffffff" stroke="#e2e8f0"/>
  <rect x="0" y="0" width="{label_w:.1f}" height="28" rx="14" fill="#0f172a"/>
  <rect x="{label_w:.1f}" y="0" width="{value_w:.1f}" height="28" rx="14" fill="{accent}"/>
  <g transform="translate({icon_cx:.1f} {icon_cy:.1f}) scale(0.5833) translate(-12 -12)" fill="none" stroke="#cbd5e1" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" color="#cbd5e1">
      {glyph}
  </g>
  <text x="{ICON_X + ICON_SIZE + ICON_TO_TEXT}" y="18.5" font-size="12" font-weight="600" fill="#e2e8f0" {FONT}>{label}</text>
  <text x="{label_w + VALUE_PAD_L:.1f}" y="18.5" font-size="12.5" font-weight="700" fill="#ffffff" {FONT}>{value}</text>
</svg>"""


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for slug, icon, label, value, accent in BADGES:
        path = os.path.join(OUT, f"{slug}.svg")
        with open(path, "w") as f:
            f.write(badge(slug, icon, label, value, accent))
        print("wrote", path)


if __name__ == "__main__":
    main()

"""Generate the SearchOS logo SVG (icon + wordmark variants).

Run:  python tools/gen_logo.py  ->  docs/assets/logo.svg, docs/assets/logo-mark.svg
"""

from __future__ import annotations

import os

OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "assets")

# ---------- palette ----------
INDIGO = "#6366f1"
VIOLET = "#8b5cf6"
CYAN = "#22d3ee"
SKY = "#38bdf8"
WHITE = "#ffffff"
INK = "#0f172a"

GRAD = f"""
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
  <stop offset="0" stop-color="{INDIGO}"/>
  <stop offset="0.55" stop-color="{VIOLET}"/>
  <stop offset="1" stop-color="{CYAN}"/>
</linearGradient>
"""

GLOW = """
<filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
  <feGaussianBlur stdDeviation="14" result="b"/>
  <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
</filter>
"""


def nodes() -> str:
    """Small graph rendered inside the magnifier lens."""
    pts = [(206, 168), (302, 146), (252, 262), (322, 252)]
    edges = [(0, 1), (0, 2), (1, 3), (2, 3)]
    parts = []
    for a, b in edges:
        x1, y1 = pts[a]
        x2, y2 = pts[b]
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{WHITE}" '
            f'stroke-width="10" stroke-linecap="round" opacity="0.55"/>'
        )
    for i, (x, y) in enumerate(pts):
        size = 34 if i == 2 else 26
        parts.append(f'<circle cx="{x}" cy="{y}" r="{size}" fill="{WHITE}"/>')
        parts.append(
            f'<circle cx="{x}" cy="{y}" r="{size + 8}" fill="none" stroke="{WHITE}" stroke-width="5" opacity="0.35"/>'
        )
    return "\n  ".join(parts)


def spark() -> str:
    """Small four-point star accent, top-right."""
    x, y, r = 388, 96, 16
    pts = [
        (x, y - r),
        (x + r * 0.34, y - r * 0.34),
        (x + r, y),
        (x + r * 0.34, y + r * 0.34),
        (x, y + r),
        (x - r * 0.34, y + r * 0.34),
        (x - r, y),
        (x - r * 0.34, y - r * 0.34),
    ]
    d = "M" + " L".join(f"{px},{py}" for px, py in pts) + " Z"
    return f'<path d="{d}" fill="{SKY}" filter="url(#glow)"/>'


def magnifier() -> str:
    """Lucide-style search: ring + handle, with a small notch gap."""
    ring = (
        f'<circle cx="256" cy="230" r="118" fill="none" stroke="{WHITE}" '
        f'stroke-width="26" stroke-linecap="round" transform="rotate(-45 256 230)"/>'
    )
    # handle: two thick lines that look like a magnifier shaft
    handle = f'<line x1="342" y1="316" x2="424" y2="398" stroke="{WHITE}" stroke-width="30" stroke-linecap="round"/>'
    return ring + "\n  " + handle


def _tile(w: int, h: int) -> str:
    return f'<rect width="{w}" height="{h}" rx="{h * 0.18}" fill="url(#bg)"/>'


def icon() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
<defs>{GRAD}{GLOW}</defs>
  {_tile(512, 512)}
  <g opacity="0.14">
    <circle cx="120" cy="120" r="4" fill="{WHITE}"/>
    <circle cx="392" cy="420" r="4" fill="{WHITE}"/>
    <circle cx="96" cy="400" r="3" fill="{WHITE}"/>
    <circle cx="420" cy="100" r="3" fill="{WHITE}"/>
  </g>
  {magnifier()}
  {nodes()}
  {spark()}
</svg>"""


def wordmark(scale: int = 3) -> str:
    """Horizontal lockup: mark + 'SearchOS' text (for landing/hero)."""
    m = icon().replace(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="96" height="96">',
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 160" width="{640 * scale}" height="{160 * scale}" font-family="Inter, 'Segoe UI', system-ui, sans-serif">
<defs>
  <linearGradient id="word" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{INDIGO}"/><stop offset="1" stop-color="{CYAN}"/>
  </linearGradient>
</defs>
<g transform="translate(0,0) scale(0.3)">{m.replace('id="bg"', 'id="bgm"').replace('id="glow"', 'id="glowm"')}</g>
<text x="150" y="105" font-size="72" font-weight="800" fill="url(#word)" letter-spacing="-2">Search</text>
<text x="150" y="105" font-size="72" font-weight="300" fill="{INK}" letter-spacing="-2">OS</text>
</svg>"""


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for name, fn in (("logo.svg", icon), ("logo-mark.svg", icon), ("logo-wordmark.svg", wordmark)):
        with open(os.path.join(OUT, name), "w") as f:
            f.write(fn())
        print("wrote", os.path.join(OUT, name))


if __name__ == "__main__":
    main()

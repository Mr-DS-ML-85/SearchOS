"""Generate the SearchOS README banner (SVG + PNG via rsvg-convert).

Run:  python tools/gen_banner.py
  ->  docs/assets/banner.svg
  ->  docs/assets/banner.png (requires rsvg-convert)
"""

from __future__ import annotations

import os
import subprocess

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "..", "docs", "assets")

INDIGO = "#6366f1"
VIOLET = "#8b5cf6"
CYAN = "#22d3ee"
WHITE = "#ffffff"
SLATE = "#94a3b8"
BANNER_W = 1700
FONT = 'font-family="Inter, -apple-system, \'Segoe UI\', Roboto, sans-serif"'

def banner() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {BANNER_W} 300" width="{BANNER_W}" height="300">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#0b1020"/>
    <stop offset="0.6" stop-color="#101733"/>
    <stop offset="1" stop-color="#0e7490"/>
  </linearGradient>
  <linearGradient id="title" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#ffffff"/>
    <stop offset="0.55" stop-color="#c7d2fe"/>
    <stop offset="1" stop-color="#a5f3fc"/>
  </linearGradient>
  <radialGradient id="orb1" cx="0.5" cy="0.5" r="0.5">
    <stop offset="0" stop-color="{INDIGO}" stop-opacity="0.55"/>
    <stop offset="1" stop-color="{INDIGO}" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="orb2" cx="0.5" cy="0.5" r="0.5">
    <stop offset="0" stop-color="{CYAN}" stop-opacity="0.45"/>
    <stop offset="1" stop-color="{CYAN}" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="bar" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="{INDIGO}"/><stop offset="1" stop-color="{CYAN}"/>
  </linearGradient>
  <linearGradient id="markgrad" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{INDIGO}"/><stop offset="0.55" stop-color="{VIOLET}"/><stop offset="1" stop-color="{CYAN}"/>
  </linearGradient>
  <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
    <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#ffffff" stroke-opacity="0.05"/>
  </pattern>
</defs>
<rect width="{BANNER_W}" height="300" fill="url(#bg)"/>
<rect width="{BANNER_W}" height="300" fill="url(#grid)"/>
<circle cx="1030" cy="60" r="180" fill="url(#orb1)"/>
<circle cx="1150" cy="240" r="150" fill="url(#orb2)"/>
<circle cx="180" cy="270" r="130" fill="url(#orb2)"/>

<!-- logo mark -->
<g transform="translate(56,54) scale(0.375)">
  <rect width="512" height="512" rx="92" fill="url(#markgrad)"/>
  <circle cx="256" cy="230" r="118" fill="none" stroke="{WHITE}" stroke-width="26" stroke-linecap="round" transform="rotate(-45 256 230)"/>
  <line x1="342" y1="316" x2="424" y2="398" stroke="{WHITE}" stroke-width="30" stroke-linecap="round"/>
  <line x1="206" y1="168" x2="302" y2="146" stroke="{WHITE}" stroke-width="10" stroke-linecap="round" opacity="0.55"/>
  <line x1="206" y1="168" x2="252" y2="262" stroke="{WHITE}" stroke-width="10" stroke-linecap="round" opacity="0.55"/>
  <line x1="302" y1="146" x2="322" y2="252" stroke="{WHITE}" stroke-width="10" stroke-linecap="round" opacity="0.55"/>
  <line x1="252" y1="262" x2="322" y2="252" stroke="{WHITE}" stroke-width="10" stroke-linecap="round" opacity="0.55"/>
  <circle cx="206" cy="168" r="26" fill="{WHITE}"/>
  <circle cx="302" cy="146" r="26" fill="{WHITE}"/>
  <circle cx="252" cy="262" r="34" fill="{WHITE}"/>
  <circle cx="322" cy="252" r="26" fill="{WHITE}"/>
  <circle cx="388" cy="76" r="3" fill="{CYAN}"/>
  <circle cx="96" cy="400" r="3" fill="{WHITE}" opacity="0.6"/>
</g>

<text x="300" y="128" font-size="64" font-weight="800" fill="url(#title)" letter-spacing="-1" {FONT}>SearchOS</text>
<text x="300" y="172" font-size="24" fill="{SLATE}" {FONT}>Self-hosted search API for AI agents — your own FTS + vector index, live web fallback, continuous crawler.</text>
<text x="300" y="212" font-size="19" fill="#cbd5e1" {FONT}>No vendor lock-in · No per-query fees · No rate-limit ceilings · No data leaving your infra</text>

<rect x="300" y="238" width="180" height="8" rx="4" fill="url(#bar)"/>
</svg>"""

def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    svg = os.path.join(OUT, "banner.svg")
    with open(svg, "w") as f:
        f.write(banner())
    print("wrote", svg)
    try:
        png = os.path.join(OUT, "banner.png")
        subprocess.run(["rsvg-convert", "-w", str(BANNER_W), "-h", "300", "-o", png, svg], check=True)
        print("wrote", png)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print("skip png:", e)


if __name__ == "__main__":
    main()

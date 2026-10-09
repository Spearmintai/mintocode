"""Generate the README / home-page art from real data.

    python3 docs/assets/build.py            # SVGs (stdlib only)
    python3 docs/assets/build.py --png      # also social-preview.png (needs `pip install cairosvg`)

Every number and name drawn here comes from linkmap.json (a real link pack from the benchmark repo), a real
`mic ask` run, or the README's benchmark table.
"""
from __future__ import annotations

import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "linkmap.json")))

THEMES = {
    "light": dict(bg="#f2f6f3", bg2="#e6eee9", panel="#ffffff", ink="#12241d", muted="#56695f", line="#cfdcd4",
                  mint="#16845f", mintsoft="#c9ebdb", link="#3b6f8f", cell="#c3d3ca", term="#0f1f19", termink="#d9e8e1"),
    "dark": dict(bg="#0b1612", bg2="#10211b", panel="#13241e", ink="#e2eee8", muted="#93a99f", line="#22372f",
                 mint="#5cd2a3", mintsoft="#16382b", link="#7fb3d4", cell="#22362d", term="#07100d", termink="#d9e8e1"),
}
SANS = "'Helvetica Neue', Helvetica, Arial, 'Segoe UI', sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace"

# Brand mark colours are fixed: the mark reads the same on light and dark pages.
MARK = dict(tile="#10261e", cell="#2b4a3e", mint="#5cd2a3", link="#7fb3d4")


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- logo
def mark(x=0, y=0, size=100) -> str:
    """A 4x4 field of paragraphs: two seeds (mint), one linked paragraph (blue), the link drawn between them."""
    s = size / 100
    cells, n, g, pad = [], 4, 4, 16
    w = (100 - 2 * pad - (n - 1) * g) / n
    lit = {(0, 1): MARK["mint"], (1, 2): MARK["mint"], (3, 0): MARK["link"]}
    centers = {}
    for r in range(n):
        for c in range(n):
            cx, cy = pad + c * (w + g), pad + r * (w + g)
            centers[(r, c)] = (cx + w / 2, cy + w / 2)
            col = lit.get((r, c), MARK["cell"])
            cells.append(f'<rect x="{cx:.1f}" y="{cy:.1f}" width="{w:.1f}" height="{w:.1f}" rx="3" fill="{col}"/>')
    (ax, ay), (bx, by), (lx, ly) = centers[(0, 1)], centers[(1, 2)], centers[(3, 0)]
    lines = (f'<path d="M{ax:.1f},{ay:.1f} L{bx:.1f},{by:.1f} L{lx:.1f},{ly:.1f}" fill="none" stroke="{MARK["mint"]}" '
             f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>')
    return (f'<g transform="translate({x},{y}) scale({s})"><rect width="100" height="100" rx="22" fill="{MARK["tile"]}"/>'
            f'{"".join(cells)}{lines}</g>')


def wordmark(x, y, scale, ink, dot) -> str:
    """'mic' drawn from strokes (no font needed): two arches, a stem with a square seed for its dot, an open arc."""
    sw = 13
    m = "M10,100 V58 A21,21 0 0 1 52,58 V100 M52,58 A21,21 0 0 1 94,58 V100"
    i = "M120,100 V40"
    a = math.radians(42)
    cx, cy, r = 177.5, 68.5, 31.5
    p1 = (cx + r * math.cos(a), cy - r * math.sin(a))
    p2 = (cx + r * math.cos(a), cy + r * math.sin(a))
    c = f"M{p1[0]:.1f},{p1[1]:.1f} A{r},{r} 0 1 0 {p2[0]:.1f},{p2[1]:.1f}"
    return (f'<g transform="translate({x},{y}) scale({scale})" fill="none" stroke="{ink}" stroke-width="{sw}" '
            f'stroke-linecap="round" stroke-linejoin="round"><path d="{m}"/><path d="{i}"/><path d="{c}"/>'
            f'<rect x="113.5" y="9" width="13" height="13" rx="2.5" fill="{dot}" stroke="none"/></g>')


def logo(theme: str) -> str:
    t = THEMES[theme]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 352 112" width="352" height="112" role="img" '
            f'aria-label="mic">{mark(0, 6, 100)}{wordmark(128, 0, 1, t["ink"], t["mint"])}</svg>')


def mark_svg() -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="256" height="256">{mark()}</svg>'


# ---------------------------------------------------------------- link map
def linkmap(x, y, w, h, t, animate=False) -> str:
    n = D["n"] + len(D["fileCounts"]) * 1.2
    cell = math.floor(math.sqrt((w * h) / n) * 0.93)
    cols = int(w // cell)
    pos, px, py, k = [], 0, 0, 0
    for count in D["fileCounts"]:
        for _ in range(count):
            if px >= cols:
                px, py = 0, py + 1
            pos.append((x + px * cell, y + py * cell))
            px, k = px + 1, k + 1
        px += 1
    seeds = {s[0] for s in D["seeds"]}
    links = {s[0] for s in D["links"]}
    pad = max(1.0, cell * 0.16)
    out = []
    for i, (cx, cy) in enumerate(pos):
        if i in seeds or i in links:
            continue
        out.append(f'<rect x="{cx + pad / 2:.1f}" y="{cy + pad / 2:.1f}" width="{cell - pad:.1f}" height="{cell - pad:.1f}" fill="{t["cell"]}"/>')
    ctr = lambda i: (pos[i][0] + cell / 2, pos[i][1] + cell / 2)  # noqa: E731
    edges = []
    for j, l in enumerate(D["links"]):
        (ax, ay), (bx, by) = ctr(l[0]), ctr(D["seeds"][j % len(D["seeds"])][0])
        edges.append(f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}"/>')
    anim = ' class="lm-edges"' if animate else ""
    out.append(f'<g{anim} stroke="{t["link"]}" stroke-width="1.2" opacity="0.55">{"".join(edges)}</g>')
    for l in D["links"]:
        cx, cy = pos[l[0]]
        out.append(f'<rect x="{cx + pad / 2 - 1:.1f}" y="{cy + pad / 2 - 1:.1f}" width="{cell - pad + 2:.1f}" height="{cell - pad + 2:.1f}" fill="{t["link"]}"/>')
    for s in D["seeds"]:
        cx, cy = pos[s[0]]
        out.append(f'<rect x="{cx - 1:.1f}" y="{cy - 1:.1f}" width="{cell + 2:.1f}" height="{cell + 2:.1f}" rx="1.5" fill="{t["mint"]}"/>')
    return "".join(out), py * cell + cell


# ---------------------------------------------------------------- banner (1280x640, also the social preview)
def banner(theme: str) -> str:
    t = THEMES[theme]
    W, H = 1280, 640
    grid, gh = linkmap(720, 150, 470, 330, t)
    left = 88
    label = "10.7× fewer tokens per answer"
    cw = len(label) * 10.6 + 36
    chips = [f'<rect x="{left}" y="472" width="{cw:.0f}" height="40" rx="20" fill="{t["mintsoft"]}"/>'
             f'<text x="{left + 18}" y="498" font-family="{SANS}" font-size="17" font-weight="700" fill="{t["mint"]}">{esc(label)}</text>'
             f'<text x="{left + cw + 18:.0f}" y="498" font-family="{SANS}" font-size="17" fill="{t["muted"]}">Claude Code · Codex · OpenCode · ZCode</text>']
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img"
 aria-label="mic: send your agent the paragraphs that matter">
<defs>
 <linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{t["bg"]}"/><stop offset="1" stop-color="{t["bg2"]}"/></linearGradient>
 <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1.5" cy="1.5" r="1.2" fill="{t["line"]}"/></pattern>
</defs>
<rect width="{W}" height="{H}" fill="url(#g)"/>
<rect width="{W}" height="{H}" fill="url(#dots)" opacity="0.6"/>
{mark(left, 88, 64)}{wordmark(left + 82, 88, 0.62, t["ink"], t["mint"])}
<text x="{left + 230}" y="137" font-family="{MONO}" font-size="15" fill="{t["muted"]}" letter-spacing="1.5">MINTOCODE</text>
<text font-family="{SANS}" font-weight="800" font-size="58" letter-spacing="-1.5" fill="{t["ink"]}">
 <tspan x="{left}" y="236">Send your agent</tspan><tspan x="{left}" y="300">the paragraphs</tspan>
 <tspan x="{left}" y="364" fill="{t["mint"]}">that matter.</tspan></text>
<text font-family="{SANS}" font-size="20" fill="{t["muted"]}"><tspan x="{left}" y="412">Compile your repo once. mic links the functions, callers</tspan>
 <tspan x="{left}" y="440">and tests a question needs, and sends only those.</tspan></text>
{"".join(chips)}
<text x="{left}" y="566" font-family="{MONO}" font-size="15" fill="{t["muted"]}">github.com/Spearmintai/mintocode</text>
<rect x="696" y="96" width="518" height="{gh + 116:.0f}" rx="18" fill="{t["panel"]}" stroke="{t["line"]}"/>
<rect x="716" y="112" width="478" height="26" rx="7" fill="{t["bg"]}" stroke="{t["line"]}"/>
<text x="728" y="130" font-family="{MONO}" font-size="11.6" fill="{t["ink"]}"><tspan fill="{t["mint"]}" font-weight="700">? </tspan>{esc(D["question"])}</text>
{grid}
<text x="720" y="{150 + gh + 30:.0f}" font-family="{MONO}" font-size="13" fill="{t["muted"]}"><tspan fill="{t["mint"]}">■</tspan> {len(D["seeds"])} seeds   <tspan fill="{t["link"]}">■</tspan> {len(D["links"])} linked   of {D["n"]:,} paragraphs · {D["packTokens"]:,} tokens</text>
</svg>'''


# ---------------------------------------------------------------- terminal demo (animated, loops)
DEMO = [
    ("cmd", "mic compile ."),
    ("log", "[mic] 211 files, 33,960 lines, 12 modules"),
    ("log", "[mic] stage 5: citations verified 961, unresolved 98"),
    ("log", "[mic] done in 541s, $13.74 (64 calls, 0 cached)"),
    ("gap", ""),
    ("cmd", 'mic ask "Why can\'t I add routes to a schematic after it is registered?"'),
    ("out", "Because a schematic locks its own configuration the first time it is enrolled."),
    ("out", "1. `route` is a @preparationmethod (skeleton.py:L42-49), which runs"),
    ("out", "   _verify_configuration_completed before every call."),
    ("out", "2. Schematic overrides it (schematics.py:L213-221): once"),
    ("out", "   _got_registered_once is True it raises AssertionError."),
    ("out", "Fix: define every route before app.enroll_plan(bp)."),
    ("gap", ""),
    ("ok", "[mic] pack 3,451 tokens -> 6,711 total, $0.0320, 8.7s"),
]


def demo(theme: str) -> str:
    t = THEMES[theme]
    W, lh, top = 940, 25, 74
    H = top + lh * len(DEMO) + 34
    T = 16.0  # loop seconds
    times, at = [], 0.6
    for kind, _ in DEMO:
        times.append(at)
        at += {"cmd": 1.4, "log": 0.55, "out": 0.35, "gap": 0.25, "ok": 0.5}[kind]
    css, rows = [], []
    for k, ((kind, text), start) in enumerate(zip(DEMO, times)):
        s = start / T * 100
        css.append(f"@keyframes l{k}{{0%,{s:.2f}%{{opacity:0}}{s + 0.8:.2f}%,94%{{opacity:1}}98%,100%{{opacity:0}}}}"
                   f".l{k}{{animation:l{k} {T}s linear infinite}}")
        y = top + k * lh
        if kind == "cmd":
            body = (f'<tspan fill="{t["mint"]}">❯ </tspan><tspan fill="#ffffff" font-weight="700">{esc(text.split(" ", 2)[0])} '
                    f'{esc(text.split(" ", 2)[1])}</tspan>' + (f' {esc(text.split(" ", 2)[2])}' if text.count(" ") >= 2 else ""))
            fill = "#e8f3ee"
        elif kind == "log":
            body, fill = esc(text), "#8fa69c"
        elif kind == "ok":
            body, fill = esc(text), "#5cd2a3"
        else:
            body, fill = esc(text).replace("`", ""), "#d9e8e1"
        rows.append(f'<text class="l{k}" x="28" y="{y}" font-family="{MONO}" font-size="15" fill="{fill}">{body}</text>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img"
 aria-label="Terminal: mic compile, then mic ask answers in one call with 6,711 tokens">
<style>{"".join(css)}@media (prefers-reduced-motion: reduce){{text{{animation:none!important;opacity:1!important}}}}</style>
<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="14" fill="{t["term"]}" stroke="{t["line"]}" stroke-width="2"/>
<circle cx="28" cy="27" r="6.5" fill="#ff5f57"/><circle cx="50" cy="27" r="6.5" fill="#febc2e"/><circle cx="72" cy="27" r="6.5" fill="#28c840"/>
<text x="{W / 2}" y="32" text-anchor="middle" font-family="{MONO}" font-size="13" fill="#7f968c">~/quill · mic</text>
<line x1="1" y1="48" x2="{W - 1}" y2="48" stroke="#1d2f28"/>
{"".join(rows)}
</svg>'''


# ---------------------------------------------------------------- benchmark chart
ROWS = [
    ("Claude Code, exploring by itself", 78.7, 77900, 0.061, False),
    ("Claude Code + mic hooks", 79.3, 67100, 0.065, True),
    ("mic ask · link pack, one call", 72.9, 7300, 0.036, True),
    ("mic ask + local Laya judge", 71.5, 6300, 0.032, True),
]


def bench(theme: str) -> str:
    t = THEMES[theme]
    W, top, rh, x0, bw = 940, 92, 58, 330, 350
    H = top + rh * len(ROWS) + 56
    rows = []
    for k, (name, score, tok, cost, ours) in enumerate(ROWS):
        y = top + k * rh
        w = bw * tok / ROWS[0][2]
        col = t["mint"] if ours else t["muted"]
        rows.append(
            f'<text x="24" y="{y + 20}" font-family="{SANS}" font-size="16" font-weight="{700 if k == 2 else 400}" fill="{t["ink"]}">{esc(name)}</text>'
            f'<rect x="{x0}" y="{y + 6}" width="{max(w, 3):.1f}" height="20" rx="4" fill="{col}" opacity="{1 if ours else 0.55}"/>'
            f'<text x="{x0 + max(w, 3) + 10:.1f}" y="{y + 21}" font-family="{MONO}" font-size="14" fill="{t["ink"]}">{tok:,}</text>'
            f'<text x="{W - 118}" y="{y + 21}" font-family="{MONO}" font-size="14" fill="{t["ink"]}" text-anchor="end">{score}</text>'
            f'<text x="{W - 24}" y="{y + 21}" font-family="{MONO}" font-size="14" fill="{t["muted"]}" text-anchor="end">${cost:.3f}</text>'
            f'<line x1="24" y1="{y + rh - 12}" x2="{W - 24}" y2="{y + rh - 12}" stroke="{t["line"]}"/>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img"
 aria-label="Tokens per answer on 48 de-memorized SWE-QA questions">
<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="14" fill="{t["panel"]}" stroke="{t["line"]}" stroke-width="2"/>
<text x="24" y="40" font-family="{SANS}" font-size="20" font-weight="800" fill="{t["ink"]}">Tokens per answer</text>
<text x="24" y="64" font-family="{SANS}" font-size="14" fill="{t["muted"]}">48 SWE-QA questions on a de-memorized repo · Sonnet · bars to scale</text>
<text x="{W - 118}" y="64" font-family="{MONO}" font-size="12" fill="{t["muted"]}" text-anchor="end" letter-spacing="1">SCORE</text>
<text x="{W - 24}" y="64" font-family="{MONO}" font-size="12" fill="{t["muted"]}" text-anchor="end" letter-spacing="1">COST</text>
{"".join(rows)}
<text x="24" y="{H - 20}" font-family="{SANS}" font-size="13" fill="{t["muted"]}">Score: SWE-QA's strict judge, 0-100. Reproduce with bench/.</text>
</svg>'''


def main() -> None:
    out = {}
    for th in THEMES:
        out[f"logo-{th}.svg"] = logo(th)
        out[f"banner-{th}.svg"] = banner(th)
        out[f"demo-{th}.svg"] = demo(th)
        out[f"bench-{th}.svg"] = bench(th)
    out["mark.svg"] = mark_svg()
    for name, svg in out.items():
        with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
            f.write(svg)
    print(f"wrote {len(out)} SVGs to {HERE}")
    if "--png" in sys.argv:
        import cairosvg
        cairosvg.svg2png(bytestring=out["banner-dark.svg"].encode(), write_to=os.path.join(HERE, "social-preview.png"),
                         output_width=1280, output_height=640)
        print("wrote social-preview.png (upload it under Settings → General → Social preview)")


if __name__ == "__main__":
    main()

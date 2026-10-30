#!/usr/bin/env python3
"""Render README charts as static SVG in light and dark variants.

Usage: svgchart.py <data.json> <out_dir> [--image-prefix docs/figures]

Reads the same data.json the landing page uses (schema: the "Figures" section of
README.md), writes <name>-light.svg and <name>-dark.svg for every chart, and prints
a Markdown snippet per chart: a <picture> element and a collapsed data table.
Standard library only.
"""

import json
import math
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from xml.sax.saxutils import escape

WIDTH = 760
PAD = 24
FONT = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MAX_SERIES = 4

THEMES = {
    "light": {
        "surface": "#ffffff",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#6f6d68",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
        "good": "#0ca30c",
        "critical": "#d03b3b",
    },
    "dark": {
        "surface": "#0d1117",
        "ink": "#f0efec",
        "ink2": "#c3c2b7",
        "muted": "#9a988f",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500"],
        "good": "#0ca30c",
        "critical": "#d03b3b",
    },
}


def fixed(value: float, places: int) -> Decimal:
    """Round half away from zero on the exact binary value, as JavaScript toFixed does."""
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def percent_places(scaled: float) -> int:
    """Whole numbers print bare; rates under 1% keep two decimals so 0.86% does not print as 0.9%."""
    if abs(scaled - round(scaled)) < 0.05:
        return 0
    return 2 if abs(scaled) < 1 else 1


def fmt(value: float, style: str = "number") -> str:
    if style == "percent":
        scaled = value * 100
        return f"{fixed(scaled, percent_places(scaled))}%"
    if style == "decimal2":
        return str(fixed(value, 2))
    if style == "decimal3":
        return str(fixed(value, 3))
    if abs(value) >= 1000:
        return f"{fixed(value, 0):,}"
    return f"{value:g}"


def text_width(text: str, size: float) -> float:
    return len(text) * size * 0.56


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    if high <= low:
        high = low + 1
    raw_step = (high - low) / count
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw_step)
    start = math.floor(low / step) * step
    ticks, value = [], start
    while value <= high + step * 0.001:
        ticks.append(round(value, 10))
        value += step
    if ticks[-1] < high:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def text(x, y, content, size=13, fill="#000", weight=400, anchor="start", halo=None) -> str:
    """halo paints a surface-colored outline so a label stays legible over gridlines."""
    outline = (
        f' stroke="{halo}" stroke-width="4" stroke-linejoin="round" paint-order="stroke"'
        if halo
        else ""
    )
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}"{outline}>{escape(str(content))}</text>'
    )


def line(x1, y1, x2, y2, stroke, width=1) -> str:
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{stroke}" stroke-width="{width}"/>'
    )


def bar_path(x, y, w, h, direction: str, radius: float = 4) -> str:
    """Bar with a 4px rounded data end and a square baseline end."""
    r = min(radius, w / 2 if direction == "up" else h / 2, w if direction == "right" else h)
    if direction == "right":
        return (
            f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}"
            f"a{r},{r} 0 0 1 -{r},{r}h-{w - r:.1f}z"
        )
    return (
        f"M{x:.1f},{y + h:.1f}v-{h - r:.1f}a{r},{r} 0 0 1 {r},-{r}h{w - 2 * r:.1f}"
        f"a{r},{r} 0 0 1 {r},{r}v{h - r:.1f}z"
    )


def header(chart: dict, theme: dict) -> tuple[list[str], float]:
    parts = [text(PAD, 30, chart["title"], 16, theme["ink"], 600)]
    top = 44
    if chart.get("subtitle"):
        parts.append(text(PAD, 50, chart["subtitle"], 13, theme["ink2"]))
        top = 64
    return parts, top


def legend(names: list[str], theme: dict, y: float, shape: str) -> tuple[list[str], float]:
    if len(names) < 2:
        return [], y
    parts, x = [], PAD
    for index, name in enumerate(names):
        color = theme["series"][index]
        if shape == "line":
            parts.append(line(x, y - 4, x + 14, y - 4, color, 2))
        else:
            parts.append(
                f'<rect x="{x}" y="{y - 10}" width="12" height="12" rx="2" fill="{color}"/>'
            )
        parts.append(text(x + 20, y, name, 12, theme["ink2"]))
        x += 20 + text_width(name, 12) + 18
    return parts, y + 16


def footer(chart: dict, theme: dict, y: float) -> list[str]:
    return [text(PAD, y, f"Source: {chart['source']}", 11, theme["muted"])]


def render_hbar(chart: dict, theme: dict) -> tuple[list[str], float]:
    parts, top = header(chart, theme)
    style = chart.get("format", "number")
    rows = chart["data"]
    label_w = min(260, max(text_width(r["label"], 13) for r in rows) + 12)
    domain_max = chart.get("max") or max(r["value"] for r in rows) * 1.1
    x0, x1 = PAD + label_w, WIDTH - PAD - 90
    scale = (x1 - x0) / domain_max
    band, thick = 34, 20
    plot_top = top + 18
    plot_bottom = plot_top + band * len(rows)
    for tick in nice_ticks(0, domain_max, 4):
        if tick > domain_max * 1.0001:
            continue
        x = x0 + tick * scale
        parts.append(line(x, plot_top - 6, x, plot_bottom, theme["grid"]))
        parts.append(
            text(x, plot_bottom + 16, fmt(tick, style), 11, theme["muted"], anchor="middle")
        )
    if "threshold" in chart:
        tx = x0 + chart["threshold"]["value"] * scale
        parts.append(line(tx, plot_top - 10, tx, plot_bottom, theme["ink2"], 1.5))
        parts.append(
            text(
                tx + 4,
                plot_top - 12,
                chart["threshold"]["label"],
                11,
                theme["ink2"],
                halo=theme["surface"],
            )
        )
    for index, row in enumerate(rows):
        y = plot_top + index * band + (band - thick) / 2
        status = row.get("status")
        color = (
            theme["good"]
            if status == "pass"
            else theme["critical"]
            if status == "fail"
            else theme["series"][0]
        )
        width = max(row["value"] * scale, 1)
        parts.append(text(PAD, y + thick / 2 + 4.5, row["label"], 13, theme["ink"]))
        parts.append(f'<path d="{bar_path(x0, y, width, thick, "right")}" fill="{color}"/>')
        value_label = fmt(row["value"], style) + (f"  {status}" if status else "")
        label_x = x0 + width + 6
        parts.append(
            f'<rect x="{label_x - 3:.1f}" y="{y + 1:.1f}" width="{text_width(value_label, 13) + 8:.1f}" '
            f'height="{thick - 2}" fill="{theme["surface"]}"/>'
        )
        parts.append(text(label_x, y + thick / 2 + 4.5, value_label, 13, theme["ink"], 600))
    parts.append(line(x0, plot_top - 6, x0, plot_bottom, theme["axis"]))
    return parts + footer(chart, theme, plot_bottom + 40), plot_bottom + 52


def y_axis(ticks, scale_y, plot_left, plot_right, style, theme) -> list[str]:
    parts = []
    for tick in ticks:
        y = scale_y(tick)
        parts.append(
            line(plot_left, y, plot_right, y, theme["axis"] if tick == ticks[0] else theme["grid"])
        )
        parts.append(text(plot_left - 8, y + 4, fmt(tick, style), 11, theme["muted"], anchor="end"))
    return parts


def render_columns(chart: dict, theme: dict) -> tuple[list[str], float]:
    parts, top = header(chart, theme)
    series = chart["series"][:MAX_SERIES]
    legend_parts, top = legend([s["name"] for s in series], theme, top + 8, "rect")
    parts += legend_parts
    style = chart.get("format", "number")
    categories = chart["categories"]
    ticks = nice_ticks(0, chart.get("max") or max(max(s["values"]) for s in series))
    plot_left, plot_right, plot_top, plot_bottom = PAD + 44, WIDTH - PAD, top + 14, top + 234
    scale_y = lambda v: (
        plot_bottom - (v - ticks[0]) / (ticks[-1] - ticks[0]) * (plot_bottom - plot_top)
    )
    parts += y_axis(ticks, scale_y, plot_left, plot_right, style, theme)
    slot = (plot_right - plot_left) / len(categories)
    bar_w = min(24, (slot * 0.7 - 2 * (len(series) - 1)) / len(series))
    group_w = bar_w * len(series) + 2 * (len(series) - 1)
    label_w = max(text_width(str(c), 11) for c in categories) + 8
    every = max(1, math.ceil(label_w / slot))
    for ci, category in enumerate(categories):
        gx = plot_left + ci * slot + (slot - group_w) / 2
        for si, s in enumerate(series):
            value = s["values"][ci]
            x, y = gx + si * (bar_w + 2), scale_y(value)
            parts.append(
                f'<path d="{bar_path(x, y, bar_w, max(plot_bottom - y, 1), "up")}" '
                f'fill="{theme["series"][si]}"/>'
            )
            if len(series) == 1 and len(categories) <= 12:
                parts.append(
                    text(x + bar_w / 2, y - 6, fmt(value, style), 11, theme["ink"], 600, "middle")
                )
        if ci % every == 0:
            parts.append(
                text(
                    plot_left + ci * slot + slot / 2,
                    plot_bottom + 18,
                    category,
                    11,
                    theme["ink2"],
                    anchor="middle",
                )
            )
    return parts + footer(chart, theme, plot_bottom + 44), plot_bottom + 56


END_LABEL_ROOM = 96


def end_label(name: str, value: str, named: bool) -> str:
    """Line end label; the series name is dropped when it would run past the right edge (the legend names it)."""
    labelled = f"{name} {value}"
    return labelled if named and text_width(labelled, 12) <= END_LABEL_ROOM else value


def render_line(chart: dict, theme: dict) -> tuple[list[str], float]:
    parts, top = header(chart, theme)
    series = chart["series"][:MAX_SERIES]
    legend_parts, top = legend([s["name"] for s in series], theme, top + 8, "line")
    parts += legend_parts
    style = chart.get("format", "number")
    xs = chart["x"]
    values = [v for s in series for v in s["values"] if v is not None]
    ticks = nice_ticks(chart.get("min", min(0, min(values))), chart.get("max") or max(values))
    plot_left, plot_right, plot_top, plot_bottom = PAD + 44, WIDTH - PAD - 110, top + 14, top + 234
    scale_y = lambda v: (
        plot_bottom - (v - ticks[0]) / (ticks[-1] - ticks[0]) * (plot_bottom - plot_top)
    )
    scale_x = lambda i: plot_left + i * (plot_right - plot_left) / max(len(xs) - 1, 1)
    parts += y_axis(ticks, scale_y, plot_left, plot_right, style, theme)
    every = max(1, math.ceil(len(xs) / 8))
    for i, label in enumerate(xs):
        if (len(xs) - 1 - i) % every == 0:
            parts.append(
                text(scale_x(i), plot_bottom + 18, label, 11, theme["muted"], anchor="middle")
            )
    end_points = []
    for si, s in enumerate(series):
        points = [(scale_x(i), scale_y(v)) for i, v in enumerate(s["values"]) if v is not None]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        color = theme["series"][si]
        parts.append(
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        ex, ey = points[-1]
        parts.append(
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{color}" '
            f'stroke="{theme["surface"]}" stroke-width="2"/>'
        )
        end_points.append(
            (ey, end_label(s["name"], fmt(s["values"][-1], style), len(series) > 1), ex)
        )
    ys = sorted(p[0] for p in end_points)
    if all(b - a >= 14 for a, b in zip(ys, ys[1:])):
        for ey, label, ex in end_points:
            parts.append(text(ex + 10, ey + 4, label, 12, theme["ink"], 600, halo=theme["surface"]))
    return parts + footer(chart, theme, plot_bottom + 44), plot_bottom + 56


def render_stats(chart: dict, theme: dict) -> tuple[list[str], float]:
    parts, top = header(chart, theme)
    tiles = chart["tiles"]
    per_row = min(4, len(tiles))
    tile_w = (WIDTH - 2 * PAD) / per_row
    for index, tile in enumerate(tiles):
        x = PAD + (index % per_row) * tile_w
        y = top + 16 + (index // per_row) * 96
        parts.append(line(x, y, x, y + 70, theme["axis"], 2))
        parts.append(text(x + 14, y + 34, tile["value"], 32, theme["ink"], 600))
        parts.append(text(x + 14, y + 54, tile["label"], 13, theme["ink2"]))
        if tile.get("note"):
            parts.append(text(x + 14, y + 70, tile["note"], 11, theme["muted"]))
    bottom = top + 16 + math.ceil(len(tiles) / per_row) * 96
    return parts + footer(chart, theme, bottom + 6), bottom + 18


RENDERERS = {
    "hbar": render_hbar,
    "columns": render_columns,
    "line": render_line,
    "stats": render_stats,
}


def render_svg(chart: dict, mode: str) -> str:
    theme = THEMES[mode]
    body, height = RENDERERS[chart["type"]](chart, theme)
    title, desc = escape(chart["title"]), escape(chart.get("alt", chart.get("subtitle", "")))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height:.0f}" '
        f'width="{WIDTH}" height="{height:.0f}" font-family="{FONT}" role="img">'
        f"<title>{title}</title><desc>{desc}</desc>"
        f'<rect width="100%" height="100%" fill="{theme["surface"]}"/>' + "".join(body) + "</svg>\n"
    )


def table_rows(chart: dict) -> list[list[str]]:
    style = chart.get("format", "number")
    kind = chart["type"]
    if kind == "hbar":
        return [["Item", "Value"]] + [
            [r["label"], fmt(r["value"], style) + (f" ({r['status']})" if r.get("status") else "")]
            for r in chart["data"]
        ]
    if kind == "stats":
        return [["Measure", "Value"]] + [[t["label"], t["value"]] for t in chart["tiles"]]
    keys = chart["categories"] if kind == "columns" else chart["x"]
    series = chart["series"][:MAX_SERIES]
    rows = [[""] + [s["name"] for s in series]]
    for i, key in enumerate(keys):
        rows.append(
            [str(key)]
            + ["" if s["values"][i] is None else fmt(s["values"][i], style) for s in series]
        )
    return rows


def markdown_snippet(chart: dict, prefix: str) -> str:
    name, alt = chart["name"], chart.get("alt", chart["title"])
    rows = table_rows(chart)
    table = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    table += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return (
        f'<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="{prefix}/{name}-dark.svg">\n'
        f'  <img alt="{escape(alt)}" src="{prefix}/{name}-light.svg" width="760">\n</picture>\n\n'
        f"<details><summary>Data table</summary>\n\n"
        + "\n".join(table)
        + f"\n\nSource: `{chart['source']}`\n\n</details>\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    data = json.loads(Path(argv[1]).read_text())
    out_dir = Path(argv[2])
    prefix = argv[argv.index("--image-prefix") + 1] if "--image-prefix" in argv else "docs/figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    for chart in data["charts"]:
        for mode in THEMES:
            (out_dir / f"{chart['name']}-{mode}.svg").write_text(render_svg(chart, mode))
        print(f"<!-- chart: {chart['name']} -->\n" + markdown_snippet(chart, prefix))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

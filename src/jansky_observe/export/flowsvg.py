"""Deterministic inline-SVG flow diagrams for the guide PDFs (roadmap M8).

WeasyPrint renders no JavaScript, so mermaid can't run at PDF-build time. These
helpers draw the same idea — a labeled node-and-arrow flow — as static SVG that
embeds directly in the guide HTML and prints cleanly. The node labels are the
contract: a build stage's diagram nodes are the exact strings its checkbox parts
list repeats, so a reader maps box → part → checkbox.

Pure Python, no dependencies; output is stable for the same input (safe to embed
in a reproducible PDF).
"""

from __future__ import annotations

from html import escape

__all__ = ["vertical_flow_svg"]


def vertical_flow_svg(nodes: list[str], *, width: int = 460) -> str:
    """Render a top-to-bottom flow of labeled rounded-rect nodes joined by arrows.

    Parameters
    ----------
    nodes : list of str
        Node labels in flow order (2+ for arrows to appear). Each becomes one
        box; the labels double as the stage's parts-list entries.
    width : int, optional
        SVG width in px (default 460); boxes span it with a small margin.

    Returns
    -------
    str
        A self-contained ``<svg>`` element (its own arrowhead marker in
        ``<defs>``), themeable via ``currentColor`` for strokes/arrows and an
        explicit light fill so it reads on the printed page.
    """
    if not nodes:
        return ""
    box_h, gap, pad, margin = 44, 28, 10, 8
    box_w = width - 2 * margin
    n = len(nodes)
    height = n * box_h + (n - 1) * gap + 2 * pad

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" class="flow" role="img">',
        '<defs><marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>',
    ]
    for i, label in enumerate(nodes):
        y = pad + i * (box_h + gap)
        cx = width / 2
        parts.append(
            f'<rect x="{margin}" y="{y}" width="{box_w}" height="{box_h}" rx="7" '
            f'fill="#f4f6fb" stroke="currentColor" stroke-width="1.2"/>'
        )
        parts.append(
            f'<text x="{cx:.0f}" y="{y + box_h / 2:.0f}" text-anchor="middle" '
            f'dominant-baseline="central" font-size="13" fill="#0b0e14">{escape(label)}</text>'
        )
        if i < n - 1:
            y1 = y + box_h
            y2 = y + box_h + gap
            parts.append(
                f'<line x1="{cx:.0f}" y1="{y1}" x2="{cx:.0f}" y2="{y2}" '
                f'stroke="currentColor" stroke-width="1.2" marker-end="url(#flow-arrow)"/>'
            )
    parts.append("</svg>")
    return "".join(parts)

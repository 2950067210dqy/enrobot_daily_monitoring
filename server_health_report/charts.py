from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, Rect, String
from reportlab.lib import colors
from reportlab.lib.units import mm


PALETTE = ["#1976D2", "#E65100", "#2E7D32", "#7B1FA2", "#C62828", "#00838F"]


def trend_chart(
    title: str,
    labels: Sequence[str],
    series: Dict[str, Sequence[Optional[float]]],
    width: float = 84 * mm,
    height: float = 40 * mm,
    percent: bool = False,
) -> Drawing:
    drawing = Drawing(width, height)
    series_count = max(1, len(series))
    legend_columns = 2 if width >= 120 * mm else 2
    legend_rows = (series_count + legend_columns - 1) // legend_columns
    left, right, bottom, top = 12 * mm, 4 * mm, (8 + legend_rows * 4) * mm, 9 * mm
    plot_w, plot_h = width - left - right, height - bottom - top
    drawing.add(String(2, height - 12, title, fontName="STSong-Light", fontSize=11, fillColor=colors.HexColor("#17365D")))
    values = [float(value) for items in series.values() for value in items if value is not None]
    max_value = 100.0 if percent else (max(values) if values else 1.0)
    if not percent:
        max_value = max(1.0, max_value * 1.15)
    drawing.add(Line(left, bottom, left, bottom + plot_h, strokeColor=colors.HexColor("#B0BEC5"), strokeWidth=0.5))
    drawing.add(Line(left, bottom, left + plot_w, bottom, strokeColor=colors.HexColor("#B0BEC5"), strokeWidth=0.5))
    for step in range(3):
        y = bottom + plot_h * step / 2.0
        drawing.add(Line(left, y, left + plot_w, y, strokeColor=colors.HexColor("#ECEFF1"), strokeWidth=0.35))
        label = max_value * step / 2.0
        suffix = "%" if percent else ""
        drawing.add(String(1, y - 2.5, f"{label:.0f}{suffix}", fontName="STSong-Light", fontSize=8, fillColor=colors.HexColor("#607D8B")))
    count = max(1, len(labels))
    for index, (name, items) in enumerate(series.items()):
        color = colors.HexColor(PALETTE[index % len(PALETTE)])
        points = []
        for position, value in enumerate(items):
            if value is None:
                continue
            x = left + (plot_w / max(1, count - 1)) * position if count > 1 else left + plot_w / 2
            y = bottom + plot_h * float(value) / max_value
            points.extend([x, y])
            drawing.add(Circle(x, y, 1.5, fillColor=color, strokeColor=None))
            value_text = f"{float(value):.0f}{'%' if percent else ''}"
            drawing.add(String(x + 2, y + 2, value_text, fontName="STSong-Light", fontSize=7.5, fillColor=color))
        if len(points) >= 4:
            drawing.add(PolyLine(points, strokeColor=color, strokeWidth=1.2, fillColor=None))
        legend_x = left + (index % legend_columns) * ((width - left - right) / legend_columns)
        legend_y = 5.5 * mm + (index // legend_columns) * 4 * mm
        drawing.add(Rect(legend_x, legend_y, 2.5 * mm, 1.4 * mm, fillColor=color, strokeColor=None))
        drawing.add(String(legend_x + 3 * mm, legend_y, name, fontName="STSong-Light", fontSize=8, fillColor=colors.HexColor("#455A64")))
    if len(labels) > 1:
        drawing.add(String(left, 1, labels[0], fontName="STSong-Light", fontSize=7.5, fillColor=colors.HexColor("#78909C")))
        drawing.add(String(left + plot_w - 23 * mm, 1, labels[-1], fontName="STSong-Light", fontSize=7.5, fillColor=colors.HexColor("#78909C")))
    else:
        drawing.add(String(left + plot_w / 2 - 9 * mm, 1, "单次采样", fontName="STSong-Light", fontSize=8, fillColor=colors.HexColor("#78909C")))
    return drawing

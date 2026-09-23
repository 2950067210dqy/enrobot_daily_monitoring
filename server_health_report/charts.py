from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from reportlab.graphics import renderPM
from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, Rect, String
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


PALETTE = ["#1976D2", "#E65100", "#2E7D32", "#7B1FA2", "#C62828", "#00838F"]
CHART_FONT_NAME = "ServerHealthChartChinese"


def _available_renderpm_backend() -> Optional[str]:
    """优先使用随ReportLab 3或renderpm extra提供的本地渲染后端。"""
    try:
        from reportlab.graphics import _renderPM  # noqa: F401
    except ImportError:
        return None
    return "_renderPM"


RENDER_PM_BACKEND = _available_renderpm_backend()


def register_chart_font() -> str:
    """注册同时支持PNG栅格化和中文显示的图表字体。

    ReportLab的CID字体适合直接写入PDF，却不能被renderPM稳定栅格化。图表改为PNG后，
    必须使用实际的TrueType/OpenType字体文件，因此优先采用显式环境变量，其次查找
    Windows及常见Linux中文字体。

    Returns:
        str: 已注册、可供图表文字节点使用的ReportLab字体名称。

    Raises:
        RuntimeError: 当前机器没有可用于渲染中文PNG的字体文件。
    """
    if CHART_FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return CHART_FONT_NAME
    configured_font = os.getenv("SERVER_HEALTH_CHART_FONT", "").strip()
    candidates = [
        configured_font,
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    errors: List[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(CHART_FONT_NAME, str(path), subfontIndex=0))
            return CHART_FONT_NAME
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    detail = "；".join(errors[:3]) or "未找到候选字体文件"
    raise RuntimeError(
        "无法生成中文图表PNG，请安装中文TrueType字体，或通过"
        f"SERVER_HEALTH_CHART_FONT指定字体文件。详情：{detail}"
    )


def _safe_chart_name(value: str, limit: int = 90) -> str:
    """把业务图表标题转换成可在Windows中安全使用的文件名片段。

    Args:
        value: 图表标题或服务器名称。
        limit: 文件名片段的最大字符数。

    Returns:
        str: 已移除路径分隔符和Windows保留字符的名称。
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "图表"))
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
    return (cleaned or "图表")[:limit]


class ChartImageWriter:
    """将报告中的每张业务图表保存为独立PNG，并记录本次生成清单。"""

    def __init__(self, directory: Path, dpi: int = 180) -> None:
        """初始化图表图片输出器并清理本工具上次生成的同目录图片。

        Args:
            directory: 与PDF配套的图表图片目录。
            dpi: PNG输出分辨率；显示尺寸仍由PDF中的点数决定。
        """
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.dpi = dpi
        self.paths: List[Path] = []
        # 只清理本工具固定前缀的PNG，避免旧图片被误认为本次报告图表。
        for old_path in self.directory.glob("chart_*.png"):
            if old_path.is_file():
                old_path.unlink()

    def write(self, drawing: Drawing, label: str) -> Path:
        """把一张ReportLab图表栅格化为PNG。

        Args:
            drawing: 已完成数据、坐标轴、图例和标签布局的矢量图表。
            label: 用于生成可读文件名的业务标题。

        Returns:
            Path: 已写出的PNG绝对或相对路径。
        """
        register_chart_font()
        sequence = len(self.paths) + 1
        path = self.directory / f"chart_{sequence:03d}_{_safe_chart_name(label)}.png"
        render_options = {
            "fmt": "PNG",
            "dpi": self.dpi,
            "bg": colors.white,
        }
        if RENDER_PM_BACKEND:
            render_options["backend"] = RENDER_PM_BACKEND
        try:
            renderPM.drawToFile(drawing, str(path), **render_options)
        except Exception as exc:
            if "renderPM backend" in str(exc) or "rlPyCairo" in str(exc):
                raise RuntimeError(
                    "ReportLab图表渲染后端不可用。请双击项目根目录的"
                    "repair_reportlab.cmd修复依赖。"
                ) from exc
            raise
        self.paths.append(path)
        return path


def trend_chart(
    title: str,
    labels: Sequence[str],
    series: Dict[str, Sequence[Optional[float]]],
    width: float = 84 * mm,
    height: float = 40 * mm,
    percent: bool = False,
) -> Drawing:
    """绘制服务器资源或线程指标的多时间点趋势图。

    Args:
        title: 图表标题。
        labels: 横坐标巡检时间标签。
        series: 图例名称到数据序列的映射。
        width: 图表显示宽度。
        height: 图表显示高度。
        percent: 是否使用百分比纵轴。

    Returns:
        Drawing: 后续会被ChartImageWriter输出为PNG的完整图表。
    """
    font_name = register_chart_font()
    drawing = Drawing(width, height)
    series_count = max(1, len(series))
    legend_columns = 2
    legend_rows = (series_count + legend_columns - 1) // legend_columns
    # 多序列数值统一放在标题下方的顶部留白区；序列较多时自动扩大留白。
    stacked_label_space = 26 + max(0, series_count - 1) * 8
    left, right = 12 * mm, 4 * mm
    bottom = (8 + legend_rows * 4) * mm
    top = max(14 * mm, stacked_label_space)
    plot_w, plot_h = width - left - right, height - bottom - top
    drawing.add(String(2, height - 12, title, fontName=font_name, fontSize=11, fillColor=colors.HexColor("#17365D")))
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
        drawing.add(String(1, y - 2.5, f"{label:.0f}{suffix}", fontName=font_name, fontSize=8, fillColor=colors.HexColor("#607D8B")))
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
            if series_count > 1:
                # x与数据点、时间刻度完全一致，只在y方向按图例顺序堆叠。
                label_y = height - 22 - index * 8
                drawing.add(String(x, label_y, value_text, fontName=font_name, fontSize=7.2, textAnchor="middle", fillColor=color))
            else:
                drawing.add(String(x + 2, y + 2, value_text, fontName=font_name, fontSize=7.5, fillColor=color))
        if len(points) >= 4:
            drawing.add(PolyLine(points, strokeColor=color, strokeWidth=1.2, fillColor=None))
        legend_x = left + (index % legend_columns) * ((width - left - right) / legend_columns)
        legend_y = 5.5 * mm + (index // legend_columns) * 4 * mm
        drawing.add(Rect(legend_x, legend_y, 2.5 * mm, 1.4 * mm, fillColor=color, strokeColor=None))
        drawing.add(String(legend_x + 3 * mm, legend_y, name, fontName=font_name, fontSize=8, fillColor=colors.HexColor("#455A64")))
    if len(labels) > 1:
        label_positions = list(range(len(labels))) if len(labels) <= 6 else sorted({round(index * (len(labels) - 1) / 5.0) for index in range(6)})
        for position in label_positions:
            label = labels[position]
            x = left + (plot_w / max(1, len(labels) - 1)) * position
            drawing.add(String(x, 1, str(label), fontName=font_name, fontSize=7.2, textAnchor="middle", fillColor=colors.HexColor("#78909C")))
    else:
        single_label = str(labels[0]) if labels else "单次采样"
        drawing.add(String(left + plot_w / 2, 1, single_label, fontName=font_name, fontSize=8, textAnchor="middle", fillColor=colors.HexColor("#78909C")))
    return drawing

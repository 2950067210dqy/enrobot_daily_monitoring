from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    CondPageBreak,
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .charts import trend_chart
from .logs import SEVERITY_ORDER, sort_groups
from .models import LogGroup, ServerSeries, Snapshot


STATUS_COLORS = {
    "异常": colors.HexColor("#C62828"),
    "警告": colors.HexColor("#EF6C00"),
    "不可判定": colors.HexColor("#7B1FA2"),
    "正常": colors.HexColor("#2E7D32"),
    "信息": colors.HexColor("#607D8B"),
    "部分不可判定": colors.HexColor("#7B1FA2"),
    "未识别": colors.HexColor("#455A64"),
}
SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
    "info": "信息",
    "unassessed": "未评估",
}
SEVERITY_COLORS = {
    "critical": colors.HexColor("#B71C1C"),
    "high": colors.HexColor("#E65100"),
    "medium": colors.HexColor("#F9A825"),
    "low": colors.HexColor("#607D8B"),
    "info": colors.HexColor("#2E7D32"),
    "unassessed": colors.HexColor("#455A64"),
}
CATEGORY_ORDER = ("应用日志目录日志", "Docker日志", "系统/进程日志")


def _clean(value: object) -> str:
    """清理不能安全写入ReportLab Paragraph的控制字符。"""
    import re
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or "")).strip()


def _short(value: object, limit: int = 220) -> str:
    """截断过长日志样例，控制PDF表格宽度和页数。"""
    text = _clean(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _styles() -> Dict[str, ParagraphStyle]:
    """创建服务器巡检报告统一使用的中文排版样式。"""
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title_cn", parent=base["Title"], fontName="STSong-Light", fontSize=18, leading=25, alignment=TA_CENTER, textColor=colors.HexColor("#17365D")),
        "h1": ParagraphStyle("h1_cn", parent=base["Heading1"], fontName="STSong-Light", fontSize=15, leading=21, spaceBefore=4, spaceAfter=6, textColor=colors.HexColor("#17365D")),
        "h2": ParagraphStyle("h2_cn", parent=base["Heading2"], fontName="STSong-Light", fontSize=11.5, leading=17, spaceBefore=6, spaceAfter=4, textColor=colors.HexColor("#1F4E79")),
        "body": ParagraphStyle("body_cn", parent=base["BodyText"], fontName="STSong-Light", fontSize=9.5, leading=14, textColor=colors.HexColor("#263238")),
        "small": ParagraphStyle("small_cn", parent=base["BodyText"], fontName="STSong-Light", fontSize=8.5, leading=12, textColor=colors.HexColor("#455A64")),
        "table": ParagraphStyle("table_cn", parent=base["BodyText"], fontName="STSong-Light", fontSize=9, leading=13, textColor=colors.HexColor("#263238")),
        "center": ParagraphStyle("center_cn", parent=base["BodyText"], fontName="STSong-Light", fontSize=9, leading=13, alignment=TA_CENTER, textColor=colors.HexColor("#263238")),
        "header": ParagraphStyle("header_cn", parent=base["BodyText"], fontName="STSong-Light", fontSize=8.8, leading=12.5, alignment=TA_CENTER, textColor=colors.white),
        "card_title": ParagraphStyle("card_title", parent=base["Heading2"], fontName="STSong-Light", fontSize=11, leading=15, textColor=colors.HexColor("#17365D")),
        "card_value": ParagraphStyle("card_value", parent=base["BodyText"], fontName="STSong-Light", fontSize=9, leading=12.5, alignment=TA_CENTER, textColor=colors.HexColor("#263238")),
    }


def _p(value: object, style: ParagraphStyle) -> Paragraph:
    """将业务文本安全转换为支持自动换行的PDF段落。"""
    text = _clean(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
    return Paragraph(text or "-", style)


def _table_style(header: colors.Color = colors.HexColor("#235A85")) -> TableStyle:
    """返回表头白字、隔行底色和网格线的通用表格样式。"""
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B7C9D6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FA")]),
    ])


def _status_paragraph(status: str, styles: Dict[str, ParagraphStyle]) -> Paragraph:
    """按正常、警告、异常等业务状态生成彩色文本。"""
    style = ParagraphStyle("status_" + status, parent=styles["center"], textColor=STATUS_COLORS.get(status, colors.black))
    return _p(status, style)


def _metric_bar(value: Optional[float], width: float = 27 * mm) -> Drawing:
    """绘制服务器最新资源使用率的阈值色条。"""
    drawing = Drawing(width, 4 * mm)
    drawing.add(Rect(0, 0, width, 4 * mm, fillColor=colors.HexColor("#E7EEF4"), strokeColor=None))
    if value is not None:
        color = colors.HexColor("#1976D2") if value < 85 else colors.HexColor("#EF6C00") if value < 95 else colors.HexColor("#C62828")
        drawing.add(Rect(0, 0, width * max(0, min(100, value)) / 100.0, 4 * mm, fillColor=color, strokeColor=None))
    return drawing


def _server_card(server: ServerSeries, styles: Dict[str, ParagraphStyle]) -> Table:
    """生成首页中单台服务器的最新状态概要卡片。"""
    latest = server.latest
    counts = latest.counters
    metrics = [
        ("CPU", latest.cpu_percent),
        ("内存", latest.memory_percent),
        ("磁盘", latest.disk_percent),
        ("IO iowait", latest.io_iowait_percent),
        ("IO %util", latest.io_util_percent),
    ]
    top = Table([[ _p(latest.remark, styles["card_title"]), _status_paragraph(latest.overall_status, styles), _p(f"{len(server.snapshots)} 次采样", styles["center"]) ]], colWidths=[102 * mm, 30 * mm, 35 * mm])
    top.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF2F8")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#A9C4D8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    cells = []
    for label, value in metrics:
        cells.append([_p(label, styles["center"]), _p("未采集" if value is None else f"{value:.1f}%", styles["center"]), _metric_bar(value)])
    coverage = _p(f"采集：{latest.collection_state}\n受限 {len(latest.limited_items)} 项 / 未纳入 {len(latest.excluded_items)} 项", styles["center"])
    body = Table([[cells[0], cells[1], cells[2], cells[3], cells[4], coverage]], colWidths=[27 * mm, 27 * mm, 27 * mm, 27 * mm, 27 * mm, 32 * mm])
    body.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C4D6E3")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    outer = Table([[top], [body]], colWidths=[167 * mm])
    outer.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return outer


def _labels(server: ServerSeries) -> List[str]:
    """生成单台服务器趋势图的巡检时间横坐标。"""
    dates = {item.captured_at.date() for item in server.snapshots if item.captured_at}
    format_text = "%H:%M" if len(dates) <= 1 else "%m-%d %H:%M"
    return [(item.captured_at.strftime(format_text) if item.captured_at else "未知") for item in server.snapshots]


def _overview_label(server: ServerSeries) -> str:
    """生成总览趋势图中简洁且稳定的服务器图例名称。"""
    remark = server.latest.remark.lower()
    if "飞马1" in remark:
        return "飞马1"
    if "飞马2" in remark:
        return "飞马2"
    if "client" in remark:
        return "Client"
    return server.latest.remark[:8]


def _overview_charts(servers: Sequence[ServerSeries]) -> List[object]:
    """生成以时间为横轴、服务器为图例的全局资源趋势图。"""
    sample_count = max((len(server.snapshots) for server in servers), default=0)
    reference = max(servers, key=lambda server: len(server.snapshots)) if servers else None
    reference_dates = {
        item.captured_at.date() for item in (reference.snapshots if reference else []) if item.captured_at
    }
    time_format = "%H:%M" if len(reference_dates) <= 1 else "%m-%d %H:%M"
    labels = [
        reference.snapshots[index].captured_at.strftime(time_format)
        if reference and index < len(reference.snapshots) and reference.snapshots[index].captured_at
        else f"采样{index + 1}"
        for index in range(sample_count)
    ]

    def metric_series(attribute: str) -> Dict[str, List[Optional[float]]]:
        """按服务器图例整理指定资源字段的同轴时间序列。"""
        return {
            _overview_label(server): [
                getattr(server.snapshots[index], attribute) if index < len(server.snapshots) else None
                for index in range(sample_count)
            ]
            for server in servers
        }

    cpu = metric_series("cpu_percent")
    memory = metric_series("memory_percent")
    disk = metric_series("disk_percent")
    iowait = metric_series("io_iowait_percent")
    util = metric_series("io_util_percent")
    return [
        trend_chart("最新 CPU 使用率", labels, cpu, width=172 * mm, height=42 * mm, percent=True),
        Spacer(1, 2 * mm),
        trend_chart("最新内存使用率", labels, memory, width=172 * mm, height=42 * mm, percent=True),
        Spacer(1, 2 * mm),
        trend_chart("最新磁盘使用率", labels, disk, width=172 * mm, height=42 * mm, percent=True),
        Spacer(1, 2 * mm),
        trend_chart("最新磁盘 IO iowait", labels, iowait, width=172 * mm, height=42 * mm, percent=True),
        Spacer(1, 2 * mm),
        trend_chart("最新磁盘 IO %util", labels, util, width=172 * mm, height=42 * mm, percent=True),
    ]


def _coverage_table(snapshot: Snapshot, styles: Dict[str, ParagraphStyle]) -> Table:
    """展示巡检采集完整度、权限受限和脚本未纳入项目。"""
    rows = [
        [_p("巡检模式", styles["header"]), _p("采集状态", styles["header"]), _p("采集受限", styles["header"]), _p("脚本未纳入", styles["header"])],
        [
            _p(snapshot.profile, styles["center"]),
            _p(snapshot.collection_state, styles["center"]),
            _p("、".join(snapshot.limited_items) or "无", styles["table"]),
            _p("、".join(snapshot.excluded_items) or "无", styles["table"]),
        ],
    ]
    table = Table(rows, colWidths=[34 * mm, 32 * mm, 48 * mm, 65 * mm])
    table.setStyle(_table_style(colors.HexColor("#546E7A")))
    return table


def _format_abnormal_time(snapshot: Snapshot) -> str:
    """格式化概要检查项发生异常的巡检时间点。"""
    if snapshot.captured_at:
        return snapshot.captured_at.strftime("%Y-%m-%d %H:%M")
    return snapshot.captured_text


def _summary_rows(server: ServerSeries, styles: Dict[str, ParagraphStyle]) -> LongTable:
    """生成服务器检查项、进程线程和目录信息的文字概要表。"""
    snapshot = server.latest
    rows = [[_p("状态", styles["header"]), _p("检查项", styles["header"]), _p("结果概要", styles["header"])]]
    for row in snapshot.summary_rows:
        abnormal_times = []
        for historical in server.snapshots:
            if any(item.item == row.item and item.status == "异常" for item in historical.summary_rows):
                time_text = _format_abnormal_time(historical)
                if time_text and time_text not in abnormal_times:
                    abnormal_times.append(time_text)
        detail = row.detail
        if abnormal_times:
            detail += "；异常时间点：" + "、".join(abnormal_times)
        rows.append([_status_paragraph(row.status, styles), _p(row.item, styles["table"]), _p(detail, styles["table"])])
    for process in snapshot.processes.values():
        thread_text = str(process.threads) if process.threads >= 0 else "未采集"
        rows.append([_p("进程", styles["center"]), _p(process.name, styles["table"]), _p(f"线程总数={thread_text}；PID={','.join(process.pids) or '未读取'}；{process.detail}", styles["table"])])
    for directory in snapshot.log_directories.values():
        rows.append([_p("日志目录", styles["center"]), _p(directory.path, styles["table"]), _p(directory.detail, styles["table"])])
    for detail in snapshot.business_directories:
        name, _, value = detail.partition("=")
        rows.append([_p("业务目录", styles["center"]), _p(name, styles["table"]), _p(value or detail, styles["table"])])
    for detail in snapshot.docker_details:
        name, _, value = detail.partition("：")
        rows.append([_p("Docker", styles["center"]), _p(name.replace("Docker容器 ", ""), styles["table"]), _p(value, styles["table"])])
    table = LongTable(rows, colWidths=[19 * mm, 42 * mm, 118 * mm], repeatRows=1)
    table.setStyle(_table_style())
    return table


def _server_charts(server: ServerSeries, styles: Dict[str, ParagraphStyle]) -> List[object]:
    """生成单台服务器资源、进程线程和日志目录的变化图。"""
    labels = _labels(server)
    cpu = {"CPU": [item.cpu_percent for item in server.snapshots]}
    memory = {"内存": [item.memory_percent for item in server.snapshots]}
    disk = {"磁盘": [item.disk_percent for item in server.snapshots]}
    iowait = {"iowait": [item.io_iowait_percent for item in server.snapshots]}
    util = {"%util": [item.io_util_percent for item in server.snapshots]}
    process_names = sorted({name for item in server.snapshots for name in item.processes})
    dir_names = sorted({name for item in server.snapshots for name in item.log_directories})
    directory_series = {name: [item.log_directories[name].size_bytes / (1024.0 ** 3) if name in item.log_directories else None for item in server.snapshots] for name in dir_names}
    charts: List[object] = [
        trend_chart("CPU 使用率变化", labels, cpu, width=172 * mm, height=43 * mm, percent=True),
        Spacer(1, 2 * mm),
        trend_chart("内存使用率变化", labels, memory, width=172 * mm, height=43 * mm, percent=True),
        Spacer(1, 2 * mm),
        trend_chart("磁盘使用率变化", labels, disk, width=172 * mm, height=43 * mm, percent=True),
        Spacer(1, 2 * mm),
        trend_chart("磁盘 IO iowait 变化", labels, iowait, width=172 * mm, height=43 * mm, percent=True),
        Spacer(1, 2 * mm),
        trend_chart("磁盘 IO %util 变化", labels, util, width=172 * mm, height=43 * mm, percent=True),
    ]
    if process_names:
        for name in process_names:
            series = {name: [
                item.processes[name].threads
                if name in item.processes and item.processes[name].threads >= 0 else None
                for item in server.snapshots
            ]}
            charts.extend([
                Spacer(1, 3 * mm),
                trend_chart(f"进程线程数变化 - {name}", labels, series, width=172 * mm, height=42 * mm),
            ])
    else:
        charts.extend([Spacer(1, 3 * mm), trend_chart("进程线程数变化", labels, {"无数据": [None] * len(labels)}, width=172 * mm, height=42 * mm)])
    charts.extend([
        Spacer(1, 3 * mm),
        trend_chart("日志目录大小变化（GB）", labels, directory_series or {"无数据": [None] * len(labels)}, width=172 * mm, height=46 * mm),
    ])
    return charts


def _assessment_summary(groups: Sequence[LogGroup], ai_status: str, styles: Dict[str, ParagraphStyle]) -> Table:
    """汇总去重异常数量、重要异常和需处理数量。"""
    counts = Counter(group.severity for group in groups)
    fix = sum(group.needs_fix is True for group in groups)
    no_fix = sum(group.needs_fix is False for group in groups)
    rows = [
        [_p("去重异常组", styles["header"]), _p("严重/高", styles["header"]), _p("需要处理", styles["header"]), _p("无需修改", styles["header"]), _p("评估状态", styles["header"])],
        [_p(str(len(groups)), styles["center"]), _p(str(counts["critical"] + counts["high"]), styles["center"]), _p(str(fix), styles["center"]), _p(str(no_fix), styles["center"]), _p(ai_status, styles["center"])],
    ]
    table = Table(rows, colWidths=[30 * mm, 28 * mm, 30 * mm, 28 * mm, 63 * mm])
    table.setStyle(_table_style(colors.HexColor("#7A263A")))
    return table


def _resource_assessment_table(servers: Sequence[ServerSeries], styles: Dict[str, ParagraphStyle]) -> LongTable:
    """生成资源指标的重要度、判断、建议、操作和来源表。"""
    rows = [[
        _p("服务器", styles["header"]), _p("指标", styles["header"]),
        _p("重要度", styles["header"]), _p("判断", styles["header"]),
        _p("建议", styles["header"]), _p("操作", styles["header"]), _p("来源", styles["header"]),
    ]]
    for server in servers:
        if not server.resource_assessments:
            rows.append([
                _p(server.latest.remark, styles["small"]), _p("资源指标", styles["table"]),
                _p("未评估", styles["center"]), _p("未取得资源指标AI评估结果。", styles["small"]),
                _p("-", styles["small"]), _p("-", styles["small"]), _p("未评估", styles["small"]),
            ])
            continue
        for item in server.resource_assessments:
            severity_style = ParagraphStyle(
                "resource_severity_" + item.severity,
                parent=styles["center"],
                textColor=SEVERITY_COLORS.get(item.severity, colors.black),
            )
            rows.append([
                _p(server.latest.remark, styles["small"]), _p(item.metric, styles["table"]),
                _p(SEVERITY_LABELS.get(item.severity, item.severity), severity_style),
                _p(f"{item.title}；{item.reason}", styles["small"]),
                _p(item.recommendation or "保持现状并持续观察该指标。", styles["small"]),
                _p(item.action_analysis or "复核下一采样周期；若持续升高或越过阈值则升级处理。", styles["small"]),
                _p(item.assessment_source, styles["small"]),
            ])
    table = LongTable(rows, colWidths=[30 * mm, 14 * mm, 14 * mm, 34 * mm, 30 * mm, 38 * mm, 19 * mm], repeatRows=1)
    table.setStyle(_table_style(colors.HexColor("#365C7D")))
    return table


def _server_log_summary(groups: Sequence[LogGroup], servers: Sequence[ServerSeries], styles: Dict[str, ParagraphStyle]) -> LongTable:
    """按服务器统计去重异常和需要处理的日志数量。"""
    rows = [[
        _p("服务器", styles["header"]), _p("去重异常组", styles["header"]),
        _p("严重/高", styles["header"]), _p("需要处理", styles["header"]), _p("无需修改", styles["header"]),
    ]]
    for server_name in [server.latest.remark for server in servers]:
        server_groups = [group for group in groups if group.server_key == server_name]
        counts = Counter(group.severity for group in server_groups)
        rows.append([
            _p(server_name, styles["table"]),
            _p(str(len(server_groups)), styles["center"]),
            _p(str(counts["critical"] + counts["high"]), styles["center"]),
            _p(str(sum(group.needs_fix is True for group in server_groups)), styles["center"]),
            _p(str(sum(group.needs_fix is False for group in server_groups)), styles["center"]),
        ])
    table = LongTable(rows, colWidths=[67 * mm, 28 * mm, 28 * mm, 30 * mm, 26 * mm], repeatRows=1)
    table.setStyle(_table_style(colors.HexColor("#5D3547")))
    return table


def _log_table(groups: Sequence[LogGroup], styles: Dict[str, ParagraphStyle]) -> LongTable:
    """生成按重要度排序的异常日志评估明细表。"""
    rows = [[
        _p("服务器", styles["header"]), _p("重要度", styles["header"]), _p("发现时间", styles["header"]),
        _p("判断", styles["header"]), _p("建议", styles["header"]), _p("操作", styles["header"]),
        _p("来源", styles["header"]), _p("去重日志样例", styles["header"]),
    ]]
    for group in groups:
        severity_style = ParagraphStyle("severity_" + group.severity, parent=styles["center"], textColor=SEVERITY_COLORS.get(group.severity, colors.black))
        first = group.first_seen.strftime("%m-%d %H:%M") if group.first_seen else "-"
        rows.append([
            _p(group.server_key, styles["small"]),
            _p(SEVERITY_LABELS.get(group.severity, group.severity), severity_style),
            _p(first, styles["small"]),
            _p(f"{group.title}；{group.reason}", styles["small"]),
            _p(group.recommendation or "保留记录并持续观察同类日志是否再次出现。", styles["small"]),
            _p(group.action_analysis or "核对相关服务状态和后续日志；若重复出现或影响业务则升级处理。", styles["small"]),
            _p(group.assessment_source, styles["small"]),
            _p(_short(group.sample, 500), styles["small"]),
        ])
    table = LongTable(rows, colWidths=[24 * mm, 13 * mm, 20 * mm, 27 * mm, 23 * mm, 30 * mm, 16 * mm, 26 * mm], repeatRows=1)
    table.setStyle(_table_style(colors.HexColor("#7A263A")))
    return table


def _draw_page(canvas, document) -> None:
    """在每页绘制报告页眉分隔线和页码。"""
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D5E0E8"))
    canvas.line(17 * mm, A4[1] - 10 * mm, A4[0] - 17 * mm, A4[1] - 10 * mm)
    canvas.setFont("STSong-Light", 7)
    canvas.setFillColor(colors.HexColor("#78909C"))
    canvas.drawString(17 * mm, A4[1] - 8 * mm, "服务器巡检汇总报告")
    canvas.drawRightString(A4[0] - 17 * mm, 10 * mm, f"第 {document.page} 页")
    canvas.restoreState()


def create_pdf(servers: Sequence[ServerSeries], output: Path, ai_status: str, max_log_groups: int = 300) -> None:
    """将服务器时间序列和异常评估渲染为巡检PDF。

    Args:
        servers: 已解析并完成日志与资源评估的服务器集合。
        output: PDF输出路径。
        ai_status: 展示在报告中的AI执行状态。
        max_log_groups: 最多展示的去重日志组，0表示全部。

    Returns:
        None: PDF直接写入指定输出路径。
    """
    styles = _styles()
    output.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(str(output), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=16 * mm, title="服务器巡检汇总报告", author="服务器巡检工具")
    story: List[object] = [_p("服务器巡检图表总览", styles["title"]), Spacer(1, 5 * mm)]
    # 首页先给出每台服务器的可视化概要，避免无关文字占据首屏。
    for server in servers:
        story.extend([_server_card(server, styles), Spacer(1, 4 * mm)])
    story.extend([Spacer(1, 2 * mm)])
    story.extend(_overview_charts(servers))

    for server in servers:
        story.extend([
            PageBreak(),
            _p(server.latest.remark + " - 指标变化", styles["h1"]),
            _p(f"主机名：{server.latest.hostname}　采样次数：{len(server.snapshots)}　巡检模式：{server.latest.profile}　时间范围：{server.snapshots[0].captured_text} 至 {server.latest.captured_text}", styles["small"]),
            Spacer(1, 3 * mm),
        ])
        story.extend(_server_charts(server, styles))
        story.extend([
            PageBreak(),
            _p(server.latest.remark + " - 汇总概要", styles["h1"]),
            _p(f"主机名：{server.latest.hostname}　采样次数：{len(server.snapshots)}　巡检模式：{server.latest.profile}　时间范围：{server.snapshots[0].captured_text} 至 {server.latest.captured_text}", styles["small"]),
            Spacer(1, 3 * mm),
            _coverage_table(server.latest, styles),
            Spacer(1, 3 * mm),
            _summary_rows(server, styles),
        ])

    all_groups = sort_groups([group for server in servers for group in server.log_groups])
    story.extend([
        PageBreak(), _p("异常评估汇总", styles["h1"]),
        _p("资源指标 AI 评估", styles["h2"]),
        _resource_assessment_table(servers, styles), Spacer(1, 4 * mm),
        _assessment_summary(all_groups, ai_status, styles), Spacer(1, 4 * mm),
        _p("各服务器日志汇总", styles["h2"]), _server_log_summary(all_groups, servers, styles),
    ])
    displayed = all_groups if max_log_groups == 0 else all_groups[:max_log_groups]
    if max_log_groups and len(all_groups) > max_log_groups:
        story.extend([Spacer(1, 2 * mm), _p(f"共 {len(all_groups)} 个去重异常组，日志明细按重要程度展示前 {max_log_groups} 个。", styles["small"])])
    # 应用日志优先于Docker和系统日志，便于业务故障首先被定位。
    for category in CATEGORY_ORDER:
        category_groups = [group for group in displayed if group.category == category]
        story.extend([CondPageBreak(50 * mm), _p(f"{category}（{len(category_groups)} 个去重异常组）", styles["h2"])])
        if category_groups:
            story.append(_log_table(category_groups, styles))
        else:
            story.append(_p("未识别到此类异常日志。", styles["body"]))
    document.build(story, onFirstPage=_draw_page, onLaterPages=_draw_page)

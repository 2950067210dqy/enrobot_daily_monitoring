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
CATEGORY_ORDER = ("系统/进程日志", "Docker日志", "应用日志目录日志")


def _clean(value: object) -> str:
    import re
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or "")).strip()


def _short(value: object, limit: int = 220) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _styles() -> Dict[str, ParagraphStyle]:
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
    text = _clean(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
    return Paragraph(text or "-", style)


def _table_style(header: colors.Color = colors.HexColor("#235A85")) -> TableStyle:
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
    style = ParagraphStyle("status_" + status, parent=styles["center"], textColor=STATUS_COLORS.get(status, colors.black))
    return _p(status, style)


def _metric_bar(value: Optional[float], width: float = 27 * mm) -> Drawing:
    drawing = Drawing(width, 4 * mm)
    drawing.add(Rect(0, 0, width, 4 * mm, fillColor=colors.HexColor("#E7EEF4"), strokeColor=None))
    if value is not None:
        color = colors.HexColor("#1976D2") if value < 85 else colors.HexColor("#EF6C00") if value < 95 else colors.HexColor("#C62828")
        drawing.add(Rect(0, 0, width * max(0, min(100, value)) / 100.0, 4 * mm, fillColor=color, strokeColor=None))
    return drawing


def _server_card(server: ServerSeries, styles: Dict[str, ParagraphStyle]) -> Table:
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
    return [(item.captured_at.strftime("%m-%d %H:%M") if item.captured_at else "未知") for item in server.snapshots]


def _overview_charts(servers: Sequence[ServerSeries]) -> Table:
    labels = [server.latest.remark[:10] for server in servers]
    resource_series = {
        "CPU": [server.latest.cpu_percent for server in servers],
        "内存": [server.latest.memory_percent for server in servers],
        "磁盘": [server.latest.disk_percent for server in servers],
    }
    io_series = {
        "iowait": [server.latest.io_iowait_percent for server in servers],
        "%util": [server.latest.io_util_percent for server in servers],
    }
    return Table([[trend_chart("最新资源使用率", labels, resource_series, width=84 * mm, percent=True), trend_chart("最新磁盘 IO", labels, io_series, width=84 * mm, percent=True)]], colWidths=[86 * mm, 86 * mm])


def _coverage_table(snapshot: Snapshot, styles: Dict[str, ParagraphStyle]) -> Table:
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


def _summary_rows(snapshot: Snapshot, styles: Dict[str, ParagraphStyle]) -> LongTable:
    rows = [[_p("状态", styles["header"]), _p("检查项", styles["header"]), _p("结果概要", styles["header"])]]
    for row in snapshot.summary_rows:
        rows.append([_status_paragraph(row.status, styles), _p(row.item, styles["table"]), _p(row.detail, styles["table"])])
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
    labels = _labels(server)
    resources = {
        "CPU": [item.cpu_percent for item in server.snapshots],
        "内存": [item.memory_percent for item in server.snapshots],
        "磁盘": [item.disk_percent for item in server.snapshots],
    }
    io_series = {
        "iowait": [item.io_iowait_percent for item in server.snapshots],
        "%util": [item.io_util_percent for item in server.snapshots],
    }
    process_names = sorted({name for item in server.snapshots for name in item.processes})
    process_series = {
        name: [item.processes[name].threads if name in item.processes and item.processes[name].threads >= 0 else None for item in server.snapshots]
        for name in process_names
    }
    dir_names = sorted({name for item in server.snapshots for name in item.log_directories})
    directory_series = {name: [item.log_directories[name].size_bytes / (1024.0 ** 3) if name in item.log_directories else None for item in server.snapshots] for name in dir_names}
    return [
        Table([[trend_chart("CPU / 内存 / 磁盘变化", labels, resources, width=84 * mm, percent=True), trend_chart("磁盘 IO 变化", labels, io_series, width=84 * mm, percent=True)]], colWidths=[86 * mm, 86 * mm]),
        Spacer(1, 3 * mm),
        trend_chart("进程线程数变化", labels, process_series or {"无数据": [None] * len(labels)}, width=172 * mm, height=52 * mm),
        Spacer(1, 3 * mm),
        trend_chart("日志目录大小变化（GB）", labels, directory_series or {"无数据": [None] * len(labels)}, width=172 * mm, height=52 * mm),
    ]


def _assessment_summary(groups: Sequence[LogGroup], ai_status: str, styles: Dict[str, ParagraphStyle]) -> Table:
    counts = Counter(group.severity for group in groups)
    immediate = sum(group.immediate is True for group in groups)
    fix = sum(group.needs_fix is True and group.immediate is not True for group in groups)
    no_fix = sum(group.needs_fix is False for group in groups)
    rows = [
        [_p("去重异常组", styles["header"]), _p("严重/高", styles["header"]), _p("需立即处理", styles["header"]), _p("需处理但非立即", styles["header"]), _p("无需修改", styles["header"]), _p("评估状态", styles["header"])],
        [_p(str(len(groups)), styles["center"]), _p(str(counts["critical"] + counts["high"]), styles["center"]), _p(str(immediate), styles["center"]), _p(str(fix), styles["center"]), _p(str(no_fix), styles["center"]), _p(ai_status, styles["center"])],
    ]
    table = Table(rows, colWidths=[25 * mm, 23 * mm, 27 * mm, 34 * mm, 24 * mm, 46 * mm])
    table.setStyle(_table_style(colors.HexColor("#7A263A")))
    return table


def _server_log_summary(groups: Sequence[LogGroup], servers: Sequence[ServerSeries], styles: Dict[str, ParagraphStyle]) -> LongTable:
    rows = [[
        _p("服务器", styles["header"]), _p("去重异常组", styles["header"]),
        _p("严重/高", styles["header"]), _p("需立即处理", styles["header"]),
        _p("需处理但非立即", styles["header"]), _p("无需修改", styles["header"]),
    ]]
    for server_name in [server.latest.remark for server in servers]:
        server_groups = [group for group in groups if group.server_key == server_name]
        counts = Counter(group.severity for group in server_groups)
        rows.append([
            _p(server_name, styles["table"]),
            _p(str(len(server_groups)), styles["center"]),
            _p(str(counts["critical"] + counts["high"]), styles["center"]),
            _p(str(sum(group.immediate is True for group in server_groups)), styles["center"]),
            _p(str(sum(group.needs_fix is True and group.immediate is not True for group in server_groups)), styles["center"]),
            _p(str(sum(group.needs_fix is False for group in server_groups)), styles["center"]),
        ])
    table = LongTable(rows, colWidths=[55 * mm, 24 * mm, 23 * mm, 25 * mm, 31 * mm, 21 * mm], repeatRows=1)
    table.setStyle(_table_style(colors.HexColor("#5D3547")))
    return table


def _log_table(groups: Sequence[LogGroup], styles: Dict[str, ParagraphStyle]) -> LongTable:
    rows = [[
        _p("服务器", styles["header"]), _p("重要度", styles["header"]), _p("立即", styles["header"]), _p("次数", styles["header"]),
        _p("时间范围", styles["header"]), _p("AI/规则判断", styles["header"]), _p("去重日志样例", styles["header"]),
    ]]
    for group in groups:
        severity_style = ParagraphStyle("severity_" + group.severity, parent=styles["center"], textColor=SEVERITY_COLORS.get(group.severity, colors.black))
        first = group.first_seen.strftime("%m-%d %H:%M") if group.first_seen else "-"
        last = group.last_seen.strftime("%m-%d %H:%M") if group.last_seen else "-"
        assessment = f"{group.title}；{group.reason}；来源={group.assessment_source}"
        rows.append([
            _p(group.server_key, styles["small"]),
            _p(SEVERITY_LABELS.get(group.severity, group.severity), severity_style),
            _p("是" if group.immediate else "否", styles["center"]),
            _p(str(group.occurrences), styles["center"]),
            _p(first + " ~ " + last, styles["small"]),
            _p(assessment, styles["small"]),
            _p(_short(group.sample, 700), styles["small"]),
        ])
    table = LongTable(rows, colWidths=[29 * mm, 15 * mm, 12 * mm, 12 * mm, 27 * mm, 40 * mm, 44 * mm], repeatRows=1)
    table.setStyle(_table_style(colors.HexColor("#7A263A")))
    return table


def _draw_page(canvas, document) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D5E0E8"))
    canvas.line(17 * mm, A4[1] - 10 * mm, A4[0] - 17 * mm, A4[1] - 10 * mm)
    canvas.setFont("STSong-Light", 7)
    canvas.setFillColor(colors.HexColor("#78909C"))
    canvas.drawString(17 * mm, A4[1] - 8 * mm, "服务器巡检汇总报告")
    canvas.drawRightString(A4[0] - 17 * mm, 10 * mm, f"第 {document.page} 页")
    canvas.restoreState()


def create_pdf(servers: Sequence[ServerSeries], output: Path, ai_status: str, max_log_groups: int = 300) -> None:
    styles = _styles()
    output.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(str(output), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=16 * mm, title="服务器巡检汇总报告", author="服务器巡检工具")
    story: List[object] = [_p("服务器巡检图表总览", styles["title"]), Spacer(1, 5 * mm)]
    for server in servers:
        story.extend([_server_card(server, styles), Spacer(1, 4 * mm)])
    story.extend([Spacer(1, 2 * mm), _overview_charts(servers)])

    for server in servers:
        story.extend([
            PageBreak(),
            _p(server.latest.remark + " - 汇总概要", styles["h1"]),
            _p(f"主机名：{server.latest.hostname}　采样次数：{len(server.snapshots)}　巡检模式：{server.latest.profile}　时间范围：{server.snapshots[0].captured_text} 至 {server.latest.captured_text}", styles["small"]),
            Spacer(1, 3 * mm),
            _coverage_table(server.latest, styles),
            Spacer(1, 3 * mm),
            _summary_rows(server.latest, styles),
            Spacer(1, 5 * mm),
            _p("指标变化图表", styles["h2"]),
        ])
        story.extend(_server_charts(server, styles))

    all_groups = sort_groups([group for server in servers for group in server.log_groups])
    story.extend([
        PageBreak(), _p("异常评估汇总", styles["h1"]),
        _assessment_summary(all_groups, ai_status, styles), Spacer(1, 4 * mm),
        _p("各服务器日志汇总", styles["h2"]), _server_log_summary(all_groups, servers, styles),
    ])
    displayed = all_groups if max_log_groups == 0 else all_groups[:max_log_groups]
    if max_log_groups and len(all_groups) > max_log_groups:
        story.extend([Spacer(1, 2 * mm), _p(f"共 {len(all_groups)} 个去重异常组，日志明细按重要程度展示前 {max_log_groups} 个。", styles["small"])])
    for category in CATEGORY_ORDER:
        category_groups = [group for group in displayed if group.category == category]
        story.extend([CondPageBreak(50 * mm), _p(f"{category}（{len(category_groups)} 个去重异常组）", styles["h2"])])
        if category_groups:
            story.append(_log_table(category_groups, styles))
        else:
            story.append(_p("未识别到此类异常日志。", styles["body"]))
    document.build(story, onFirstPage=_draw_page, onLaterPages=_draw_page)

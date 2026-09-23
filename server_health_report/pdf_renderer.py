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
    Image as PlatypusImage,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .charts import ChartImageWriter, trend_chart
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
# 一小时一次的CPU、内存和IO瞬时值不纳入PDF展示与状态判断；磁盘及inode仍保留。
PDF_HIDDEN_RESOURCE_ITEMS = {"CPU", "CPU与负载", "内存", "Swap", "磁盘IO性能"}


def _is_hidden_pdf_resource(item: str) -> bool:
    """判断巡检项是否属于PDF不再展示的瞬时资源指标。"""
    return any(item == name or item.startswith(name + "（") for name in PDF_HIDDEN_RESOURCE_ITEMS)


def _pdf_status(snapshot: Snapshot) -> str:
    """仅根据PDF保留的检查项计算服务器状态，避免瞬时CPU、内存或IO影响总览。"""
    priority = {"异常": 0, "警告": 1, "不可判定": 2, "正常": 3, "信息": 4}
    statuses = [
        row.status for row in snapshot.summary_rows
        if not _is_hidden_pdf_resource(row.item) and row.status in priority
    ]
    if not statuses:
        return "未识别"
    worst = min(statuses, key=lambda value: priority[value])
    return "部分不可判定" if worst == "不可判定" else worst


def _pdf_coverage(snapshot: Snapshot) -> tuple[str, List[str], List[str]]:
    """过滤瞬时资源采集限制，返回PDF关注范围内的覆盖状态。"""
    limited = [item for item in snapshot.limited_items if not _is_hidden_pdf_resource(item)]
    excluded = [item for item in snapshot.excluded_items if not _is_hidden_pdf_resource(item)]
    state = "部分受限" if limited else "按巡检范围完成" if excluded else "完整"
    return state, limited, excluded


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


def _chart_image(writer: ChartImageWriter, drawing: Drawing, label: str) -> PlatypusImage:
    """保存一张图表PNG，并创建保持原显示尺寸的PDF图片元素。

    Args:
        writer: 本次PDF专用的图表图片输出器。
        drawing: 已完成布局的ReportLab图表。
        label: PNG文件名中使用的业务图表名称。

    Returns:
        PlatypusImage: PDF排版时引用对应PNG文件的图片元素。
    """
    image_path = writer.write(drawing, label)
    image = PlatypusImage(str(image_path), width=drawing.width, height=drawing.height)
    image.hAlign = "CENTER"
    return image


def _disk_mount_sort_key(mount_point: str) -> tuple:
    """让根目录优先，其余挂载点按路径层级和名称稳定排序。"""
    return (0 if mount_point == "/" else 1, mount_point.count("/"), mount_point)


def _card_metric_cell(
    server_name: str, label: str, value: Optional[float], styles: Dict[str, ParagraphStyle],
    writer: ChartImageWriter,
) -> List[object]:
    """生成首页概要卡中的单值资源指标单元格。"""
    bar = _metric_bar(value, width=20 * mm)
    bar_image = _chart_image(writer, bar, f"{server_name}_最新{label}使用率")
    return [
        _p(label, styles["center"]),
        _p("未采集" if value is None else f"{value:.1f}%", styles["center"]),
        bar_image,
    ]


def _disk_card_cell(snapshot: Snapshot, styles: Dict[str, ParagraphStyle], writer: ChartImageWriter) -> List[object]:
    """生成列出全部真实挂载点及各自使用率的首页磁盘单元格。"""
    mounts = sorted(snapshot.disk_mounts.values(), key=lambda item: _disk_mount_sort_key(item.mount_point))
    content: List[object] = [_p("磁盘挂载点", styles["center"])]
    if mounts:
        for item in mounts:
            content.append(_p(f"{item.mount_point}：{item.percent:.1f}%", styles["center"]))
            # 每个挂载点单独输出进度条PNG，颜色也按该挂载点自身阈值判断。
            bar = _metric_bar(item.percent, width=108 * mm)
            content.append(_chart_image(
                writer,
                bar,
                f"{snapshot.remark}_最新磁盘挂载点_{item.mount_point}_使用率",
            ))
    else:
        value_text = "未采集" if snapshot.disk_percent is None else f"挂载点未识别：{snapshot.disk_percent:.1f}%"
        content.append(_p(value_text, styles["center"]))
        bar = _metric_bar(snapshot.disk_percent, width=108 * mm)
        content.append(_chart_image(writer, bar, f"{snapshot.remark}_最新磁盘挂载点未识别_使用率"))
    return content


def _server_card(server: ServerSeries, styles: Dict[str, ParagraphStyle], writer: ChartImageWriter) -> Table:
    """生成首页中单台服务器的最新状态概要卡片。"""
    latest = server.latest
    coverage_state, limited_items, excluded_items = _pdf_coverage(latest)
    top = Table([[ _p(latest.remark, styles["card_title"]), _status_paragraph(_pdf_status(latest), styles), _p(f"{len(server.snapshots)} 次采样", styles["center"]) ]], colWidths=[102 * mm, 30 * mm, 35 * mm])
    top.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF2F8")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#A9C4D8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    disk_cell = _disk_card_cell(latest, styles, writer)
    coverage = _p(f"采集：{coverage_state}\n受限 {len(limited_items)} 项 / 未纳入 {len(excluded_items)} 项", styles["center"])
    body = Table(
        [[disk_cell, coverage]],
        colWidths=[127 * mm, 40 * mm],
    )
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


def _overview_charts(servers: Sequence[ServerSeries], writer: ChartImageWriter) -> List[object]:
    """生成以时间为横轴、服务器为图例的全局磁盘趋势图。"""
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

    charts: List[object] = []
    mount_points = sorted(
        {mount for server in servers for snapshot in server.snapshots for mount in snapshot.disk_mounts},
        key=_disk_mount_sort_key,
    )
    if mount_points:
        for mount_point in mount_points:
            mount_series = {
                _overview_label(server): [
                    snapshot.disk_mounts[mount_point].percent if mount_point in snapshot.disk_mounts else None
                    for snapshot in server.snapshots
                ]
                for server in servers
                if any(mount_point in snapshot.disk_mounts for snapshot in server.snapshots)
            }
            charts.extend([
                Spacer(1, 2 * mm),
                _chart_image(
                    writer,
                    trend_chart(f"最新磁盘使用率 - 挂载点 {mount_point}", labels, mount_series, width=172 * mm, height=42 * mm, percent=True),
                    f"总览_最新磁盘使用率_挂载点_{mount_point}",
                ),
            ])
    else:
        charts.extend([
            Spacer(1, 2 * mm),
            _chart_image(
                writer,
                trend_chart("最新磁盘使用率 - 挂载点未识别", labels, metric_series("disk_percent"), width=172 * mm, height=42 * mm, percent=True),
                "总览_最新磁盘使用率_挂载点未识别",
            ),
        ])
    return charts


def _coverage_table(snapshot: Snapshot, styles: Dict[str, ParagraphStyle]) -> Table:
    """展示巡检采集完整度、权限受限和脚本未纳入项目。"""
    coverage_state, limited_items, excluded_items = _pdf_coverage(snapshot)
    rows = [
        [_p("巡检模式", styles["header"]), _p("采集状态", styles["header"]), _p("采集受限", styles["header"]), _p("脚本未纳入", styles["header"])],
        [
            _p(snapshot.profile, styles["center"]),
            _p(coverage_state, styles["center"]),
            _p("、".join(limited_items) or "无", styles["table"]),
            _p("、".join(excluded_items) or "无", styles["table"]),
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


def _disk_capacity_summary(snapshot: Snapshot) -> str:
    """把全部真实挂载点的容量和使用率整理为服务器详情文字。"""
    details = []
    for item in sorted(snapshot.disk_mounts.values(), key=lambda mount: _disk_mount_sort_key(mount.mount_point)):
        capacity = ""
        if item.size != "未采集":
            capacity = f"，{item.fs_type}，总量={item.size}，已用={item.used}，可用={item.available}"
        details.append(f"{item.mount_point}={item.percent:.1f}%（文件系统={item.filesystem}{capacity}）")
    return "全部挂载点：" + "；".join(details)


def _inode_mount_summary(snapshot: Snapshot) -> str:
    """把全部真实挂载点的inode使用率整理为服务器详情文字。"""
    details = [
        f"{item.mount_point}=" + (
            f"{item.inode_percent:.1f}%" if item.inode_percent is not None else "不可用/未提供"
        )
        for item in sorted(snapshot.disk_mounts.values(), key=lambda mount: _disk_mount_sort_key(mount.mount_point))
    ]
    return "各挂载点inode使用率：" + "；".join(details) if details else "各挂载点inode使用率未采集"


def _summary_rows(server: ServerSeries, styles: Dict[str, ParagraphStyle]) -> LongTable:
    """生成服务器检查项、进程线程和目录信息的文字概要表。"""
    snapshot = server.latest
    rows = [[_p("状态", styles["header"]), _p("检查项", styles["header"]), _p("结果概要", styles["header"])]]
    for row in snapshot.summary_rows:
        if _is_hidden_pdf_resource(row.item):
            continue
        abnormal_times = []
        for historical in server.snapshots:
            if any(item.item == row.item and item.status == "异常" for item in historical.summary_rows):
                time_text = _format_abnormal_time(historical)
                if time_text and time_text not in abnormal_times:
                    abnormal_times.append(time_text)
        if row.item == "磁盘容量" and snapshot.disk_mounts:
            detail = _disk_capacity_summary(snapshot)
        elif row.item == "inode" and snapshot.disk_mounts:
            detail = _inode_mount_summary(snapshot)
        else:
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


def _server_charts(server: ServerSeries, styles: Dict[str, ParagraphStyle], writer: ChartImageWriter) -> List[object]:
    """生成单台服务器的进程线程和日志目录变化图。

    磁盘已经在首页按全部服务器统一展示，此处不再重复绘制。
    """
    labels = _labels(server)
    process_names = sorted({name for item in server.snapshots for name in item.processes})
    dir_names = sorted({name for item in server.snapshots for name in item.log_directories})
    directory_series = {name: [item.log_directories[name].size_bytes / (1024.0 ** 3) if name in item.log_directories else None for item in server.snapshots] for name in dir_names}
    charts: List[object] = []
    if process_names:
        for name in process_names:
            series = {name: [
                item.processes[name].threads
                if name in item.processes and item.processes[name].threads >= 0 else None
                for item in server.snapshots
            ]}
            charts.extend([
                Spacer(1, 3 * mm),
                _chart_image(
                    writer,
                    trend_chart(f"进程线程数变化 - {name}", labels, series, width=172 * mm, height=42 * mm),
                    f"{server.latest.remark}_进程线程数变化_{name}",
                ),
            ])
    else:
        charts.extend([
            Spacer(1, 3 * mm),
            _chart_image(
                writer,
                trend_chart("进程线程数变化", labels, {"无数据": [None] * len(labels)}, width=172 * mm, height=42 * mm),
                f"{server.latest.remark}_进程线程数变化_无数据",
            ),
        ])
    charts.extend([
        Spacer(1, 3 * mm),
        _chart_image(
            writer,
            trend_chart("日志目录大小变化（GB）", labels, directory_series or {"无数据": [None] * len(labels)}, width=172 * mm, height=46 * mm),
            f"{server.latest.remark}_日志目录大小变化_GB",
        ),
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
    """生成磁盘指标的重要度、判断、建议、操作和来源表。"""
    rows = [[
        _p("服务器", styles["header"]), _p("指标", styles["header"]),
        _p("重要度", styles["header"]), _p("判断", styles["header"]),
        _p("建议", styles["header"]), _p("操作", styles["header"]), _p("来源", styles["header"]),
    ]]
    for server in servers:
        disk_assessments = [item for item in server.resource_assessments if item.metric == "磁盘"]
        if not disk_assessments:
            rows.append([
                _p(server.latest.remark, styles["small"]), _p("磁盘", styles["table"]),
                _p("未评估", styles["center"]), _p("未取得磁盘指标AI评估结果。", styles["small"]),
                _p("-", styles["small"]), _p("-", styles["small"]), _p("未评估", styles["small"]),
            ])
            continue
        for item in disk_assessments:
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


def _log_sample_page_blocks(value: object, target_chars: int = 900) -> List[str]:
    """只在原日志换行处生成无损分页块，避免截断堆栈行。

    ReportLab 3.6的跨页表格单元格会把续页片段错误撑到整页高度，因此仍需使用多个
    物理行帮助分页；这些行在样式层不绘制内部边线和间距，PDF中表现为一个连续单元格。

    Args:
        value: 完整去重日志样例。
        target_chars: 每个内部分页块的目标字符数。

    Returns:
        List[str]: 拼接后与清理后原文完全一致、且不切断原始文字行的分页块。
    """
    text = _clean(value)
    if not text:
        return [""]
    lines = text.splitlines(keepends=True)
    blocks: List[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) > max(300, target_chars):
            blocks.append(current)
            current = ""
        current += line
    if current or not blocks:
        blocks.append(current)
    return blocks


def _log_table(groups: Sequence[LogGroup], styles: Dict[str, ParagraphStyle]) -> LongTable:
    """以概要行和视觉连续的宽幅样例单元格生成异常日志明细表。"""
    rows = [[
        _p("编号", styles["header"]), _p("服务器", styles["header"]),
        _p("重要度", styles["header"]), _p("发现时间", styles["header"]),
        _p("判断", styles["header"]), _p("建议", styles["header"]), _p("操作", styles["header"]),
        _p("来源", styles["header"]), _p("重复数量", styles["header"]),
    ]]
    paired_rows = []
    for number, group in enumerate(groups, start=1):
        severity_style = ParagraphStyle("severity_" + group.severity, parent=styles["center"], textColor=SEVERITY_COLORS.get(group.severity, colors.black))
        first = group.first_seen.strftime("%m-%d %H:%M") if group.first_seen else "-"
        summary_row_index = len(rows)
        rows.append([
            _p(str(number), styles["center"]),
            _p(group.server_key, styles["small"]),
            _p(SEVERITY_LABELS.get(group.severity, group.severity), severity_style),
            _p(first, styles["small"]),
            _p(f"{group.title}；{group.reason}", styles["small"]),
            _p(group.recommendation or "保留记录并持续观察同类日志是否再次出现。", styles["small"]),
            _p(group.action_analysis or "核对相关服务状态和后续日志；若重复出现或影响业务则升级处理。", styles["small"]),
            _p(group.assessment_source, styles["small"]),
            _p(str(max(0, group.occurrences - 1)), styles["center"]),
        ])
        sample_row_indices = []
        for block_index, sample_block in enumerate(_log_sample_page_blocks(group.sample)):
            sample_row_index = len(rows)
            rows.append([
                _p(str(number), styles["center"]) if block_index == 0 else "",
                _p(("去重日志样例：" if block_index == 0 else "") + sample_block, styles["small"]),
                "", "", "", "", "", "", "",
            ])
            sample_row_indices.append(sample_row_index)
        paired_rows.append((summary_row_index, sample_row_indices))

    # 分页块不启用行内拆分；每块已经严格落在原日志换行处，不会截断一条堆栈行。
    table = LongTable(
        rows,
        colWidths=[9 * mm, 22 * mm, 11 * mm, 19 * mm, 28 * mm, 24 * mm, 36 * mm, 18 * mm, 12 * mm],
        # 日志表跨到新页面时重复第一行表头，方便直接识别各列含义。
        repeatRows=1,
        splitInRow=0,
    )
    line_color = colors.HexColor("#B7C9D6")
    sample_background = colors.HexColor("#F7FAFC")
    table_style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7A263A")),
        ("GRID", (0, 0), (-1, 0), 0.3, line_color),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ])
    for group_index, (summary_row_index, sample_row_indices) in enumerate(paired_rows):
        summary_background = colors.white if group_index % 2 == 0 else colors.HexColor("#F5F8FA")
        table_style.add("BACKGROUND", (0, summary_row_index), (-1, summary_row_index), summary_background)
        table_style.add("GRID", (0, summary_row_index), (-1, summary_row_index), 0.3, line_color)
        for block_index, sample_row_index in enumerate(sample_row_indices):
            table_style.add("SPAN", (1, sample_row_index), (-1, sample_row_index))
            table_style.add("BACKGROUND", (0, sample_row_index), (-1, sample_row_index), sample_background)
            # 只保留整个样例区域的左右边界和编号分隔线，块与块之间完全不画横线。
            table_style.add("LINEBEFORE", (0, sample_row_index), (0, sample_row_index), 0.3, line_color)
            table_style.add("LINEBEFORE", (1, sample_row_index), (1, sample_row_index), 0.3, line_color)
            table_style.add("LINEAFTER", (-1, sample_row_index), (-1, sample_row_index), 0.3, line_color)
            table_style.add("TOPPADDING", (0, sample_row_index), (-1, sample_row_index), 3.5 if block_index == 0 else 0)
            table_style.add("BOTTOMPADDING", (0, sample_row_index), (-1, sample_row_index), 3.5 if block_index == len(sample_row_indices) - 1 else 0)
            if block_index == 0:
                table_style.add("LINEABOVE", (0, sample_row_index), (-1, sample_row_index), 0.3, line_color)
            if block_index == len(sample_row_indices) - 1:
                table_style.add("LINEBELOW", (0, sample_row_index), (-1, sample_row_index), 0.3, line_color)
    table.setStyle(table_style)
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


def chart_directory_for_pdf(output: Path) -> Path:
    """根据PDF文件名生成同目录下的配套图表图片目录。

    Args:
        output: 最终PDF输出路径。

    Returns:
        Path: 形如“服务器巡检报告_YYYY_MM_DD_图表”的目录路径。
    """
    return output.parent / f"{output.stem}_图表"


def create_pdf(servers: Sequence[ServerSeries], output: Path, ai_status: str, max_log_groups: int = 300) -> List[Path]:
    """先输出全部图表PNG，再将这些图片和评估表格排版为巡检PDF。

    Args:
        servers: 已解析并完成日志与资源评估的服务器集合。
        output: PDF输出路径。
        ai_status: 展示在报告中的AI执行状态。
        max_log_groups: 最多展示的去重日志组，0表示全部。

    Returns:
        List[Path]: 本次生成并嵌入PDF的全部图表PNG路径。
    """
    styles = _styles()
    output.parent.mkdir(parents=True, exist_ok=True)
    chart_writer = ChartImageWriter(chart_directory_for_pdf(output))
    document = SimpleDocTemplate(str(output), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=16 * mm, title="服务器巡检汇总报告", author="服务器巡检工具")
    story: List[object] = [_p("服务器磁盘巡检总览", styles["title"]), Spacer(1, 5 * mm)]
    # 首页先给出每台服务器的可视化概要，避免无关文字占据首屏。
    for server in servers:
        story.extend([_server_card(server, styles, chart_writer), Spacer(1, 4 * mm)])
    story.extend([Spacer(1, 2 * mm)])
    story.extend(_overview_charts(servers, chart_writer))

    for server in servers:
        story.extend([
            Spacer(1, 4 * mm),
            _p(server.latest.remark + " - 进程与日志目录变化", styles["h1"]),
            _p(f"主机名：{server.latest.hostname}　采样次数：{len(server.snapshots)}　巡检模式：{server.latest.profile}　时间范围：{server.snapshots[0].captured_text} 至 {server.latest.captured_text}", styles["small"]),
            Spacer(1, 3 * mm),
        ])
        story.extend(_server_charts(server, styles, chart_writer))
        story.extend([
            Spacer(1, 4 * mm),
            _p(server.latest.remark + " - 汇总概要", styles["h1"]),
            _p(f"主机名：{server.latest.hostname}　采样次数：{len(server.snapshots)}　巡检模式：{server.latest.profile}　时间范围：{server.snapshots[0].captured_text} 至 {server.latest.captured_text}", styles["small"]),
            Spacer(1, 3 * mm),
            _coverage_table(server.latest, styles),
            Spacer(1, 3 * mm),
            _summary_rows(server, styles),
        ])

    all_groups = sort_groups([group for server in servers for group in server.log_groups])
    story.extend([
        Spacer(1, 4 * mm), _p("异常评估汇总", styles["h1"]),
        _p("磁盘指标 AI 评估", styles["h2"]),
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
        story.extend([Spacer(1, 3 * mm), _p(f"{category}（{len(category_groups)} 个去重异常组）", styles["h2"])])
        if category_groups:
            story.append(_log_table(category_groups, styles))
        else:
            story.append(_p("未识别到此类异常日志。", styles["body"]))
    try:
        document.build(story, onFirstPage=_draw_page, onLaterPages=_draw_page)
    except Exception:
        # PDF被阅读器占用时，CLI会改用递增文件名重试；先移除失败尝试产生的孤立图片。
        for image_path in chart_writer.paths:
            if image_path.is_file():
                image_path.unlink()
        try:
            chart_writer.directory.rmdir()
        except OSError:
            # 目录中存在非本工具文件时保留目录，绝不扩大清理范围。
            pass
        raise
    return list(chart_writer.paths)

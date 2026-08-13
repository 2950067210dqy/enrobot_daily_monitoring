from __future__ import annotations

import hashlib
import re
from typing import Dict, Iterable, List

from .models import LogGroup, RawLog, ServerSeries


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unassessed": 5}


def fingerprint(text: str) -> str:
    """生成异常日志的基础去重指纹。

    Args:
        text: 原始异常日志文本。

    Returns:
        str: 移除时间、PID和动态标识后的稳定指纹。
    """
    value = text.lower()
    value = re.sub(r"\b\d{4}[-/]\d{2}[-/]\d{2}[t ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:z|[+-]\d{2}:?\d{2})?", "<time>", value)
    value = re.sub(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b", "<time>", value)
    value = re.sub(r"\b[0-9a-f]{24,}\b", "<id>", value)
    value = re.sub(r"\b(pid|process|thread|spanid|traceid|container)=?\s*[0-9a-f-]+", r"\1=<id>", value)
    value = re.sub(r"\b\d+\b", "<n>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:900]


def group_logs(server: ServerSeries) -> List[LogGroup]:
    """按服务器归并多个巡检时点中语义稳定的重复日志。

    Args:
        server: 同一服务器的巡检时间序列。

    Returns:
        List[LogGroup]: 基础去重后的异常日志组。
    """
    groups: Dict[str, LogGroup] = {}
    server_name = server.latest.remark
    for snapshot in server.snapshots:
        for raw in snapshot.raw_logs:
            key_text = raw.category + "|" + fingerprint(raw.text)
            group_id = hashlib.sha1((server.key + "|" + key_text).encode("utf-8")).hexdigest()[:12]
            group = groups.get(group_id)
            if group is None:
                group = LogGroup(
                    group_id=group_id,
                    server_key=server_name,
                    category=raw.category,
                    fingerprint=key_text,
                    sample=raw.text,
                    sections=[raw.section],
                    occurrences=1,
                    first_seen=raw.snapshot_time,
                    last_seen=raw.snapshot_time,
                )
                groups[group_id] = group
                continue
            if raw.section not in group.sections:
                group.sections.append(raw.section)
    return list(groups.values())


def rule_assessment(group: LogGroup) -> None:
    """在AI不可用前为日志组生成保守的规则评估。

    Args:
        group: 待评估的异常日志组。

    Returns:
        None: 评估字段直接写回日志组。
    """
    text = group.sample.lower()
    if any(token in text for token in ("oom-killer", "outofmemory", "no space left on device", "panic", "segfault", "data corruption")):
        group.severity, group.immediate, group.needs_fix = "critical", True, True
        group.title = "资源耗尽或进程级严重异常"
    elif any(token in text for token in ("fatal", "unhealthy", "read-only file system", "i/o error", "failed to start")):
        group.severity, group.immediate, group.needs_fix = "high", True, True
        group.title = "服务可用性异常"
    elif "error" in text or "exception" in text or "failed" in text or "connection reset" in text:
        group.severity, group.immediate, group.needs_fix = "medium", False, True
        group.title = "运行错误或连接异常"
    elif "warning" in text or "warn" in text:
        group.severity, group.immediate, group.needs_fix = "low", False, False
        group.title = "警告日志"
    else:
        group.severity, group.immediate, group.needs_fix = "info", False, False
        group.title = "一般异常线索"
    group.reason = "AI 未执行时的规则预分类；不替代模型评估。"
    group.assessment_source = "规则预分类"


def assess_with_rules(groups: Iterable[LogGroup]) -> None:
    """批量应用本地规则，保证每条日志都有可展示的初始结论。

    Args:
        groups: 待评估日志组集合。

    Returns:
        None: 评估结果直接写回各日志组。
    """
    for group in groups:
        rule_assessment(group)


def sort_groups(groups: Iterable[LogGroup]) -> List[LogGroup]:
    """按重要程度和发现时间排列PDF中的异常日志。

    Args:
        groups: 已评估日志组集合。

    Returns:
        List[LogGroup]: 严重异常优先的有序日志组。
    """
    return sorted(
        groups,
        key=lambda item: (
            SEVERITY_ORDER.get(item.severity, 9),
            0 if item.immediate else 1,
            item.category,
        ),
    )

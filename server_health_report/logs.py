from __future__ import annotations

import hashlib
import re
from typing import Dict, Iterable, List

from .models import LogGroup, RawLog, ServerSeries


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unassessed": 5}


def fingerprint(text: str) -> str:
    value = text.lower()
    value = re.sub(r"\b\d{4}[-/]\d{2}[-/]\d{2}[t ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:z|[+-]\d{2}:?\d{2})?", "<time>", value)
    value = re.sub(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b", "<time>", value)
    value = re.sub(r"\b[0-9a-f]{24,}\b", "<id>", value)
    value = re.sub(r"\b(pid|process|thread|spanid|traceid|container)=?\s*[0-9a-f-]+", r"\1=<id>", value)
    value = re.sub(r"\b\d+\b", "<n>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:900]


def group_logs(server: ServerSeries) -> List[LogGroup]:
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
                    occurrences=0,
                    first_seen=raw.snapshot_time,
                    last_seen=raw.snapshot_time,
                )
                groups[group_id] = group
            group.occurrences += 1
            if raw.section not in group.sections:
                group.sections.append(raw.section)
            group.first_seen = min(group.first_seen, raw.snapshot_time) if group.first_seen else raw.snapshot_time
            group.last_seen = max(group.last_seen, raw.snapshot_time) if group.last_seen else raw.snapshot_time
    return list(groups.values())


def rule_assessment(group: LogGroup) -> None:
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
        group.severity, group.immediate, group.needs_fix = "low", False, group.occurrences > 5
        group.title = "重复警告日志"
    else:
        group.severity, group.immediate, group.needs_fix = "info", False, False
        group.title = "一般异常线索"
    group.reason = "AI 未执行时的规则预分类；不替代模型评估。"
    group.assessment_source = "规则预分类"


def assess_with_rules(groups: Iterable[LogGroup]) -> None:
    for group in groups:
        rule_assessment(group)


def sort_groups(groups: Iterable[LogGroup]) -> List[LogGroup]:
    return sorted(
        groups,
        key=lambda item: (
            SEVERITY_ORDER.get(item.severity, 9),
            0 if item.immediate else 1,
            -item.occurrences,
            item.category,
        ),
    )

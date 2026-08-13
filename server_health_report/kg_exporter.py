from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import ServerSeries, Snapshot


SCHEMA_VERSION = "1.0"


def _stable_id(prefix: str, *parts: object) -> str:
    """根据业务实体关键字段生成可重复计算的知识图谱ID。"""
    raw = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _time_text(value: Optional[datetime]) -> Optional[str]:
    """将巡检时间转换为带秒精度的ISO字符串。"""
    return value.isoformat(timespec="seconds") if value else None


def _metric_series(server: ServerSeries, attribute: str) -> List[Dict[str, Any]]:
    """导出知识图谱资源评估所引用的指标观测序列。"""
    values: List[Dict[str, Any]] = []
    for snapshot in server.snapshots:
        value = getattr(snapshot, attribute)
        if value is not None:
            values.append({"observed_at": _time_text(snapshot.captured_at), "value": float(value)})
    return values


def _add_recommended_action(
    entities: List[dict], relations: List[dict], execution_history: List[dict],
    subject_id: str, recommendation: str, action_analysis: str, source: str,
) -> None:
    """为异常或指标建立尚未执行的建议操作节点和关系。

    Args:
        entities: 待写入的知识图谱节点集合。
        relations: 待写入的知识图谱关系集合。
        execution_history: 面向Agent顺序读取的履历集合。
        subject_id: 产生建议的日志事件或资源评估ID。
        recommendation: AI或规则给出的处理建议。
        action_analysis: 具体核验或操作分析。
        source: 评估来源。

    Returns:
        None: 节点、关系和履历直接追加到输入集合。
    """
    recommendation = (recommendation or "").strip()
    action_analysis = (action_analysis or "").strip()
    if not recommendation and not action_analysis:
        return
    action_id = _stable_id("recommended_action", subject_id, recommendation, action_analysis)
    entities.append({
        "id": action_id,
        "type": "RecommendedAction",
        "properties": {
            "recommendation": recommendation,
            "operation_analysis": action_analysis,
            "assessment_source": source,
            "execution_status": "proposed_not_executed",
        },
    })
    relations.append({"source": subject_id, "type": "PROPOSES_ACTION", "target": action_id})
    execution_history.append({
        "event_id": action_id,
        "event_type": "recommended_action",
        "status": "proposed_not_executed",
        "subject_id": subject_id,
        "recommendation": recommendation,
        "operation_analysis": action_analysis,
        "source": source,
    })


def _snapshot_properties(snapshot: Snapshot) -> dict:
    """把巡检快照转换为可序列化的知识图谱属性。"""
    return {
        "captured_at": _time_text(snapshot.captured_at),
        "captured_text": snapshot.captured_text,
        "overall_status": snapshot.overall_status,
        "profile": snapshot.profile,
        "collection_state": snapshot.collection_state,
        "execution_permission": snapshot.execution_permission,
        "script_version": snapshot.script_version,
        "config_file": snapshot.config_file,
        "metrics": {
            "cpu_percent": snapshot.cpu_percent,
            "memory_percent": snapshot.memory_percent,
            "disk_percent": snapshot.disk_percent,
            "inode_percent": snapshot.inode_percent,
            "io_iowait_percent": snapshot.io_iowait_percent,
            "io_util_percent": snapshot.io_util_percent,
            "io_device": snapshot.io_device,
        },
        "processes": [
            {"name": process.name, "threads": process.threads, "pids": process.pids, "detail": process.detail}
            for process in snapshot.processes.values()
        ],
        "log_directories": [
            {"path": item.path, "size_bytes": item.size_bytes, "file_count": item.file_count, "detail": item.detail}
            for item in snapshot.log_directories.values()
        ],
        "summary": [
            {"status": row.status, "item": row.item, "detail": row.detail}
            for row in snapshot.summary_rows
        ],
        "collected_items": snapshot.collected_items,
        "limited_items": snapshot.limited_items,
        "excluded_items": snapshot.excluded_items,
    }


def export_operations_history(
    servers: Iterable[ServerSeries], source_paths: Iterable[Path], pdf_path: Path,
    output_path: Path, report_date: str, ai_status: str,
) -> Path:
    """导出适合知识图谱Agent摄取的节点、关系和运维时间线。

    Args:
        servers: 已完成解析和评估的服务器时间序列。
        source_paths: 本次报告使用的原始巡检TXT。
        pdf_path: 已生成的PDF报告路径。
        output_path: 运维执行履历JSON输出路径。
        report_date: 报告业务日期。
        ai_status: DeepSeek或规则评估的执行状态。

    Returns:
        Path: 成功写入的知识图谱履历文件路径。
    """
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    servers = list(servers)
    source_paths = list(source_paths)
    # 报告和实体ID采用内容关键字段计算，便于知识图谱多次导入时做幂等合并。
    report_id = _stable_id("inspection_report", report_date, pdf_path.resolve())
    report_execution_id = _stable_id("operation_execution", report_id, "report_generation")
    entities: List[dict] = [
        {
            "id": report_id,
            "type": "InspectionReport",
            "properties": {
                "report_date": report_date,
                "generated_at": generated_at,
                "pdf_path": str(pdf_path.resolve()),
                "history_path": str(output_path.resolve()),
                "source_txt_count": len(source_paths),
                "server_count": len(servers),
                "ai_assessment_status": ai_status,
            },
        },
        {
            "id": report_execution_id,
            "type": "OperationExecution",
            "properties": {
                "operation_kind": "inspection_report_generation",
                "status": "completed",
                "completed_at": generated_at,
                "result_pdf": str(pdf_path.resolve()),
            },
        },
    ]
    relations: List[dict] = [{"source": report_execution_id, "type": "GENERATED", "target": report_id}]
    execution_history: List[dict] = [{
        "event_id": report_execution_id,
        "event_type": "inspection_report_generation",
        "status": "completed",
        "occurred_at": generated_at,
        "result_id": report_id,
        "result_pdf": str(pdf_path.resolve()),
    }]

    evidence_ids: Dict[Path, str] = {}
    for path in source_paths:
        resolved = path.resolve()
        evidence_id = _stable_id("evidence", resolved)
        evidence_ids[resolved] = evidence_id
        try:
            size_bytes = resolved.stat().st_size
        except OSError:
            size_bytes = None
        entities.append({
            "id": evidence_id,
            "type": "EvidenceFile",
            "properties": {"file_name": resolved.name, "path": str(resolved), "size_bytes": size_bytes},
        })
        relations.append({"source": report_id, "type": "USES_EVIDENCE", "target": evidence_id})

    metric_attributes = {
        "cpu": "cpu_percent", "memory": "memory_percent", "disk": "disk_percent",
        "iowait": "io_iowait_percent", "util": "io_util_percent",
    }
    for server in servers:
        latest = server.latest
        server_id = _stable_id("server", server.key)
        entities.append({
            "id": server_id,
            "type": "Server",
            "properties": {"server_key": server.key, "remark": latest.remark, "hostname": latest.hostname},
        })
        relations.append({"source": report_id, "type": "COVERS_SERVER", "target": server_id})

        for snapshot in server.snapshots:
            snapshot_id = _stable_id("inspection_snapshot", server_id, snapshot.captured_text, snapshot.path.resolve())
            entities.append({"id": snapshot_id, "type": "InspectionSnapshot", "properties": _snapshot_properties(snapshot)})
            relations.extend([
                {"source": report_id, "type": "CONTAINS_SNAPSHOT", "target": snapshot_id},
                {"source": snapshot_id, "type": "OBSERVED_SERVER", "target": server_id},
            ])
            evidence_id = evidence_ids.get(snapshot.path.resolve())
            if evidence_id:
                relations.append({"source": snapshot_id, "type": "SUPPORTED_BY", "target": evidence_id})
            execution_history.append({
                "event_id": snapshot_id,
                "event_type": "server_inspection_collection",
                "status": "completed",
                "occurred_at": _time_text(snapshot.captured_at),
                "server_id": server_id,
                "evidence_id": evidence_id,
            })

        for assessment in server.resource_assessments:
            assessment_id = _stable_id("resource_assessment", report_id, server_id, assessment.metric)
            entities.append({
                "id": assessment_id,
                "type": "ResourceAssessment",
                "properties": {
                    "metric": assessment.metric,
                    "severity": assessment.severity,
                    "needs_fix": assessment.needs_fix,
                    "title": assessment.title,
                    "judgment": assessment.reason,
                    "assessment_source": assessment.assessment_source,
                    "observations": _metric_series(server, metric_attributes.get(assessment.metric, "cpu_percent")),
                },
            })
            relations.extend([
                {"source": report_id, "type": "CONTAINS_ASSESSMENT", "target": assessment_id},
                {"source": assessment_id, "type": "ASSESSES_SERVER", "target": server_id},
            ])
            _add_recommended_action(
                entities, relations, execution_history, assessment_id,
                assessment.recommendation, assessment.action_analysis, assessment.assessment_source,
            )

        for group in server.log_groups:
            log_id = _stable_id("log_event", report_id, server_id, group.group_id)
            entities.append({
                "id": log_id,
                "type": "LogEvent",
                "properties": {
                    "group_id": group.group_id,
                    "category": group.category,
                    "severity": group.severity,
                    "needs_fix": group.needs_fix,
                    "title": group.title,
                    "judgment": group.reason,
                    "sample": group.sample,
                    "sections": group.sections,
                    "first_seen": _time_text(group.first_seen),
                    "last_seen": _time_text(group.last_seen),
                    "occurrences_after_deduplication": group.occurrences,
                    "duplicate_of": group.duplicate_of,
                    "assessment_source": group.assessment_source,
                },
            })
            relations.extend([
                {"source": report_id, "type": "CONTAINS_LOG_EVENT", "target": log_id},
                {"source": log_id, "type": "OBSERVED_ON", "target": server_id},
            ])
            _add_recommended_action(
                entities, relations, execution_history, log_id,
                group.recommendation, group.action_analysis, group.assessment_source,
            )

    # 语义边界随数据一起输出，防止下游Agent把AI建议误判成已实施操作。
    document = {
        "schema_version": SCHEMA_VERSION,
        "document_type": "operations_execution_history_knowledge_graph",
        "generated_at": generated_at,
        "semantic_boundary": {
            "completed": "巡检采集、评估和报告生成等已实际发生的事件",
            "proposed_not_executed": "AI给出的建议或操作分析，仅为待执行方案，不代表已经修改服务器",
        },
        "graph_schema": {
            "node_types": [
                "InspectionReport", "OperationExecution", "Server", "InspectionSnapshot",
                "EvidenceFile", "ResourceAssessment", "LogEvent", "RecommendedAction",
            ],
            "relation_types": [
                "GENERATED", "USES_EVIDENCE", "COVERS_SERVER", "CONTAINS_SNAPSHOT",
                "OBSERVED_SERVER", "SUPPORTED_BY", "CONTAINS_ASSESSMENT", "ASSESSES_SERVER",
                "CONTAINS_LOG_EVENT", "OBSERVED_ON", "PROPOSES_ACTION",
            ],
        },
        "root_report_id": report_id,
        "entities": entities,
        "relations": relations,
        "execution_history": execution_history,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path

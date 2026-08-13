from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class SummaryRow:
    """保存巡检概要表中的单项检查结论。"""
    status: str
    item: str
    detail: str


@dataclass
class ProcessMetric:
    """保存指定业务进程的名称、线程数和进程实例信息。"""
    name: str
    threads: int = 0
    pids: List[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class LogDirectoryMetric:
    """保存业务日志目录在单次巡检时的容量与文件数量。"""
    path: str
    size_bytes: float
    file_count: int = 0
    detail: str = ""


@dataclass
class RawLog:
    """保存从巡检TXT中提取、尚未去重的异常日志证据。"""
    category: str
    section: str
    text: str
    snapshot_time: datetime


@dataclass
class Snapshot:
    """表示一台服务器在一个采样时间点的完整巡检快照。"""
    path: Path
    remark: str = "未识别"
    hostname: str = "未识别"
    captured_at: Optional[datetime] = None
    captured_text: str = "未识别"
    overall_status: str = "未识别"
    script_version: str = "未识别"
    config_file: str = "未识别"
    summary_rows: List[SummaryRow] = field(default_factory=list)
    counters: Dict[str, int] = field(default_factory=dict)
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    disk_percent: Optional[float] = None
    inode_percent: Optional[float] = None
    io_iowait_percent: Optional[float] = None
    io_util_percent: Optional[float] = None
    io_device: str = "未识别"
    execution_permission: str = "未识别"
    profile: str = "标准巡检"
    collection_state: str = "完整"
    collected_items: List[str] = field(default_factory=list)
    limited_items: List[str] = field(default_factory=list)
    excluded_items: List[str] = field(default_factory=list)
    processes: Dict[str, ProcessMetric] = field(default_factory=dict)
    log_directories: Dict[str, LogDirectoryMetric] = field(default_factory=dict)
    docker_details: List[str] = field(default_factory=list)
    business_directories: List[str] = field(default_factory=list)
    service_details: List[str] = field(default_factory=list)
    raw_logs: List[RawLog] = field(default_factory=list)

    @property
    def server_key(self) -> str:
        """返回跨时间点归并服务器时使用的稳定业务标识。"""
        return self.hostname if self.hostname != "未识别" else self.remark


@dataclass
class LogGroup:
    """表示基础去重或AI语义去重后的一组同类异常日志。"""
    group_id: str
    server_key: str
    category: str
    fingerprint: str
    sample: str
    sections: List[str] = field(default_factory=list)
    occurrences: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    severity: str = "unassessed"
    immediate: Optional[bool] = None
    needs_fix: Optional[bool] = None
    title: str = "未评估异常"
    reason: str = ""
    recommendation: str = ""
    action_analysis: str = ""
    duplicate_of: Optional[str] = None
    assessment_source: str = "未评估"


@dataclass
class ResourceAssessment:
    """保存AI或规则对单项服务器资源趋势的评估结果。"""
    metric: str
    severity: str = "unassessed"
    immediate: Optional[bool] = None
    needs_fix: Optional[bool] = None
    title: str = "未评估指标"
    reason: str = ""
    recommendation: str = ""
    action_analysis: str = ""
    assessment_source: str = "未评估"


@dataclass
class ServerSeries:
    """聚合同一服务器的多次巡检快照、日志和资源评估。"""
    key: str
    snapshots: List[Snapshot]
    log_groups: List[LogGroup] = field(default_factory=list)
    resource_assessments: List[ResourceAssessment] = field(default_factory=list)

    @property
    def latest(self) -> Snapshot:
        """返回该服务器时间序列中最新的一次巡检快照。"""
        return self.snapshots[-1]

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class SummaryRow:
    status: str
    item: str
    detail: str


@dataclass
class ProcessMetric:
    name: str
    threads: int = 0
    pids: List[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class LogDirectoryMetric:
    path: str
    size_bytes: float
    file_count: int = 0
    detail: str = ""


@dataclass
class RawLog:
    category: str
    section: str
    text: str
    snapshot_time: datetime


@dataclass
class Snapshot:
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
        return self.hostname if self.hostname != "未识别" else self.remark


@dataclass
class LogGroup:
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
    assessment_source: str = "未评估"


@dataclass
class ServerSeries:
    key: str
    snapshots: List[Snapshot]
    log_groups: List[LogGroup] = field(default_factory=list)

    @property
    def latest(self) -> Snapshot:
        return self.snapshots[-1]

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import LogDirectoryMetric, ProcessMetric, RawLog, ServerSeries, Snapshot, SummaryRow


STATUS_ORDER = ("异常", "警告", "不可判定", "正常", "信息")
SUMMARY_ROW_RE = re.compile(r"^\[(正常|警告|异常|不可判定|信息)\]\s*(.+?)：(.*)$")
SECTION_RE = re.compile(r"^=+\s*(.*?)\s*=+$")
PROCESS_RE = re.compile(r"^进程\s+(.+?)：(.+)$")
PROCESS_INSTANCE_RE = re.compile(
    r"PID=(\d+).*?线程数=(\d+)(?:\s+CPU=([\d.]+)%\s+内存=([\d.]+)%)?"
)
CLIENT_PROCESS_SECTION_RE = re.compile(r"^---\s*进程：(.+?)；匹配规则：.*?---$")
INTERFACE_PROCESS_SECTION_RE = re.compile(r"^(?:正常|缺失)\s+进程=(.+?)\s+匹配规则=(.+)$")
LOG_DIR_RE = re.compile(r"([^，]+)=文件数(\d+)，目录占用([^，]+)，([^，]+(?:，[^(，]+(?:\([^)]*\))?)?)")
LOG_SIGNAL_RE = re.compile(
    r"(?:\bERROR\b|\bFATAL\b|Exception\b|Traceback\b|OutOfMemory|oom-killer|OOMKilled=true|"
    r"\blevel=(?:error|warning|fatal)\b|\bfailed\b|No space left on device|connection reset by peer|"
    r"unhealthy|segfault|panic|I/O error|read-only file system)",
    re.IGNORECASE,
)


def read_text(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            pass
    return payload.decode("utf-8", errors="replace")


def clean(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", value).strip()


def parse_datetime(value: str) -> Optional[datetime]:
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    match = re.search(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})[ T_](\d{2})[:]?([0-5]\d)[:]?([0-5]\d)", value)
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups()))
    except ValueError:
        return None


def parse_size(value: str) -> float:
    match = re.search(r"([\d.]+)\s*([KMGTPE]?)(?:i?B)?", value, re.IGNORECASE)
    if not match:
        return 0.0
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}.get(match.group(2).upper(), 0)
    return float(match.group(1)) * (1024.0 ** power)


def _percent(detail: str, pattern: str) -> Optional[float]:
    match = re.search(pattern, detail)
    return float(match.group(1)) if match else None


def _process_name_from_command(command: str, fallback: str, pattern: str = "") -> str:
    """优先返回 ps 命令行里的真实程序文件名，配置别名只用于无法识别时兜底。"""
    candidates = re.findall(r"(?:^|[/\\\s])([\w.-]+(?:\.jar|\.pyc?|\.sh))(?:\s|$)", command)
    if candidates:
        actual = candidates[-1]
    elif "scheduler_deamon" in pattern:
        actual = "scheduler_deamon.pyc"
    elif "enrobot-api" in pattern:
        actual = "enrobot-api-0.1-SNAPSHOT.jar"
    elif "rpa-admin" in pattern:
        actual = "rpa-admin-0.1-SNAPSHOT.jar"
    elif "rpaAdmin" in pattern:
        actual = "rpaAdmin-0.0.1-SNAPSHOT.jar"
    elif "enrobot-client" in pattern:
        actual = "enrobot-client-0.1-SNAPSHOT.jar"
    else:
        actual = fallback
    if "spring" in pattern and "profiles" in pattern and "job" in pattern:
        return actual + "（JOB）"
    return actual


def _ps_process_instance(line: str):
    fields = line.split()
    # PID PPID USER LSTART(5列) ETIMES CPU MEM NLWP STAT COMM ARGS
    if len(fields) < 14 or not fields[0].isdigit() or not fields[1].isdigit() or not fields[11].isdigit():
        return None
    return fields[0], int(fields[11]), fields[9], fields[10], " ".join(fields[13:])


def _configured_processes(lines: List[str]) -> Dict[str, ProcessMetric]:
    """解析 Client 与 Interface 两种指定进程明细，统一改用真实进程文件名。"""
    result: Dict[str, ProcessMetric] = {}
    current_alias: Optional[str] = None
    current_pattern = ""
    current_instances = []

    def flush() -> None:
        nonlocal current_alias, current_pattern, current_instances
        if current_alias is None:
            return
        sample_command = current_instances[0][4] if current_instances else ""
        actual_name = _process_name_from_command(sample_command, current_alias, current_pattern)
        # 同一实际 JAR 的 API 与 JOB 需要按 profile 区分，并避免 API 汇总重复计算 JOB PID。
        if actual_name == "enrobot-api-0.1-SNAPSHOT.jar" and current_alias.lower().endswith("api"):
            current_instances = [item for item in current_instances if "spring.profiles.active=job" not in item[4]]
        detail = "；".join(
            f"PID={pid} 线程数={threads} CPU={cpu}% 内存={memory}%"
            for pid, threads, cpu, memory, _ in current_instances
        )
        if not detail:
            detail = "未发现匹配进程，线程数=0"
        result[actual_name] = ProcessMetric(
            name=actual_name,
            threads=sum(item[1] for item in current_instances),
            pids=[item[0] for item in current_instances],
            detail=detail,
        )
        current_alias = None
        current_pattern = ""
        current_instances = []

    for line in lines:
        client = CLIENT_PROCESS_SECTION_RE.match(line)
        interface = INTERFACE_PROCESS_SECTION_RE.match(line)
        if client or interface:
            flush()
            if client:
                current_alias = client.group(1)
                pattern_match = re.search(r"匹配规则：(.+?)\s*---$", line)
                current_pattern = pattern_match.group(1) if pattern_match else ""
            else:
                current_alias, current_pattern = interface.groups()
            continue
        if current_alias is not None:
            if line.startswith("未发现匹配进程") or line.startswith("PID="):
                continue
            instance = _ps_process_instance(line)
            if instance:
                current_instances.append(instance)
            elif line.startswith(("正常 进程=", "缺失 进程=", "====================", "--- JVM")):
                flush()
    flush()
    return result


def classify_log(section: str, line: str) -> str:
    joined = f"{section} {line}".lower()
    if "docker" in joined or "dockerd" in joined or "containerd" in joined or "容器" in joined:
        return "Docker日志"
    if "应用日志" in section or "日志目录" in section or re.search(r"(?:\.log|\.out)(?:\s|:|$)", line, re.I):
        return "应用日志目录日志"
    return "系统/进程日志"


def is_log_line(line: str) -> bool:
    if not line or line.startswith(("$ ", "---", "###", "字段说明：", "以下内容")):
        return False
    if any(token in line for token in ("扫描最后", "匹配规则=", "ERROR/Exception/Traceback", "失败数=", "候选日志：")):
        return False
    return bool(LOG_SIGNAL_RE.search(line))


def parse_snapshot(path: Path) -> Snapshot:
    source_lines = [clean(raw) for raw in read_text(path).splitlines()]
    basic: Dict[str, str] = {}
    rows: List[SummaryRow] = []
    counters: Dict[str, int] = {}
    front_lines: List[str] = []
    raw_logs: List[RawLog] = []
    section = "报告前言"
    in_front = False
    in_raw = False
    overall = "未识别"
    raw_candidates = []
    process_detail_lines: Dict[str, List[str]] = {}
    current_process_name: Optional[str] = None

    for line in source_lines:
        if line == "--- 前置详细指标 ---":
            in_front = True
            continue
        if "原始采集明细" in line:
            in_front = False
            in_raw = True
            continue
        match = SECTION_RE.match(line)
        if match:
            section = match.group(1)
            current_process_name = None
            continue
        process_section = CLIENT_PROCESS_SECTION_RE.match(line)
        if process_section:
            current_process_name = process_section.group(1)
            process_detail_lines.setdefault(current_process_name, [])
            continue
        if current_process_name and line:
            process_detail_lines[current_process_name].append(line)
        if "：" in line:
            key, value = line.split("：", 1)
            if key in {"服务器备注", "脚本版本", "配置文件", "巡检开始时间", "生成时间", "主机名", "执行权限"}:
                basic[key] = value.strip()
        if line.startswith("总体状态："):
            overall = line.split("：", 1)[1].strip()
        elif line.startswith("统计："):
            counters.update({name: int(count) for name, count in re.findall(r"(正常|警告|异常|不可判定|信息)=(\d+)", line)})
        else:
            summary = SUMMARY_ROW_RE.match(line)
            if summary:
                rows.append(SummaryRow(*summary.groups()))
        if in_front and line:
            front_lines.append(line)
        if in_raw and is_log_line(line):
            raw_candidates.append((section, line))

    captured_text = basic.get("巡检开始时间") or basic.get("生成时间") or path.stem
    captured_at = parse_datetime(captured_text) or parse_datetime(path.stem) or datetime.fromtimestamp(path.stat().st_mtime)
    snapshot = Snapshot(
        path=path,
        remark=basic.get("服务器备注", path.stem),
        hostname=basic.get("主机名", "未识别"),
        captured_at=captured_at,
        captured_text=captured_text,
        overall_status=overall,
        script_version=basic.get("脚本版本", "未识别"),
        config_file=basic.get("配置文件", "未识别"),
        execution_permission=basic.get("执行权限", "未识别"),
        summary_rows=rows,
        counters=counters,
    )
    for row in rows:
        if row.item in {"CPU与负载", "CPU"}:
            snapshot.cpu_percent = _percent(row.detail, r"使用率=([\d.]+)%")
        elif row.item == "内存":
            available = _percent(row.detail, r"可用=.*?[（(]([\d.]+)%")
            snapshot.memory_percent = None if available is None else max(0.0, 100.0 - available)
        elif row.item == "磁盘容量":
            snapshot.disk_percent = _percent(row.detail, r"最高使用率=([\d.]+)%")
        elif row.item == "inode":
            snapshot.inode_percent = _percent(row.detail, r"最高使用率=([\d.]+)%")
        elif row.item == "磁盘IO性能":
            snapshot.io_iowait_percent = _percent(row.detail, r"iowait=([\d.]+)%")
            snapshot.io_util_percent = _percent(row.detail, r"(?:最高)?%util=([\d.]+)%")
            device = re.search(r"最繁忙设备=([^，,]+)", row.detail)
            if device:
                snapshot.io_device = device.group(1).strip()

    for line in front_lines:
        process = PROCESS_RE.match(line)
        if process:
            name, detail = process.groups()
            instances = PROCESS_INSTANCE_RE.findall(detail)
            snapshot.processes[name] = ProcessMetric(
                name=name,
                threads=sum(int(item[1]) for item in instances),
                pids=[item[0] for item in instances],
                detail=detail,
            )
        elif line.startswith("Docker容器 "):
            snapshot.docker_details.append(line)
        elif line.startswith("日志目录逐项状态："):
            detail = line.split("：", 1)[1]
            for match in LOG_DIR_RE.finditer(detail):
                path_text, count, size, extra = match.groups()
                snapshot.log_directories[path_text] = LogDirectoryMetric(
                    path=path_text,
                    size_bytes=parse_size(size),
                    file_count=int(count),
                    detail=f"文件数={count}，占用={size}，{extra}",
                )
        elif line.startswith("业务目录逐项状态："):
            snapshot.business_directories.extend(filter(None, line.split("：", 1)[1].rstrip("，").split("，")))
        elif line.startswith("systemd服务逐项状态："):
            snapshot.service_details.extend(filter(None, line.split("：", 1)[1].rstrip("，").split("，")))

    configured = _configured_processes(source_lines)
    if configured:
        snapshot.processes = configured

    # PDF 概要中的“指定进程”不展示配置别名，直接列真实进程名及线程总数。
    process_summary = "；".join(
        f"{process.name}：{'存在' if process.pids else '未发现'}，线程数={process.threads}"
        for process in snapshot.processes.values()
    )
    if process_summary:
        for row in snapshot.summary_rows:
            if row.item in {"指定进程", "宿主机进程"}:
                row.detail = process_summary

    item_names = {row.item for row in rows}
    is_low_privilege_client = (
        "普通用户" in snapshot.execution_permission
        or ("磁盘IO性能" in item_names and item_names.issubset({"CPU", "内存", "Swap", "磁盘容量", "inode", "磁盘IO性能", "指定进程", "Docker容器"}))
    )
    if is_low_privilege_client:
        snapshot.profile = "Client低权限巡检"
        snapshot.excluded_items = ["systemd", "端口", "网络依赖", "JVM", "应用日志", "业务目录"]
        if not snapshot.processes:
            snapshot.processes["enrobot-client-0.1-SNAPSHOT.jar"] = ProcessMetric(
                name="enrobot-client-0.1-SNAPSHOT.jar",
                threads=-1,
                detail="本次TXT未输出匹配进程明细，线程数未采集",
            )
            for row in snapshot.summary_rows:
                if row.item == "指定进程":
                    row.detail = "enrobot-client-0.1-SNAPSHOT.jar：线程数未采集"
    else:
        snapshot.profile = "Interface/完整巡检"
    snapshot.collected_items = [row.item for row in rows if row.status not in {"不可判定"}]
    snapshot.limited_items = [row.item for row in rows if row.status == "不可判定"]
    if snapshot.limited_items:
        snapshot.collection_state = "部分受限"
    elif snapshot.excluded_items:
        snapshot.collection_state = "按低权限范围完成"

    # 老版 Interface TXT 尚无 IO 汇总时明确标记为源文件未采集，不补造数值。
    if "磁盘IO性能" not in item_names:
        snapshot.excluded_items.append("磁盘IO性能（旧版TXT未采集）")
        if snapshot.profile == "Interface/完整巡检" and snapshot.collection_state == "完整":
            snapshot.collection_state = "源文件部分缺项"

    if overall == "未识别":
        priority = {"异常": 0, "警告": 1, "不可判定": 2, "正常": 3, "信息": 4}
        meaningful = [row.status for row in rows if row.status in priority]
        if meaningful:
            worst = min(meaningful, key=lambda value: priority[value])
            snapshot.overall_status = "部分不可判定" if worst == "不可判定" else worst

    for log_section, text in raw_candidates:
        snapshot.raw_logs.append(RawLog(
            category=classify_log(log_section, text),
            section=log_section,
            text=text[:1600],
            snapshot_time=captured_at,
        ))
    if not snapshot.counters:
        counted = Counter(row.status for row in rows)
        snapshot.counters = {status: counted.get(status, 0) for status in STATUS_ORDER}
    return snapshot


def group_servers(snapshots: Iterable[Snapshot]) -> List[ServerSeries]:
    grouped: Dict[str, List[Snapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.server_key, []).append(snapshot)
    servers = []
    for key, items in grouped.items():
        items.sort(key=lambda item: item.captured_at or datetime.min)
        servers.append(ServerSeries(key=key, snapshots=items))
    return sorted(servers, key=lambda server: server.latest.remark)

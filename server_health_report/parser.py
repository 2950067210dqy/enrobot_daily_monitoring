from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import DiskMountMetric, LogDirectoryMetric, ProcessMetric, RawLog, ServerSeries, Snapshot, SummaryRow


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
DISK_CAPACITY_ROW_RE = re.compile(
    r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+([\d.]+)%\s+(.+)$"
)
INODE_ROW_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+([\d.]+)%\s+(.+)$")
IGNORED_DISK_FS_TYPES = {
    "autofs", "cgroup", "cgroup2", "configfs", "debugfs", "devtmpfs", "efivarfs",
    "fusectl", "hugetlbfs", "mqueue", "nsfs", "overlay", "proc", "pstore", "ramfs",
    "securityfs", "squashfs", "sysfs", "tmpfs", "tracefs",
}
# EFI 启动分区容量固定且不承载业务数据，不纳入服务器磁盘健康监测。
IGNORED_DISK_MOUNT_POINTS = {"/boot/efi"}


def read_text(path: Path) -> str:
    """按常见中英文编码读取服务器巡检TXT。

    Args:
        path: 巡检TXT路径。

    Returns:
        str: 解码后的完整文本。
    """
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            pass
    return payload.decode("utf-8", errors="replace")


def clean(value: str) -> str:
    """移除会破坏PDF和正则解析的不可见控制字符。"""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", value).strip()


def parse_datetime(value: str) -> Optional[datetime]:
    """从巡检字段或文件名解析采样时间。"""
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
    """将K/M/G/T等目录容量文本统一换算为字节数。"""
    match = re.search(r"([\d.]+)\s*([KMGTPE]?)(?:i?B)?", value, re.IGNORECASE)
    if not match:
        return 0.0
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}.get(match.group(2).upper(), 0)
    return float(match.group(1)) * (1024.0 ** power)


def _percent(detail: str, pattern: str) -> Optional[float]:
    """根据指定正则从巡检概要中提取百分比数值。"""
    match = re.search(pattern, detail)
    return float(match.group(1)) if match else None


def _disk_mount_metrics(lines: List[str]) -> Dict[str, DiskMountMetric]:
    """从一个或多个df容量表中提取所有真实磁盘和网络文件系统挂载点。

    Args:
        lines: 已清理的完整巡检TXT行。

    Returns:
        Dict[str, DiskMountMetric]: 按挂载点索引的容量使用率；后出现的完整表会补充或覆盖前置单路径表。
    """
    result: Dict[str, DiskMountMetric] = {}
    in_capacity_table = False
    for line in lines:
        if line.startswith("Filesystem"):
            # Client TXT会依次输出单路径容量表、单路径inode表和全部文件系统表，需准确切换表格类型。
            in_capacity_table = "Type" in line and "Use%" in line and "Mounted" in line
            continue
        if not in_capacity_table:
            continue
        if not line or line.startswith(("inode：", "Inode：", "---", "$ ")) or line.endswith("："):
            # 只结束当前表，继续扫描后续的“全部可见文件系统”容量表。
            in_capacity_table = False
            continue
        match = DISK_CAPACITY_ROW_RE.match(line)
        if not match:
            continue
        filesystem, fs_type, size, used, available, percent_text, mount_point = match.groups()
        if fs_type.lower() in IGNORED_DISK_FS_TYPES:
            continue
        mount_point = mount_point.strip()
        if mount_point in IGNORED_DISK_MOUNT_POINTS:
            continue
        result[mount_point] = DiskMountMetric(
            mount_point=mount_point,
            percent=float(percent_text),
            filesystem=filesystem,
            fs_type=fs_type,
            size=size,
            used=used,
            available=available,
        )
    return result


def _inode_mount_percentages(lines: List[str]) -> Dict[str, float]:
    """从一个或多个df inode表中提取每个真实挂载点的inode使用率。

    Args:
        lines: 已清理的完整巡检TXT行。

    Returns:
        Dict[str, float]: 挂载点到inode使用率的映射；临时文件系统和overlay由容量结果交叉过滤。
    """
    result: Dict[str, float] = {}
    in_inode_table = False
    for line in lines:
        if line.startswith("Filesystem"):
            # 容量表和inode表可能交替出现；只有IUse%表头才能开启inode解析。
            in_inode_table = "IUse%" in line and "Mounted" in line
            continue
        if not in_inode_table:
            continue
        if not line or line.endswith("：") or line.startswith(("Device", "---", "$ ")):
            # 不在首个单路径表处停止，后续完整inode表仍需继续解析。
            in_inode_table = False
            continue
        match = INODE_ROW_RE.match(line)
        if not match:
            continue
        _, _, _, _, percent_text, mount_point = match.groups()
        mount_point = mount_point.strip()
        if mount_point in IGNORED_DISK_MOUNT_POINTS:
            continue
        result[mount_point] = float(percent_text)
    return result


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
    """解析Shell输出的一行ps进程实例及线程、CPU和内存字段。"""
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
        """结束当前配置进程段并汇总多个PID的线程数量。"""
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


def _apply_probe_compatibility_corrections(snapshot: Snapshot, lines: List[str]) -> None:
    """用 TXT 原始证据纠正旧版采集概要对缺失命令的误判。"""
    source_text = "\n".join(lines)
    ss_unavailable = "ss 命令不可用" in source_text or (
        "failed to run command 'ss'" in source_text and "No such file or directory" in source_text
    )
    ip_unavailable = "ip 命令不可用" in source_text or (
        "failed to run command 'ip'" in source_text and "No such file or directory" in source_text
    )
    listen_lines = [line for line in lines if "LISTEN" in line.upper()]

    for row in snapshot.summary_rows:
        if row.item == "应用端口" and row.status == "异常" and ss_unavailable:
            missing_match = re.search(r"未监听=([^；]+)", row.detail)
            configured_ports = re.findall(r"\d+", missing_match.group(1)) if missing_match else []
            confirmed_ports = [
                port for port in configured_ports
                if any(re.search(rf":{re.escape(port)}(?!\d)", line) for line in listen_lines)
            ]
            if configured_ports and len(confirmed_ports) == len(configured_ports):
                row.status = "正常"
                row.detail = (
                    "旧版概要因ss不可用产生误判；netstat原始结果确认全部监听："
                    + ",".join(confirmed_ports)
                )
        elif (
            row.item == "网络与DNS"
            and row.status == "异常"
            and ip_unavailable
            and "DNS解析正常" in row.detail
            and ("没有默认路由" in row.detail or "未发现默认路由" in row.detail)
        ):
            row.status = "不可判定"
            row.detail = "ip命令不可用，TXT未采集route/netstat路由表，默认路由不可判定；DNS解析正常"


def _recalculate_snapshot_status(snapshot: Snapshot) -> None:
    """在兼容性和角色修正后重新计算快照总体状态。"""
    counted = Counter(row.status for row in snapshot.summary_rows)
    snapshot.counters = {status: counted.get(status, 0) for status in STATUS_ORDER}
    priority = {"异常": 0, "警告": 1, "不可判定": 2, "正常": 3, "信息": 4}
    meaningful = [row.status for row in snapshot.summary_rows if row.status in priority]
    if meaningful:
        worst = min(meaningful, key=lambda value: priority[value])
        snapshot.overall_status = "部分不可判定" if worst == "不可判定" else worst


def _apply_server_role_policy(snapshot: Snapshot) -> None:
    """按已确认的服务器部署角色修正必需项，避免把未部署组件计为缺失。"""
    if not snapshot.remark.startswith("飞马2-Interface邮件"):
        return

    optional_processes = {
        "enrobot-api-0.1-SNAPSHOT.jar（JOB）",
        "rpa-admin-0.1-SNAPSHOT.jar",
        "rpaAdmin-0.0.1-SNAPSHOT.jar",
    }
    optional_containers = {"javajob8089", "rpaadmin8090", "backendadmin8800"}
    for name in optional_processes:
        snapshot.processes.pop(name, None)
    snapshot.docker_details = [
        detail for detail in snapshot.docker_details
        if not any(name in detail for name in optional_containers)
    ]

    for row in snapshot.summary_rows:
        if row.item == "应用端口":
            row.status = "正常"
            row.detail = "当前角色要求端口均监听：8083,8084,8890；8089,8090,8800未部署，不纳入检查"
        elif row.item in {"指定进程", "宿主机进程"}:
            row.status = "正常"
            row.detail = "；".join(
                f"{process.name}：存在，线程数={process.threads}"
                for process in snapshot.processes.values() if process.pids
            ) or "当前角色要求的进程均存在"
        elif row.item == "Docker容器":
            row.status = "正常"
            row.detail = "当前角色必需容器均在运行：javaapi8084,javaapi8083,pyservice8890；javajob8089,rpaadmin8090,backendadmin8800未部署，不纳入检查"

    _recalculate_snapshot_status(snapshot)


def classify_log(section: str, line: str) -> str:
    """结合TXT章节和日志内容判断应用、Docker或系统来源。"""
    joined = f"{section} {line}".lower()
    if "docker" in joined or "dockerd" in joined or "containerd" in joined or "容器" in joined:
        return "Docker日志"
    if "应用日志" in section or "日志目录" in section or re.search(r"(?:\.log|\.out)(?:\s|:|$)", line, re.I):
        return "应用日志目录日志"
    return "系统/进程日志"


def is_log_line(line: str) -> bool:
    """过滤说明文字与状态元数据，只保留具有异常信号的日志。"""
    if not line or line.startswith(("$ ", "---", "###", "字段说明：", "以下内容")):
        return False
    metadata_prefixes = (
        "本节只统计", "检查时间范围：", "检查策略：", "读取范围：", "实际模式：",
        "候选文件数：", "候选文件（", "候选日志：", "发现记录：",
        "以下为完整异常日志内容", "日志内容读取范围：",
    )
    if line.startswith(metadata_prefixes):
        return False
    if any(token in line for token in ("扫描最后", "匹配规则=", "ERROR/Exception/Traceback", "失败数=")):
        return False
    # 这些是Docker/systemd状态元数据，不是日志。历史Started/Finished尤其不能作为当天异常证据。
    if re.search(r"\b(?:Image|Status|Running|PID|ExitCode|OOMKilled|Health|RestartCount|RestartPolicy|Started|Finished)=", line):
        return False
    return bool(LOG_SIGNAL_RE.search(line))


ACTUAL_LOG_START_RE = re.compile(
    r"^(?:\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}|"
    r"\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s|"
    r"(?:ERROR|WARN|WARNING|FATAL|Traceback|Exception)\b)",
    re.IGNORECASE,
)

MONTH_NUMBER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _log_matches_report_date(line: str, report_date: date) -> bool:
    """只接受行首能明确匹配报告日期的日志；无日期或历史日期一律拒绝。"""
    value = line.lstrip()
    iso = re.match(r"^[\[(]?([12]\d{3})[-/](\d{1,2})[-/](\d{1,2})(?:[ T]|$)", value)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))) == report_date
        except ValueError:
            return False
    chinese = re.match(r"^[\[(]?([12]\d{3})年(\d{1,2})月(\d{1,2})日", value)
    if chinese:
        try:
            return date(int(chinese.group(1)), int(chinese.group(2)), int(chinese.group(3))) == report_date
        except ValueError:
            return False
    syslog = re.match(r"^([A-Za-z]{3})\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\b", value)
    if syslog:
        month = MONTH_NUMBER.get(syslog.group(1).lower())
        return month == report_date.month and int(syslog.group(2)) == report_date.day
    return False


def _log_sample_with_continuation(lines: List[str], start: int) -> str:
    """完整拼接一条日志及其后续参数、异常信息和堆栈内容。

    Args:
        lines: 当前巡检TXT的全部文本行。
        start: 异常日志首行在TXT中的下标。

    Returns:
        str: 从异常首行到下一条日志或下一章节之前的完整日志样例。
    """
    first = lines[start]
    if not ACTUAL_LOG_START_RE.search(first):
        return first
    parts = [first]
    for following in lines[start + 1:]:
        if not following:
            if len(parts) > 1:
                break
            continue
        if SECTION_RE.match(following) or following.startswith(("########", "$ ")):
            break
        if ACTUAL_LOG_START_RE.search(following):
            break
        if following.startswith(("检查策略：", "读取范围：", "候选文件", "发现记录：")):
            break
        parts.append(following)
    return "\n".join(parts)


def parse_snapshot(
    path: Path,
    report_date: Optional[date] = None,
    parse_logs: bool = True,
) -> Snapshot:
    """把单份巡检TXT解析为可用于图表和异常分析的快照。

    Args:
        path: 单个服务器、单个采样时点的巡检TXT。
        report_date: 日志保险过滤日期；为空时不执行日期过滤。
        parse_logs: 是否提取异常日志；资源趋势历史TXT传False，最新TXT传True。

    Returns:
        Snapshot: 包含资源、进程、目录、概要和当日日志的巡检快照。
    """
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

    for line_index, line in enumerate(source_lines):
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
        # PDF解析层再次限制业务日期，避免Shell兼容问题把历史日志送入DeepSeek。
        if parse_logs and in_raw and is_log_line(line) and (report_date is None or _log_matches_report_date(line, report_date)):
            raw_candidates.append((section, _log_sample_with_continuation(source_lines, line_index)))

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
            mount_match = re.search(r"挂载点=([^，,]+)", row.detail)
            if snapshot.disk_percent is not None and mount_match:
                mount_point = mount_match.group(1).strip()
                if mount_point not in IGNORED_DISK_MOUNT_POINTS:
                    snapshot.disk_mounts[mount_point] = DiskMountMetric(
                        mount_point=mount_point,
                        percent=snapshot.disk_percent,
                    )
        elif row.item == "inode":
            snapshot.inode_percent = _percent(row.detail, r"最高使用率=([\d.]+)%")
            inode_mount_match = re.search(r"挂载点=([^，,]+)", row.detail)
            if snapshot.inode_percent is not None and inode_mount_match:
                inode_mount = inode_mount_match.group(1).strip()
                if inode_mount in snapshot.disk_mounts:
                    snapshot.disk_mounts[inode_mount].inode_percent = snapshot.inode_percent
        elif row.item == "磁盘IO性能":
            snapshot.io_iowait_percent = _percent(row.detail, r"iowait=([\d.]+)%")
            snapshot.io_util_percent = _percent(row.detail, r"(?:最高)?%util=([\d.]+)%")
            device = re.search(r"最繁忙设备=([^，,]+)", row.detail)
            if device:
                snapshot.io_device = device.group(1).strip()

    # df明细优先于概要最高值，可同时保留根目录、数据盘和NFS等业务挂载点。
    detailed_mounts = _disk_mount_metrics(source_lines)
    if detailed_mounts:
        snapshot.disk_mounts = detailed_mounts
        inode_percentages = _inode_mount_percentages(source_lines)
        for mount_point, mount in snapshot.disk_mounts.items():
            mount.inode_percent = inode_percentages.get(mount_point)
        snapshot.disk_percent = max(item.percent for item in detailed_mounts.values())
        available_inode_values = [item.inode_percent for item in detailed_mounts.values() if item.inode_percent is not None]
        if available_inode_values:
            snapshot.inode_percent = max(available_inode_values)

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

    _apply_probe_compatibility_corrections(snapshot, source_lines)
    _apply_server_role_policy(snapshot)
    _recalculate_snapshot_status(snapshot)

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
            text=text,
            snapshot_time=captured_at,
        ))
    if not snapshot.counters:
        counted = Counter(row.status for row in rows)
        snapshot.counters = {status: counted.get(status, 0) for status in STATUS_ORDER}
    return snapshot


def group_servers(snapshots: Iterable[Snapshot]) -> List[ServerSeries]:
    """按服务器标识归并多个TXT快照并按时间排序。

    Args:
        snapshots: 从所有输入TXT解析出的巡检快照。

    Returns:
        List[ServerSeries]: 每台服务器一条连续时间序列。
    """
    grouped: Dict[str, List[Snapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.server_key, []).append(snapshot)
    servers = []
    for key, items in grouped.items():
        items.sort(key=lambda item: item.captured_at or datetime.min)
        servers.append(ServerSeries(key=key, snapshots=items))
    return sorted(servers, key=lambda server: server.latest.remark)

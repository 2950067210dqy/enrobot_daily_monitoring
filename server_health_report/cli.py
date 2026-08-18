from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from .ai import evaluate_groups
from .kg_exporter import export_operations_history
from .logs import assess_with_rules, group_logs
from .models import ServerSeries
from .parser import group_servers, parse_snapshot
from .pdf_renderer import create_pdf
from .runtime_log import configure_runtime_log, logger


def resolve_reports(arguments: List[str]) -> List[Path]:
    """解析文件、目录和通配符形式的巡检TXT输入。

    Args:
        arguments: 命令行提供的文件、目录或通配符列表。

    Returns:
        List[Path]: 去重后保持输入顺序的TXT路径。
    """
    paths: List[Path] = []
    for argument in arguments:
        candidate = Path(argument)
        if candidate.is_dir():
            paths.extend(sorted(candidate.glob("*.txt")))
        elif any(char in argument for char in "*?[]"):
            paths.extend(Path(item) for item in sorted(glob.glob(argument)))
        else:
            paths.append(candidate)
    unique = []
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def project_root() -> Path:
    """返回入口脚本所在项目目录，不受命令执行时的当前目录影响。"""
    return Path(__file__).resolve().parent.parent


def next_available_output(path: Path) -> Path:
    """原PDF被阅读器占用时，返回同目录下未使用的递增文件名。"""
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{date.today():%Y%m%d}_{os.getpid()}{path.suffix}")


def default_daily_paths(day_text: str) -> Tuple[List[Path], List[Path]]:
    """查找三类服务器在指定日期下的默认巡检TXT。

    Args:
        day_text: 目标报告日期，格式为YYYY-MM-DD。

    Returns:
        Tuple[List[Path], List[Path]]: 找到的TXT和没有TXT的默认目录。
    """
    root = project_root()
    daily_directories = [
        root / "txt" / "client" / day_text,
        root / "txt" / "interface_mail_1" / day_text,
        root / "txt" / "interface_mail_2" / day_text,
    ]
    paths: List[Path] = []
    unavailable: List[Path] = []
    for directory in daily_directories:
        directory.mkdir(parents=True, exist_ok=True)
        found = sorted(directory.glob("*.txt"))
        if found:
            paths.extend(found)
        else:
            unavailable.append(directory)
    return resolve_reports([str(path) for path in paths]), unavailable


def parse_args() -> argparse.Namespace:
    """定义并解析巡检报告命令行参数。

    Returns:
        argparse.Namespace: 输入路径、日期、AI模式和输出配置。
    """
    parser = argparse.ArgumentParser(description="将一个或多个时点的服务器巡检 TXT 汇总为趋势 PDF。")
    parser.add_argument("reports", nargs="*", help="TXT 文件、目录或通配符；省略时读取当天三个默认目录。")
    parser.add_argument("-o", "--output", type=Path, help="输出 PDF；默认模式写入 result/YYYY-MM-DD/服务器巡检报告_YYYY_MM_DD.pdf。")
    parser.add_argument("--date", help="默认目录日期，格式 YYYY-MM-DD；省略时使用本机当天日期。")
    parser.add_argument("--ai", choices=("auto", "required", "off"), default="auto", help="auto：有密钥时调用 AI；required：AI 失败则终止；off：只做规则预分类。")
    parser.add_argument("--max-log-groups", type=int, default=300, help="最多展示的唯一日志组；0 表示全部。")
    return parser.parse_args()


def parse_report_series(paths: List[Path], report_date: date) -> List[ServerSeries]:
    """解析全部资源快照，但每台服务器只从最后一次TXT提取日志。

    监控Shell输出从当天零点累计到当前执行时间，最后一次巡检已经包含此前日志。
    因此历史TXT仅用于CPU、内存、磁盘、IO、进程和目录趋势，避免累计日志重复进入去重和AI。

    Args:
        paths: 本次报告输入的全部巡检TXT路径。
        report_date: 允许进入报告和DeepSeek分析的目标日志日期。

    Returns:
        List[ServerSeries]: 按服务器归并的时间序列，且只有最新快照包含日志。
    """
    snapshots = [
        parse_snapshot(path, report_date=report_date, parse_logs=False)
        for path in paths
    ]
    servers = group_servers(snapshots)
    for server in servers:
        latest_path = server.latest.path
        latest_snapshot = parse_snapshot(latest_path, report_date=report_date, parse_logs=True)
        # 用重新解析的最新快照替换原对象，使后续去重只看到最后一次累计日志。
        server.snapshots[-1] = latest_snapshot
        logger.info(
            "服务器={}仅从最新TXT提取日志：时间={}，文件={}，日志条数={}",
            latest_snapshot.remark,
            latest_snapshot.captured_text,
            latest_path.name,
            len(latest_snapshot.raw_logs),
        )
    return servers


def main() -> int:
    """编排TXT解析、日志评估、PDF及知识图谱履历生成流程。

    Returns:
        int: 0表示成功，2表示输入错误，3表示强制AI评估失败。
    """
    args = parse_args()
    day_text = args.date or date.today().isoformat()
    log_path = configure_runtime_log(project_root(), day_text)
    logger.info("开始生成服务器巡检PDF：日期={}，AI模式={}，最大日志组={}", day_text, args.ai, args.max_log_groups)
    if args.date:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            print("--date 必须使用 YYYY-MM-DD 格式。", file=sys.stderr)
            return 2
    if args.reports:
        logger.info("输入模式：命令行指定文件/目录，共 {} 个参数", len(args.reports))
        paths = resolve_reports(args.reports)
        unavailable: List[Path] = []
        output = args.output or (Path.cwd() / "服务器巡检报告.pdf")
    else:
        logger.info("输入模式：读取当天三个默认TXT目录")
        paths, unavailable = default_daily_paths(day_text)
        default_output_directory = project_root() / "result" / day_text
        default_output_directory.mkdir(parents=True, exist_ok=True)
        output_date = day_text.replace("-", "_")
        output = args.output or (default_output_directory / f"服务器巡检报告_{output_date}.pdf")
        for directory in unavailable:
            logger.warning("默认目录没有TXT，已跳过：{}", directory)
            print("提示：默认目录没有 TXT，已跳过：" + str(directory), file=sys.stderr)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing or not paths:
        searched = args.reports or [
            str(project_root() / "txt" / role / day_text)
            for role in ("client", "interface_mail_1", "interface_mail_2")
        ]
        print("未找到可读的巡检 TXT：" + "，".join(missing or searched), file=sys.stderr)
        return 2
    if args.max_log_groups < 0:
        print("--max-log-groups 不能小于 0。", file=sys.stderr)
        return 2
    report_date = date.fromisoformat(day_text)
    logger.info("开始解析 {} 份TXT；每台服务器仅解析最新TXT日志；日志日期保险={}", len(paths), report_date.isoformat())
    servers = parse_report_series(paths, report_date)
    logger.info("TXT解析完成：识别 {} 台服务器", len(servers))
    groups = []
    for server in servers:
        logger.info("基础日志去重和规则预分类：服务器={}，采样数={}", server.latest.remark, len(server.snapshots))
        server.log_groups = group_logs(server)
        assess_with_rules(server.log_groups)
        groups.extend(server.log_groups)
        logger.info("服务器={}，基础去重后日志组={}", server.latest.remark, len(server.log_groups))
    try:
        logger.info("开始DeepSeek日志语义去重、分类、建议和操作分析")
        ai_status = evaluate_groups(groups, servers=servers, mode=args.ai)
    except Exception as exc:
        logger.exception("AI日志分析失败：{}", exc)
        if args.ai == "required":
            print("AI 日志评估失败：" + str(exc), file=sys.stderr)
            return 3
        ai_status = "AI 调用失败，使用规则预分类：" + str(exc)[:100]
    for server in servers:
        server.log_groups = [group for group in groups if group.server_key == server.latest.remark]
    logger.info("开始生成PDF：{}", output.resolve())
    try:
        chart_paths = create_pdf(servers, output, ai_status, args.max_log_groups)
    except PermissionError as exc:
        occupied_output = output
        output = next_available_output(occupied_output)
        logger.warning("目标PDF无写入权限或正被占用：{}；改为创建新PDF：{}；错误={}", occupied_output.resolve(), output.resolve(), exc)
        chart_paths = create_pdf(servers, output, ai_status, args.max_log_groups)
    chart_directory = chart_paths[0].parent.resolve() if chart_paths else output.parent.resolve()
    logger.info("图表PNG生成完成：目录={}，图片数量={}", chart_directory, len(chart_paths))
    logger.info("PDF生成完成：文件={}，TXT={}，服务器={}，唯一日志组={}", output.resolve(), len(paths), len(servers), len(groups))
    history_output = output.with_name(f"{output.stem}_运维执行履历.json")
    logger.info("开始生成知识图谱运维执行履历：{}", history_output.resolve())
    try:
        export_operations_history(servers, paths, output, history_output, day_text, ai_status)
    except PermissionError as exc:
        occupied_history = history_output
        history_output = next_available_output(occupied_history)
        logger.warning("目标履历文件无写入权限或正被占用：{}；改为创建新文件：{}；错误={}", occupied_history.resolve(), history_output.resolve(), exc)
        export_operations_history(servers, paths, output, history_output, day_text, ai_status)
    logger.info("知识图谱运维执行履历生成完成：{}", history_output.resolve())
    print(f"已汇总 {len(paths)} 份 TXT、{len(servers)} 台服务器、{len(groups)} 条唯一异常日志：{output.resolve()}")
    print(f"图表图片目录：{chart_directory}（{len(chart_paths)} 张PNG）")
    print("知识图谱运维执行履历：" + str(history_output.resolve()))
    print("日志评估状态：" + ai_status)
    print("执行日志：" + str(log_path.resolve()))
    return 0

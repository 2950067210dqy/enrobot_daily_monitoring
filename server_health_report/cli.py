from __future__ import annotations

import argparse
import glob
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from .ai import evaluate_groups
from .logs import assess_with_rules, group_logs
from .parser import group_servers, parse_snapshot
from .pdf_renderer import create_pdf


def resolve_reports(arguments: List[str]) -> List[Path]:
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


def default_daily_paths(day_text: str) -> Tuple[List[Path], List[Path]]:
    root = project_root()
    daily_directories = [
        root / "txt" / "client" / day_text,
        root / "txt" / "interface_mail_1" / day_text,
        root / "txt" / "interface_mail_2" / day_text,
    ]
    paths: List[Path] = []
    unavailable: List[Path] = []
    for directory in daily_directories:
        if directory.is_dir():
            found = sorted(directory.glob("*.txt"))
            if found:
                paths.extend(found)
            else:
                unavailable.append(directory)
        else:
            unavailable.append(directory)
    return resolve_reports([str(path) for path in paths]), unavailable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将一个或多个时点的服务器巡检 TXT 汇总为趋势 PDF。")
    parser.add_argument("reports", nargs="*", help="TXT 文件、目录或通配符；省略时读取当天三个默认目录。")
    parser.add_argument("-o", "--output", type=Path, help="输出 PDF；默认模式写入 result/YYYY-MM-DD/服务器巡检报告.pdf。")
    parser.add_argument("--date", help="默认目录日期，格式 YYYY-MM-DD；省略时使用本机当天日期。")
    parser.add_argument("--ai", choices=("auto", "required", "off"), default="auto", help="auto：有密钥时调用 AI；required：AI 失败则终止；off：只做规则预分类。")
    parser.add_argument("--ai-cache", type=Path, default=Path(".server_health_ai_cache.json"), help="AI 评估缓存文件。")
    parser.add_argument("--max-log-groups", type=int, default=300, help="最多展示的去重日志组；0 表示全部。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    day_text = args.date or date.today().isoformat()
    if args.date:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            print("--date 必须使用 YYYY-MM-DD 格式。", file=sys.stderr)
            return 2
    if args.reports:
        paths = resolve_reports(args.reports)
        unavailable: List[Path] = []
        output = args.output or (Path.cwd() / "服务器巡检报告.pdf")
    else:
        paths, unavailable = default_daily_paths(day_text)
        output = args.output or (project_root() / "result" / day_text / "服务器巡检报告.pdf")
        for directory in unavailable:
            print("提示：默认目录不存在或没有 TXT，已跳过：" + str(directory), file=sys.stderr)
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
    servers = group_servers([parse_snapshot(path) for path in paths])
    groups = []
    for server in servers:
        server.log_groups = group_logs(server)
        assess_with_rules(server.log_groups)
        groups.extend(server.log_groups)
    try:
        ai_status = evaluate_groups(groups, mode=args.ai, cache_path=args.ai_cache)
    except Exception as exc:
        if args.ai == "required":
            print("AI 日志评估失败：" + str(exc), file=sys.stderr)
            return 3
        ai_status = "AI 调用失败，使用规则预分类：" + str(exc)[:100]
    create_pdf(servers, output, ai_status, args.max_log_groups)
    print(f"已汇总 {len(paths)} 份 TXT、{len(servers)} 台服务器、{len(groups)} 个去重异常组：{output.resolve()}")
    print("日志评估状态：" + ai_status)
    return 0

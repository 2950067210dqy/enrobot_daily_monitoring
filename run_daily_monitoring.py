#!/usr/bin/env python3
"""下载三台服务器的每日巡检 TXT，生成 PDF，并通过 QQ 邮箱发送。"""

from __future__ import annotations

import argparse
import mimetypes
import os
import shutil
import smtplib
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional, Sequence

import server_health_report.secret as secret


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ScpSource:
    """一台远程服务器及其本地落盘位置。"""

    name: str
    endpoint: str
    remote_root: str
    local_role: str
    port: int = 22


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="下载当天三台服务器巡检 TXT，生成服务器巡检 PDF，并发送邮件。"
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="处理日期，格式 YYYY-MM-DD；默认使用本机当天日期。",
    )
    parser.add_argument(
        "--ai",
        choices=("auto", "required", "off"),
        default="auto",
        help="传给 server_health_pdf_report.py 的 AI 模式。",
    )
    parser.add_argument(
        "--max-log-groups",
        type=int,
        default=300,
        help="报告最多展示的唯一日志组数量；0 表示全部。",
    )
    parser.add_argument(
        "--skip-email",
        action="store_true",
        help="完成下载和报告生成后不发送邮件。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只展示下载位置和将执行的命令，不连接服务器、不生成报告、不发邮件。",
    )
    return parser.parse_args()


def config_text(name: str, default: str = "") -> str:
    return str(getattr(secret, name, default)).strip()


def config_port(name: str, default: int = 22) -> int:
    value = getattr(secret, name, default)
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("secret.py 中的 {} 必须是端口号".format(name))
    if not 1 <= port <= 65535:
        raise ValueError("secret.py 中的 {} 超出有效端口范围".format(name))
    return port


def load_sources() -> List[ScpSource]:
    sources = [
        ScpSource(
            "飞马1",
            config_text("scp_feima1"),
            "/daily_monitoring",
            "interface_mail_1",
            config_port("scp_port_feima1"),
        ),
        ScpSource(
            "飞马2",
            config_text("scp_feima2"),
            "/daily_monitoring",
            "interface_mail_2",
            config_port("scp_port_feima2"),
        ),
        ScpSource(
            "飞马client",
            config_text("scp_feima_client"),
            "/home/enrobot/daily_monitoring",
            "client",
            config_port("scp_port_feima_client"),
        ),
    ]
    missing = [source.name for source in sources if not source.endpoint]
    if missing:
        raise ValueError(
            "请先在 secret.py 中配置服务器用户和 IP：{}".format("、".join(missing))
        )
    invalid = [source.name for source in sources if "@" not in source.endpoint]
    if invalid:
        raise ValueError(
            "secret.py 中以下配置应使用 用户名@IP 格式：{}".format("、".join(invalid))
        )
    return sources


def identity_file() -> Optional[Path]:
    config_name = (
        "scp_system_identity_file"
        if os.environ.get("SERVER_HEALTH_SYSTEM_TASK") == "1"
        else "scp_identity_file"
    )
    configured = config_text(config_name)
    if not configured and config_name != "scp_identity_file":
        configured = config_text("scp_identity_file")
    if not configured:
        return None
    path = Path(configured).expanduser()
    if not path.is_file():
        raise FileNotFoundError("SSH 私钥不存在或当前账户不可读取：{}".format(path))
    return path


def find_scp_executable() -> str:
    """查找 scp；兼容虚拟环境或计划任务 PATH 不包含 OpenSSH 的情况。"""
    discovered = shutil.which("scp")
    if discovered:
        return discovered
    windows_root = os.environ.get("WINDIR", r"C:\Windows")
    # 32 位 Python 在 64 位 Windows 上访问 System32 时会被重定向；
    # Sysnative 是访问真实 64 位系统目录的官方别名。
    candidates = (
        Path(windows_root) / "Sysnative" / "OpenSSH" / "scp.exe",
        Path(windows_root) / "System32" / "OpenSSH" / "scp.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("未找到 scp 命令，请安装或启用 Windows OpenSSH Client")


def scp_command(
    scp_executable: str,
    source: ScpSource,
    day_text: str,
    destination: Path,
    key_path: Optional[Path],
) -> List[str]:
    command = [
        scp_executable,
        "-B",
        "-P",
        str(source.port),
        "-o",
        "ConnectTimeout=30",
    ]
    if key_path is not None:
        command.extend(["-i", str(key_path)])
        known_hosts = key_path.parent / "known_hosts"
        if known_hosts.is_file():
            command.extend(
                [
                    "-o",
                    "UserKnownHostsFile={}".format(known_hosts),
                    "-o",
                    "StrictHostKeyChecking=yes",
                ]
            )
    remote_pattern = "{}/{}/{}.txt".format(
        source.remote_root.rstrip("/"), day_text, "*"
    )
    command.extend(["{}:{}".format(source.endpoint, remote_pattern), str(destination)])
    return command


def printable_command(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def download_reports(day_text: str, dry_run: bool) -> None:
    sources = load_sources()
    key_path = identity_file()
    scp_executable = find_scp_executable()

    for source in sources:
        destination = PROJECT_ROOT / "txt" / source.local_role / day_text
        command = scp_command(
            scp_executable, source, day_text, destination, key_path
        )
        print("\n[下载] {} -> {}".format(source.name, destination))
        print(printable_command(command))
        if dry_run:
            continue
        destination.mkdir(parents=True, exist_ok=True)
        subprocess.run(command, cwd=str(PROJECT_ROOT), check=True)
        txt_files = sorted(destination.glob("*.txt"))
        if not txt_files:
            raise RuntimeError("{} 下载完成后未发现 TXT：{}".format(source.name, destination))
        print("[完成] {}：本地共有 {} 份 TXT".format(source.name, len(txt_files)))


def generate_report(
    day_text: str, ai_mode: str, max_log_groups: int, dry_run: bool
) -> List[Path]:
    report_script = PROJECT_ROOT / "server_health_pdf_report.py"
    command = [
        sys.executable,
        str(report_script),
        "--date",
        day_text,
        "--ai",
        ai_mode,
        "--max-log-groups",
        str(max_log_groups),
    ]
    print("\n[生成报告]")
    print(printable_command(command))
    if dry_run:
        return []

    subprocess.run(command, cwd=str(PROJECT_ROOT), check=True)
    result_directory = PROJECT_ROOT / "result" / day_text
    pdf_files = sorted(result_directory.glob("*.pdf"))
    if not pdf_files:
        raise RuntimeError("报告脚本执行成功，但目录中没有 PDF：{}".format(result_directory))
    return pdf_files


def send_pdfs(day_text: str, pdf_files: Sequence[Path]) -> None:
    sender = config_text("email_sender")
    recipient = config_text("email_recipient", "2950067210@qq.com")
    auth_code = config_text("email_auth_code")
    smtp_host = config_text("smtp_host", "smtp.qq.com")
    smtp_port = config_port("smtp_port", 465)
    missing = [
        name
        for name, value in (
            ("email_sender", sender),
            ("email_recipient", recipient),
            ("email_auth_code", auth_code),
        )
        if not value
    ]
    if missing:
        raise ValueError("请先在 secret.py 中配置：{}".format("、".join(missing)))

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "服务器巡检报告 {}".format(day_text)
    message.set_content(
        "{} 的服务器巡检已完成，邮件附带 {} 份 PDF。".format(day_text, len(pdf_files))
    )

    for pdf_path in pdf_files:
        mime_type, _ = mimetypes.guess_type(str(pdf_path))
        main_type, sub_type = (mime_type or "application/pdf").split("/", 1)
        with pdf_path.open("rb") as file_handle:
            message.add_attachment(
                file_handle.read(),
                maintype=main_type,
                subtype=sub_type,
                filename=pdf_path.name,
            )

    print(
        "\n[发送邮件] {} -> {}，附件：{}".format(
            sender, recipient, "、".join(path.name for path in pdf_files)
        )
    )
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60) as smtp:
        smtp.login(sender, auth_code)
        smtp.send_message(message)
    print("[完成] 邮件发送成功")


def main() -> int:
    args = parse_args()
    try:
        date.fromisoformat(args.date)
        if args.max_log_groups < 0:
            raise ValueError("--max-log-groups 不能小于 0")
        download_reports(args.date, args.dry_run)
        pdf_files = generate_report(
            args.date, args.ai, args.max_log_groups, args.dry_run
        )
        if args.dry_run:
            print("\n演练完成：没有连接服务器、生成报告或发送邮件。")
        elif args.skip_email:
            print("\n已跳过邮件发送。生成的 PDF：")
            for pdf_path in pdf_files:
                print(pdf_path)
        else:
            send_pdfs(args.date, pdf_files)
    except (ValueError, FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("\n执行失败：{}".format(exc), file=sys.stderr)
        return 1
    except (OSError, smtplib.SMTPException) as exc:
        print("\n网络或邮件操作失败：{}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

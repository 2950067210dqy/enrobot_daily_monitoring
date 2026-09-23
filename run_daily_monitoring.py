#!/usr/bin/env python3
"""下载三台服务器的每日巡检 TXT，生成 PDF，并通过 QQ 邮箱发送。"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import smtplib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

import server_health_report.secret as secret
import analyze_cache_log_accuracy as cache_log_accuracy
from server_health_report.runtime_log import configure_task_log, logger


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
        help="完成下载和报告生成后不发送邮件和飞书消息。",
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
        logger.info("[下载] {} -> {}", source.name, destination)
        logger.info("执行命令：{}", printable_command(command))
        if dry_run:
            continue
        destination.mkdir(parents=True, exist_ok=True)
        subprocess.run(command, cwd=str(PROJECT_ROOT), check=True)
        txt_files = sorted(destination.glob("*.txt"))
        if not txt_files:
            raise RuntimeError("{} 下载完成后未发现 TXT：{}".format(source.name, destination))
        logger.info("[完成] {}：本地共有 {} 份 TXT", source.name, len(txt_files))


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
    logger.info("[生成报告]")
    logger.info("执行命令：{}", printable_command(command))
    if dry_run:
        return []

    subprocess.run(command, cwd=str(PROJECT_ROOT), check=True)
    result_directory = PROJECT_ROOT / "result" / day_text
    pdf_files = sorted(result_directory.glob("*.pdf"))
    if not pdf_files:
        raise RuntimeError("报告脚本执行成功，但目录中没有 PDF：{}".format(result_directory))
    return pdf_files


def _short_text(value: Any, limit: int = 220) -> str:
    """压缩邮件摘要字段，避免日志样例令正文过长。"""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_abnormal_summary(day_text: str) -> Tuple[str, int]:
    """从报告的运维履历 JSON 汇总异常指标、日志和建议。"""
    result_directory = PROJECT_ROOT / "result" / day_text
    history_files = sorted(
        result_directory.glob("*运维执行履历.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not history_files:
        return "异常摘要：未找到本次运维执行履历 JSON，请查看 PDF。", 0

    try:
        document = json.loads(history_files[0].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "异常摘要：运维执行履历读取失败（{}），请查看 PDF。".format(exc), 0

    entities = document.get("entities", [])
    relations = document.get("relations", [])
    entity_by_id: Dict[str, Dict[str, Any]] = {
        str(entity.get("id")): entity
        for entity in entities
        if entity.get("id")
    }
    servers = {
        entity_id: entity.get("properties", {})
        for entity_id, entity in entity_by_id.items()
        if entity.get("type") == "Server"
    }
    subject_server: Dict[str, str] = {}
    action_by_subject: Dict[str, Dict[str, Any]] = {}
    for relation in relations:
        relation_type = relation.get("type")
        source = str(relation.get("source", ""))
        target = str(relation.get("target", ""))
        if relation_type in ("ASSESSES_SERVER", "OBSERVED_ON"):
            subject_server[source] = target
        elif relation_type == "PROPOSES_ACTION":
            action = entity_by_id.get(target, {})
            action_by_subject[source] = action.get("properties", {})

    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    severity_label = {
        "critical": "严重",
        "high": "高",
        "medium": "中",
        "low": "低",
    }
    metrics: List[Dict[str, Any]] = []
    logs: List[Dict[str, Any]] = []
    for entity in entities:
        entity_type = entity.get("type")
        properties = entity.get("properties", {})
        severity = str(properties.get("severity", "low")).lower()
        item = {
            "id": str(entity.get("id", "")),
            "properties": properties,
            "severity": severity,
            "rank": severity_rank.get(severity, 0),
        }
        if entity_type == "ResourceAssessment" and (
            properties.get("needs_fix") or item["rank"] >= severity_rank["medium"]
        ):
            metrics.append(item)
        elif entity_type == "LogEvent":
            logs.append(item)

    metrics.sort(key=lambda item: (item["rank"], bool(item["properties"].get("needs_fix"))), reverse=True)
    logs.sort(key=lambda item: (item["rank"], bool(item["properties"].get("needs_fix"))), reverse=True)
    total_count = len(metrics) + len(logs)
    lines = [
        "服务器异常监控摘要",
        "报告日期：{}；生成时间：{}".format(
            day_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ),
        "异常指标：{} 项；异常日志：{} 项。".format(len(metrics), len(logs)),
    ]

    selected = [("指标", item) for item in metrics[:12]]
    selected.extend(("日志", item) for item in logs[:10])
    if not selected:
        lines.append("本次未识别到需要处理的异常指标或异常日志。")
        return "\n".join(lines), total_count

    current_server = None
    for item_type, item in selected:
        properties = item["properties"]
        server_id = subject_server.get(item["id"], "")
        server_properties = servers.get(server_id, {})
        server_name = str(
            server_properties.get("remark")
            or server_properties.get("hostname")
            or "未知服务器"
        )
        if server_name != current_server:
            lines.extend(["", "【{}】".format(server_name)])
            current_server = server_name
        level = severity_label.get(item["severity"], item["severity"] or "未知")
        if item_type == "指标":
            name = properties.get("metric") or properties.get("title") or "资源指标"
            lines.append(
                "- 指标[{}] {}：{}；{}".format(
                    level,
                    _short_text(name, 40),
                    _short_text(properties.get("title"), 100),
                    _short_text(properties.get("judgment")),
                )
            )
        else:
            lines.append(
                "- 日志[{}] {}：{}".format(
                    level,
                    _short_text(properties.get("title") or properties.get("category"), 100),
                    _short_text(properties.get("judgment")),
                )
            )
            sample = _short_text(properties.get("sample"), 260)
            if sample:
                lines.append("  样例：{}".format(sample))
        action = action_by_subject.get(item["id"], {})
        recommendation = _short_text(action.get("recommendation"), 220)
        if recommendation:
            lines.append("  建议：{}".format(recommendation))

    omitted_metrics = max(0, len(metrics) - 12)
    omitted_logs = max(0, len(logs) - 10)
    if omitted_metrics or omitted_logs:
        lines.extend(
            [
                "",
                "其余 {} 项异常指标、{} 项异常日志请查看附件 PDF。".format(
                    omitted_metrics, omitted_logs
                ),
            ]
        )
    return "\n".join(lines), total_count


def send_pdfs(
    day_text: str,
    pdf_files: Sequence[Path],
    abnormal_summary: str,
    abnormal_count: int,
) -> None:
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
    message["Subject"] = "[异常{}项] 服务器巡检报告 {} {}".format(
        abnormal_count, day_text, datetime.now().strftime("%H:%M")
    )
    message.set_content(
        "{}\n\n本邮件附带 {} 份服务器巡检 PDF，详细趋势、日志分析和建议请查看附件。".format(
            abnormal_summary, len(pdf_files)
        )
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

    logger.info(
        "[发送邮件] {} -> {}，附件：{}",
        sender,
        recipient,
        "、".join(path.name for path in pdf_files),
    )
    logger.info("{}", abnormal_summary)
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60) as smtp:
        smtp.login(sender, auth_code)
        smtp.send_message(message)
    logger.info("[完成] 邮件发送成功")


def build_field_accuracy_markdown(accuracy_result: Dict[str, Any]) -> str:
    """将字段识别准确率转换为飞书 Markdown 表格。"""
    lines = [
        "## 字段识别准确率统计",
        "",
        "| 字段 | 准确率 | 总数 | 匹配 | AI空 | 人工空 | 不匹配 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for field_result in accuracy_result.get("fields", []):
        field_name = str(field_result.get("field", "")).replace("|", "\\|")
        lines.append(
            "| {} | {:.2f}% | {} | {} | {} | {} | {} |".format(
                field_name,
                float(field_result.get("accuracy", 0)),
                field_result.get("total", 0),
                field_result.get("match", 0),
                field_result.get("ai_empty", 0),
                field_result.get("man_empty", 0),
                field_result.get("mismatch", 0),
            )
        )

    overall = accuracy_result.get("overall", {})
    lines.append(
        "| **总体** | **{:.2f}%** | **{}** | **{}** | **{}** | **{}** | **{}** |".format(
            float(overall.get("accuracy", 0)),
            overall.get("total", 0),
            overall.get("match", 0),
            overall.get("ai_empty", 0),
            overall.get("man_empty", 0),
            overall.get("mismatch", 0),
        )
    )
    return "\n".join(lines)


def build_feishu_markdown_payload(message_text: str) -> bytes:
    """构造飞书 Markdown 卡片请求体，不执行网络请求。"""
    return json.dumps(
        {
            "msg_type": "interactive",
            "card": {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": message_text}
                    ]
                },
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


def send_feishu_summary(
    day_text: str,
    abnormal_summary: str,
    abnormal_count: int,
    field_accuracy_markdown: str,
) -> None:
    """将与邮件一致的简要异常摘要推送给飞书自定义机器人。"""
    hook_url = config_text("feishu_hook_url")
    if not hook_url:
        raise ValueError("请先在 server_health_report/secret.py 中配置 feishu_hook_url")
    brief_summary = build_feishu_brief(abnormal_summary)
    message_text = "**【服务器巡检与字段识别统计】{} {}｜异常{}项**\n\n{}".format(
        day_text,
        datetime.now().strftime("%H:%M"),
        abnormal_count,
        brief_summary,
    )
    if field_accuracy_markdown:
        message_text = "{}\n\n---\n\n{}".format(
            message_text, field_accuracy_markdown
        )
    payload = build_feishu_markdown_payload(message_text)
    logger.info("[飞书推送] 正在发送 {} 的异常摘要（{} 项）", day_text, abnormal_count)
    logger.info("{}", message_text)

    request = urlrequest.Request(
        hook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=30) as response:
            response_status = int(getattr(response, "status", 200))
            response_text = response.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            "飞书机器人 HTTP {}：{}".format(exc.code, _short_text(response_text, 300))
        )
    except (urlerror.URLError, OSError) as exc:
        logger.warning(
            "[飞书推送] Python HTTPS 失败，自动切换系统 curl：{}".format(
                _short_text(getattr(exc, "reason", exc), 180)
            )
        )
        response_status, response_text = _post_feishu_with_curl(hook_url, payload)

    if not 200 <= response_status < 300:
        raise RuntimeError(
            "飞书机器人 HTTP {}：{}".format(
                response_status, _short_text(response_text, 300)
            )
        )
    if response_text.strip():
        try:
            response_data = json.loads(response_text)
        except ValueError:
            response_data = {}
        status_code = response_data.get("StatusCode")
        code = response_data.get("code")
        if status_code not in (None, 0) or code not in (None, 0):
            raise RuntimeError(
                "飞书机器人拒绝消息：{}".format(_short_text(response_text, 300))
            )
    logger.info("[完成] 飞书异常摘要推送成功")


def build_feishu_brief(abnormal_summary: str) -> str:
    """压缩邮件摘要供飞书展示，去除建议和重复说明，保留日志样例。"""
    brief_lines: List[str] = []
    for line in abnormal_summary.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "服务器异常监控摘要" or stripped.startswith("报告日期："):
            continue
        if stripped.startswith("建议："):
            continue
        if stripped.startswith("异常指标："):
            brief_lines.append(
                stripped.replace("异常指标：", "指标：").replace("异常日志：", "日志：")
            )
            continue
        if stripped.startswith("【") and stripped.endswith("】"):
            if brief_lines:
                brief_lines.append("")
            brief_lines.append("**{}**".format(stripped))
            brief_lines.append("")
            continue
        if stripped.startswith("- 指标"):
            stripped = "- " + stripped[len("- 指标") :]
        elif stripped.startswith("- 日志"):
            stripped = "- " + stripped[len("- 日志") :]
        brief_lines.append(stripped)
    return "\n".join(brief_lines)


def _find_curl_executable() -> str:
    discovered = shutil.which("curl.exe") or shutil.which("curl")
    if discovered:
        return discovered
    windows_root = os.environ.get("WINDIR", r"C:\Windows")
    candidates = (
        Path(windows_root) / "Sysnative" / "curl.exe",
        Path(windows_root) / "System32" / "curl.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("Python HTTPS 失败，且未找到系统 curl.exe 作为飞书备用通道")


def _post_feishu_with_curl(hook_url: str, payload: bytes) -> Tuple[int, str]:
    """使用 curl 备用发送，且不把 Webhook URL 暴露在进程命令行中。"""
    if any(character in hook_url for character in ('"', "\r", "\n")):
        raise ValueError("feishu_hook_url 包含无效字符")
    curl_executable = _find_curl_executable()
    temporary_directory = PROJECT_ROOT / "logs"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    payload_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="feishu_payload_",
            suffix=".json",
            dir=str(temporary_directory),
            delete=False,
        ) as payload_file:
            payload_file.write(payload)
            payload_path = Path(payload_file.name)
        curl_config = "\n".join(
            [
                'url = "{}"'.format(hook_url),
                'request = "POST"',
                'header = "Content-Type: application/json; charset=utf-8"',
                'data-binary = "@{}"'.format(str(payload_path).replace("\\", "/")),
                "silent",
                "show-error",
                "max-time = 35",
                'write-out = "\\n%{http_code}"',
            ]
        )
        completed = subprocess.run(
            [curl_executable, "--config", "-"],
            input=curl_config.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            timeout=45,
            check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise RuntimeError(
                "飞书 curl 连接失败（退出码 {}）：{}".format(
                    completed.returncode, _short_text(stderr or stdout, 300)
                )
            )
        response_text, separator, status_text = stdout.rpartition("\n")
        if not separator or not status_text.strip().isdigit():
            raise RuntimeError("飞书 curl 返回格式异常：{}".format(_short_text(stdout, 300)))
        return int(status_text.strip()), response_text
    except subprocess.TimeoutExpired:
        raise RuntimeError("飞书 curl 连接超时（45秒）")
    finally:
        if payload_path is not None:
            try:
                payload_path.unlink()
            except OSError:
                pass


def send_notifications(
    day_text: str,
    pdf_files: Sequence[Path],
    field_accuracy_markdown: str,
) -> None:
    """独立尝试邮件和飞书两个渠道，任一失败都会保留另一渠道的发送机会。"""
    abnormal_summary, abnormal_count = build_abnormal_summary(day_text)
    errors: List[str] = []
    try:
        send_pdfs(day_text, pdf_files, abnormal_summary, abnormal_count)
    except Exception as exc:
        errors.append("邮件发送失败：{}".format(exc))
        logger.error("{}", errors[-1])
    try:
        send_feishu_summary(
            day_text,
            abnormal_summary,
            abnormal_count,
            field_accuracy_markdown,
        )
    except Exception as exc:
        errors.append("飞书推送失败：{}".format(exc))
        logger.error("{}", errors[-1])
    if errors:
        raise RuntimeError("；".join(errors))


def main() -> int:
    args = parse_args()
    try:
        log_day = date.fromisoformat(args.date).isoformat()
    except ValueError:
        log_day = date.today().isoformat()
    configure_task_log(PROJECT_ROOT, log_day, "run_daily_monitoring")
    logger.info(
        "开始每日巡检：日期={}，AI模式={}，最大日志组={}，演练={}，跳过通知={}",
        args.date,
        args.ai,
        args.max_log_groups,
        args.dry_run,
        args.skip_email,
    )
    try:
        date.fromisoformat(args.date)
        if args.max_log_groups < 0:
            raise ValueError("--max-log-groups 不能小于 0")
        download_reports(args.date, args.dry_run)
        pdf_files = generate_report(
            args.date, args.ai, args.max_log_groups, args.dry_run
        )
        field_accuracy_markdown = ""
        if not args.dry_run:
            end_date = (date.fromisoformat(args.date) + timedelta(days=1)).isoformat()
            logger.info("[字段识别准确率统计]")
            accuracy_result = cache_log_accuracy.analyze_accuracy(args.date, end_date)
            field_accuracy_markdown = build_field_accuracy_markdown(accuracy_result)
            logger.info("{}", field_accuracy_markdown)
        if args.dry_run:
            logger.info("演练完成：没有连接服务器、生成报告、发送邮件或推送飞书消息。")
        elif args.skip_email:
            logger.info("已跳过邮件和飞书推送。生成的 PDF：")
            for pdf_path in pdf_files:
                logger.info("{}", pdf_path)
        else:
            send_notifications(args.date, pdf_files, field_accuracy_markdown)
    except (ValueError, FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        logger.error("执行失败：{}", exc)
        return 1
    except (OSError, smtplib.SMTPException) as exc:
        logger.error("网络或邮件操作失败：{}", exc)
        return 1
    logger.info("每日巡检流程执行完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

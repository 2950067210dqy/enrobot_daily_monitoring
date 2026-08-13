from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

deepseek_logger = logger.bind(log_channel="deepseek_response")


def configure_runtime_log(root: Path, day_text: str) -> Path:
    """为一次巡检报告任务配置普通日志和DeepSeek响应日志。

    Args:
        root: 监控项目根目录。
        day_text: 报告业务日期，格式为YYYY-MM-DD。

    Returns:
        Path: 本次普通执行日志文件路径。
    """
    log_directory = root / "logs" / day_text
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"server_health_pdf_{datetime.now():%Y%m%d_%H%M%S}.log"
    deepseek_log_path = log_directory / f"deepseek_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger.remove()
    exclude_deepseek_response = lambda record: record["extra"].get("log_channel") != "deepseek_response"
    logger.add(
        sys.stderr, level="INFO", colorize=False,
        filter=exclude_deepseek_response,
        format="{time:HH:mm:ss} | {level:<8} | {message}",
    )
    logger.add(
        str(log_path),
        level="DEBUG",
        filter=exclude_deepseek_response,
        encoding="utf-8",
        rotation="20 MB",
        retention="30 days",
        enqueue=False,
        backtrace=True,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}",
    )
    logger.add(
        str(deepseek_log_path),
        level="DEBUG",
        encoding="utf-8",
        rotation="50 MB",
        retention="30 days",
        enqueue=False,
        backtrace=False,
        diagnose=False,
        filter=lambda record: record["extra"].get("log_channel") == "deepseek_response",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}",
    )
    logger.info("执行日志已初始化：{}", log_path)
    logger.info("DeepSeek响应日志已初始化：{}", deepseek_log_path)
    return log_path


__all__ = ["configure_runtime_log", "logger", "deepseek_logger"]

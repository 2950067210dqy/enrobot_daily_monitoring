#!/usr/bin/env python3
"""服务器巡检报告兼容入口；实现已拆分至 server_health_report 包。"""

from __future__ import annotations

import sys

try:
    from server_health_report.cli import main
except (ImportError, SyntaxError) as exc:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    raise SystemExit(
        f"依赖缺失或与 Python {version} 不兼容。请执行："
        "python -m pip install --force-reinstall -r requirements-server-health-pdf.txt"
    ) from exc


if __name__ == "__main__":
    raise SystemExit(main())

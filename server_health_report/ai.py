from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import LogGroup


AI_URL = "https://api.siliconflow.cn/v1/chat/completions"
AI_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def build_prompt() -> str:
    return """你是生产环境 SRE 日志审查模型。请只依据输入的去重日志组评估异常重要程度，不猜测未提供的事实。
对每组返回：
- severity：critical/high/medium/low/info 之一；
- immediate：是否需要立即处理；
- needs_fix：是否需要修复或调整；
- title：不超过20个中文字符的异常名称；
- reason：不超过80个中文字符，只说明判断依据，不给操作步骤或修复建议。
判断口径：正在发生的服务不可用、OOM、磁盘写满、崩溃循环、数据损坏或明显安全事故为 critical；高概率影响服务且持续重复为 high；需要排期处理但没有即时中断证据为 medium；低影响或偶发噪声为 low；仅信息线索为 info。历史日志不能仅因措辞严重就判断为当前故障，需结合 occurrence_count、first_seen、last_seen。
必须返回严格 JSON 对象：{"assessments":[{"id":"...","severity":"...","immediate":true,"needs_fix":true,"title":"...","reason":"..."}]}。不得返回 Markdown。
输入日志组：
"""


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def gen_sign_headers(headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    result = dict(headers or {})
    timestamp = str(int(time.time() * 1000))
    sha = hashlib.sha256(("en-robot" + timestamp).encode("utf-8")).hexdigest()
    key = hashlib.md5((sha + timestamp).encode("utf-8")).hexdigest()
    result["sign-ts"] = _b64(timestamp)
    result["sign-key"] = _b64(key)
    return result


def _payload(groups: Iterable[LogGroup]) -> List[dict]:
    return [
        {
            "id": item.group_id,
            "category": item.category,
            "occurrence_count": item.occurrences,
            "first_seen": item.first_seen.isoformat() if item.first_seen else None,
            "last_seen": item.last_seen.isoformat() if item.last_seen else None,
            "sample": item.sample[:1200],
        }
        for item in groups
    ]


def _load_cache(path: Optional[Path]) -> Dict[str, dict]:
    if not path or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _save_cache(path: Optional[Path], cache: Dict[str, dict]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply(group: LogGroup, assessment: dict, source: str) -> None:
    severity = assessment.get("severity", "unassessed")
    if severity not in {"critical", "high", "medium", "low", "info"}:
        return
    group.severity = severity
    group.immediate = bool(assessment.get("immediate"))
    group.needs_fix = bool(assessment.get("needs_fix"))
    group.title = str(assessment.get("title") or "AI异常评估")[:60]
    group.reason = str(assessment.get("reason") or "")[:240]
    group.assessment_source = source


def evaluate_groups(
    groups: List[LogGroup],
    mode: str = "auto",
    api_key: Optional[str] = None,
    cache_path: Optional[Path] = None,
    batch_size: int = 20,
) -> str:
    if mode == "off":
        return "AI 已关闭"
    key = api_key or os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not key:
        if mode == "required":
            raise RuntimeError("未设置 SILICONFLOW_API_KEY，无法执行必需的 AI 日志评估。")
        return "未配置 SILICONFLOW_API_KEY，使用规则预分类"
    cache = _load_cache(cache_path)
    pending = []
    for group in groups:
        cached = cache.get(group.group_id)
        if cached:
            _apply(group, cached, "AI缓存")
        else:
            pending.append(group)

    for start in range(0, len(pending), max(1, batch_size)):
        batch = pending[start:start + max(1, batch_size)]
        content = build_prompt() + json.dumps(_payload(batch), ensure_ascii=False)
        data = {
            "model": AI_MODEL,
            "temperature": 0.1,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
        }
        headers = gen_sign_headers({
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + key,
        })
        request = urllib.request.Request(
            AI_URL,
            data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=150) as response:
                status = response.getcode()
                body = response.read().decode("utf-8", errors="replace")
            if status != 200:
                raise RuntimeError(f"SiliconFlow HTTP {status}: {body[:500]}")
            outer = json.loads(body)
            parsed = json.loads(outer["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"SiliconFlow HTTP {exc.code}: {body[:500]}") from exc
        by_id = {str(item.get("id")): item for item in parsed.get("assessments", [])}
        for group in batch:
            assessment = by_id.get(group.group_id)
            if assessment:
                _apply(group, assessment, "AI实时评估")
                cache[group.group_id] = assessment
    _save_cache(cache_path, cache)
    return "AI 实时/缓存评估完成"

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from .models import LogGroup, ResourceAssessment, ServerSeries
from .runtime_log import deepseek_logger, logger


AI_URL = "https://api.deepseek.com/chat/completions"
AI_MODEL = "deepseek-v4-flash"
VALID_CATEGORIES = {"应用日志目录日志", "Docker日志", "系统/进程日志"}
CATEGORY_TO_CODE = {"应用日志目录日志": "a", "Docker日志": "d", "系统/进程日志": "s"}
CODE_TO_CATEGORY = {value: key for key, value in CATEGORY_TO_CODE.items()}


def build_prompt() -> str:
    """读取外置的DeepSeek运维分析提示词。

    Returns:
        str: 已追加输入分隔标记的完整提示词。

    Raises:
        RuntimeError: Prompt文件缺失、不可读或内容为空。
    """
    prompt_path = Path(__file__).with_name("deepseek_prompt.txt")
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"无法读取DeepSeek Prompt文件：{prompt_path}") from exc
    if not prompt:
        raise RuntimeError(f"DeepSeek Prompt文件为空：{prompt_path}")
    return prompt + "\n输入："


def _payload_item(item: LogGroup) -> dict:
    """把日志组压缩为节省Token的DeepSeek输入对象。

    Args:
        item: 已基础去重的日志组。

    Returns:
        dict: 包含稳定ID、类别代码和日志样例的紧凑对象。
    """
    sample = re.sub(r"\s+", " ", item.sample).strip()[:800]
    return {
        "id": item.group_id,
        "c": CATEGORY_TO_CODE.get(item.category, "s"),
        "t": sample,
    }


def _payload(groups: Iterable[LogGroup]) -> List[dict]:
    """批量构造DeepSeek日志输入数组。"""
    return [_payload_item(item) for item in groups]


def _metric_payload(server: Optional[ServerSeries]) -> dict:
    """提取一台服务器的资源指标时间序列。

    Args:
        server: 待分析服务器；为空时不发送资源指标。

    Returns:
        dict: CPU、内存、磁盘和IO的时间点数值。
    """
    if server is None:
        return {}
    result = {}
    attributes = {
        "cpu": "cpu_percent", "memory": "memory_percent", "disk": "disk_percent",
        "iowait": "io_iowait_percent", "util": "io_util_percent",
    }
    for metric, attribute in attributes.items():
        values = []
        for snapshot in server.snapshots:
            value = getattr(snapshot, attribute)
            if value is not None:
                values.append([
                    snapshot.captured_at.strftime("%m-%d %H:%M") if snapshot.captured_at else snapshot.captured_text,
                    round(float(value), 2),
                ])
        result[metric] = values
    return result


def _request_payload(groups: List[LogGroup], server: Optional[ServerSeries]) -> dict:
    """组装一次DeepSeek请求中的服务器、日志和指标数据。"""
    server_name = server.latest.remark if server else (groups[0].server_key if groups else "")
    payload = {"server": server_name, "logs": _payload(groups)}
    metrics = _metric_payload(server)
    if metrics:
        payload["metrics"] = metrics
    return payload


def _batches(groups: List[LogGroup], max_items: int, max_chars: int) -> Iterator[List[LogGroup]]:
    """按日志数量和字符预算切分AI请求，控制Token和请求失败风险。

    Args:
        groups: 同一服务器的日志组。
        max_items: 单批最大日志组数量。
        max_chars: 单批紧凑JSON最大估算字符数。

    Yields:
        List[LogGroup]: 可一次提交给DeepSeek的日志批次。
    """
    batch: List[LogGroup] = []
    batch_chars = 2
    for group in groups:
        item_chars = len(json.dumps(_payload_item(group), ensure_ascii=False)) + 1
        if batch and (len(batch) >= max_items or batch_chars + item_chars > max_chars):
            yield batch
            batch = []
            batch_chars = 2
        batch.append(group)
        batch_chars += item_chars
    if batch:
        yield batch


def _parse_json_content(content: str) -> dict:
    """解析 DeepSeek JSON；清理 Markdown 包裹和常见尾逗号，失败时交由重试。"""
    value = (content or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    start, end = value.find("{"), value.rfind("}")
    if start >= 0 and end > start:
        value = value[start:end + 1]
    value = re.sub(r",\s*([}\]])", r"\1", value)
    # DeepSeek 长 JSON 偶尔遗漏属性/数组元素之间的逗号。仅在解析器明确报告
    # “Expecting ',' delimiter”时，于报错位置插入逗号并重新解析。
    for _ in range(30):
        try:
            parsed = json.loads(value)
            break
        except json.JSONDecodeError as exc:
            if "Expecting ',' delimiter" not in exc.msg:
                raise
            before = value[:exc.pos].rstrip()
            after = value[exc.pos:].lstrip()
            if not before or not after or after[0] not in {'"', '{', '['}:
                raise
            logger.warning("DeepSeek JSON缺少逗号，尝试在 line={} column={} 自动补全", exc.lineno, exc.colno)
            value = before + "," + after
    else:
        raise ValueError("DeepSeek JSON缺少逗号过多，自动修复超过30次")
    assessments = parsed.get("log_assessments", parsed.get("a", parsed.get("assessments"))) if isinstance(parsed, dict) else None
    if not isinstance(assessments, list):
        raise ValueError("DeepSeek JSON 缺少 a 数组")
    parsed["assessments"] = assessments
    metric_assessments = parsed.get("metric_assessments", parsed.get("m", []))
    if not isinstance(metric_assessments, list):
        raise ValueError("DeepSeek JSON 的 m 不是数组")
    parsed["metric_assessments"] = metric_assessments
    return parsed


def _request_batch(
    batch: List[LogGroup], key: str, request_number: int,
    server: Optional[ServerSeries] = None,
) -> dict:
    """向DeepSeek提交一个服务器批次并解析结构化评估结果。

    Args:
        batch: 当前批次的去重日志组。
        key: DeepSeek API密钥。
        request_number: 本次任务内的请求序号。
        server: 首批请求携带的服务器资源时间序列。

    Returns:
        dict: 规范化后的日志和资源指标评估对象。
    """
    content = build_prompt() + json.dumps(_request_payload(batch, server), ensure_ascii=False, separators=(",", ":"))
    data = {
        "model": AI_MODEL,
        "temperature": 0.1,
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": "请严格输出json对象，所有属性名和字符串必须使用双引号。"}, {"role": "user", "content": content}],
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + key,
    }
    request = urllib.request.Request(
        AI_URL,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    logger.info("DeepSeek请求 #{}：服务器={}，日志组={}，携带资源指标={}，输入约 {} 字符", request_number, _request_payload(batch, server)["server"], len(batch), server is not None, len(content))
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            status = response.getcode()
            body = response.read().decode("utf-8", errors="replace")
        deepseek_logger.info(
            "请求编号={} | HTTP状态={} | 日志组数={} | 携带资源指标={} | 日志组ID={}\n{}",
            request_number, status, len(batch), server is not None, ",".join(group.group_id for group in batch), body,
        )
        if status != 200:
            raise RuntimeError(f"DeepSeek HTTP {status}: {body[:500]}")
        outer = json.loads(body)
        model_content = outer["choices"][0]["message"]["content"]
        parsed = _parse_json_content(model_content)
        usage = outer.get("usage") or {}
        logger.info(
            "DeepSeek请求 #{} token：输入={}，输出={}，合计={}，缓存命中={}",
            request_number,
            usage.get("prompt_tokens", "未知"),
            usage.get("completion_tokens", "未知"),
            usage.get("total_tokens", "未知"),
            usage.get("prompt_cache_hit_tokens", 0),
        )
        logger.info("DeepSeek请求 #{}：成功解析 {} 条评估结果", request_number, len(parsed.get("assessments", [])))
        return parsed
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        deepseek_logger.error(
            "请求编号={} | HTTP状态={} | 日志组数={} | 携带资源指标={} | 日志组ID={}\n{}",
            request_number, exc.code, len(batch), server is not None, ",".join(group.group_id for group in batch), body,
        )
        logger.error("DeepSeek请求 #{}：HTTP {}，响应摘要={}", request_number, exc.code, body[:500])
        raise RuntimeError(f"DeepSeek HTTP {exc.code}: {body[:500]}") from exc
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("DeepSeek请求 #{}：JSON解析失败：{}", request_number, exc)
        raise


def _apply(group: LogGroup, assessment: dict, source: str) -> None:
    """将单条AI日志评估安全写回业务日志组。"""
    severity = assessment.get("severity", assessment.get("v", "unassessed"))
    severity = {"c": "critical", "h": "high", "m": "medium", "l": "low", "i": "info"}.get(severity, severity)
    if severity not in {"critical", "high", "medium", "low", "info"}:
        return
    group.severity = severity
    group.immediate = bool(assessment.get("immediate", assessment.get("i")))
    group.needs_fix = bool(assessment.get("needs_fix", assessment.get("f")))
    group.title = str(assessment.get("title", assessment.get("t")) or "AI异常评估")[:60]
    group.reason = str(assessment.get("reason", assessment.get("r")) or "")[:240]
    category = str(assessment.get("category", assessment.get("c")) or "")
    category = {"application": "应用日志目录日志", "docker": "Docker日志", "system_process": "系统/进程日志"}.get(category, category)
    category = CODE_TO_CATEGORY.get(category, category)
    if category in VALID_CATEGORIES:
        group.category = category
    duplicate_of = assessment.get("duplicate_of", assessment.get("d"))
    group.duplicate_of = str(duplicate_of) if duplicate_of else None
    group.recommendation = str(assessment.get("recommendation", assessment.get("g")) or "")[:360]
    group.action_analysis = str(assessment.get("action_analysis", assessment.get("x")) or "")[:480]
    group.assessment_source = source


def _apply_resource(server: ServerSeries, assessments: List[dict]) -> None:
    """将AI资源评估写回服务器并为缺失结果生成规则兜底。"""
    metric_names = {"cpu": "CPU", "memory": "内存", "disk": "磁盘", "iowait": "IO iowait", "util": "IO util"}
    results = []
    for item in assessments:
        metric = str(item.get("metric", item.get("id")) or "")
        if metric not in metric_names:
            continue
        raw_severity = str(item.get("severity", item.get("v")) or "")
        severity = {"c": "critical", "h": "high", "m": "medium", "l": "low", "i": "info"}.get(raw_severity, raw_severity)
        if severity not in {"critical", "high", "medium", "low", "info"}:
            continue
        results.append(ResourceAssessment(
            metric=metric_names[metric], severity=severity,
            immediate=bool(item.get("immediate", item.get("i"))),
            needs_fix=bool(item.get("needs_fix", item.get("f"))),
            title=str(item.get("title", item.get("t")) or "资源指标评估")[:60],
            reason=str(item.get("reason", item.get("r")) or "")[:240],
            recommendation=str(item.get("recommendation", item.get("g")) or "")[:360],
            action_analysis=str(item.get("action_analysis", item.get("x")) or "")[:480],
            assessment_source="AI实时评估",
        ))
    server.resource_assessments = results


def _merge_ai_duplicates(groups: List[LogGroup]) -> None:
    """根据AI的duplicate_of关系移除语义重复从项。"""
    """按 AI 返回的主记录合并同服务器语义重复项，并保留时间范围和章节来源。"""
    by_id = {group.group_id: group for group in groups}
    removed = set()
    for group in groups:
        if not group.duplicate_of or group.duplicate_of == group.group_id:
            continue
        target = by_id.get(group.duplicate_of)
        if target is None or target.server_key != group.server_key:
            continue
        for section in group.sections:
            if section not in target.sections:
                target.sections.append(section)
        if group.first_seen and (target.first_seen is None or group.first_seen < target.first_seen):
            target.first_seen = group.first_seen
        if group.last_seen and (target.last_seen is None or group.last_seen > target.last_seen):
            target.last_seen = group.last_seen
        removed.add(group.group_id)
    groups[:] = [group for group in groups if group.group_id not in removed]


def _secret_api_key() -> str:
    """从secret模块或环境变量读取DeepSeek API密钥。"""
    try:
        from .secret import api_secret
        return str(api_secret).strip()
    except (ImportError, AttributeError):
        return ""


def evaluate_groups(
    groups: List[LogGroup],
    servers: Optional[List[ServerSeries]] = None,
    mode: str = "auto",
    api_key: Optional[str] = None,
    batch_size: int = 60,
    max_batch_chars: int = 55000,
) -> str:
    """按服务器集中完成日志语义去重和资源指标AI评估。

    Args:
        groups: 全部服务器的基础去重日志组。
        servers: 服务器时间序列，用于携带资源指标。
        mode: auto自动降级、required失败终止、off关闭AI。
        max_items: 单次请求最大日志组数量。
        max_chars: 单次请求最大输入字符预算。

    Returns:
        str: 供PDF和履历展示的AI执行状态说明。
    """
    if mode == "off":
        logger.info("AI模式=off，跳过DeepSeek，保留规则预分类")
        return "AI 已关闭"
    key = api_key or _secret_api_key() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        if mode == "required":
            raise RuntimeError("secret.py 未配置 api_secret，且未设置 DEEPSEEK_API_KEY。")
        return "未配置 DeepSeek API 密钥，使用规则预分类"
    pending = list(groups)
    logger.info("AI准备完成：日志组总数={}，不使用本地缓存，全部重新分析", len(groups))

    request_count = 0
    # 按服务器分别分批：服务器名每批只发送一次，也确保AI语义去重不会跨服务器。
    pending_by_server: Dict[str, List[LogGroup]] = {}
    for group in pending:
        pending_by_server.setdefault(group.server_key, []).append(group)
    server_by_name = {server.latest.remark: server for server in (servers or [])}
    initial_batches = []
    all_server_names = list(dict.fromkeys([*(server_by_name.keys()), *(pending_by_server.keys())]))
    for server_name in all_server_names:
        log_batches = list(_batches(pending_by_server.get(server_name, []), max(1, batch_size), max(6000, max_batch_chars)))
        if not log_batches:
            log_batches = [[]]
        for index, batch in enumerate(log_batches):
            initial_batches.append((batch, server_by_name.get(server_name) if index == 0 else None, server_name))

    def process_batch(batch: List[LogGroup], metric_server: Optional[ServerSeries], server_name: str) -> None:
        """执行单个AI批次，并在长JSON损坏时递归拆批重试。"""
        nonlocal request_count
        request_count += 1
        try:
            parsed = _request_batch(batch, key, request_count, metric_server)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # 本地自动修复仍无法解析时直接拆分，不再原样重发整批，减少无效请求。
            if len(batch) <= 1:
                if not batch:
                    logger.error("服务器 {} 的纯资源指标请求返回无效JSON，跳过资源AI评估", server_name)
                    return
                group = batch[0]
                group.assessment_source = "规则预分类（AI返回无效JSON）"
                logger.error("单条日志组 {} 返回无效JSON，跳过AI并保留规则预分类", group.group_id)
                return
            middle = len(batch) // 2
            logger.warning("批次返回无效JSON，直接拆分为 {} 和 {} 条，避免原批次重复计费", middle, len(batch) - middle)
            process_batch(batch[:middle], metric_server, server_name)
            process_batch(batch[middle:], None, server_name)
            return
        by_id = {str(item.get("id")): item for item in parsed.get("assessments", [])}
        for group in batch:
            assessment = by_id.get(group.group_id)
            if assessment:
                _apply(group, assessment, "AI实时评估")
        missing_count = len(batch) - len([group for group in batch if group.group_id in by_id])
        if missing_count:
            logger.warning("DeepSeek响应缺少 {} 个日志组的评估，缺失项保留规则结果", missing_count)
        if metric_server is not None:
            _apply_resource(metric_server, parsed.get("metric_assessments", []))
            logger.info("服务器={}，资源指标AI评估返回 {} 项", server_name, len(metric_server.resource_assessments))

    for index, (batch, metric_server, server_name) in enumerate(initial_batches, start=1):
        logger.info("处理AI初始批次 {}/{}", index, len(initial_batches))
        process_batch(batch, metric_server, server_name)
    _merge_ai_duplicates(groups)
    logger.info("AI处理完成：语义去重后剩余 {} 个日志组", len(groups))
    return f"DeepSeek V4-Flash 语义去重、分类及分析完成（{request_count} 次请求）"

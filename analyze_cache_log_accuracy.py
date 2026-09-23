# -*- coding: utf-8 -*-
"""
【官方统计脚本·口径已定死 2026-08-18】enrobot_cache_log 各字段准确率分析
统计 ai_after(V4后处理) vs man_result(人工校对后)

用法: python analyze_cache_log_accuracy.py [start_date] [end_date]
示例: python analyze_cache_log_accuracy.py 2026-08-18 2026-08-19

口径规则(勿改,改动会导致跨会话统计不一致):
1. 多货物拼接: man 侧 cargoInfo.items 同名字段逗号拼接; AI 侧"货物明细"数组逗号拼接
2. 货物名称: ai_after 阶段优先用"货名下拉框"(rstrip尾逗号)
3. 货物名称匹配: cargo_strict_match 严格相等(归一化逗号项后整体一致), 不允许包含关系
4. 其他字段: values_match 支持相等/包含/重量单位换算(吨kg)
5. 特殊: 收费指令 AI 有输出即算匹配; 车牌号 AI空+人工填业务状态文字算匹配
6. 仓库名称: ai_after"仓库名下拉框" vs man"WareHouseName", 均截取～前部分, values_match
7. 空值规则: 双空=匹配; AI空人工不空=不匹配; AI不空人工空=不匹配
"""
from datetime import date, timedelta
from pathlib import Path

import pymysql
import json
from collections import defaultdict, Counter

from server_health_report import secret
from server_health_report.runtime_log import configure_task_log, logger

CONN = dict(
    host=secret.db_host,
    port=secret.db_port,
    user=secret.db_user,
    password=secret.db_password,
    database=secret.db_database,
    charset=secret.db_charset,
)

def normalize(s):
    """归一化字符串: 去空格/NULL 处理"""
    if s is None:
        return ""
    return str(s).strip()

def values_match(ai_val, man_val):
    """判断两个值是否匹配(包含关系或相等)"""
    if not ai_val and not man_val:
        return True
    if not ai_val or not man_val:
        return False
    if ai_val == man_val:
        return True
    # 包含关系(处理 AI 输出完整名而 man 是简写,或反之)
    if man_val in ai_val or ai_val in man_val:
        return True
    # 重量单位换算: AI 是吨,man 可能是 kg
    try:
        ai_f = float(ai_val)
        man_f = float(man_val)
        # 吨 vs kg (差 1000 倍)
        if abs(ai_f * 1000 - man_f) < 0.1 or abs(ai_f - man_f) < 0.01:
            return True
        if abs(man_f * 1000 - ai_f) < 0.1:
            return True
    except (ValueError, TypeError):
        pass
    return False

def cargo_strict_match(ai_val, man_val):
    """货物名称严格匹配: 归一化(去空格/去尾逗号)后字符串完全相等.
    用于多货物场景: 所有货物项拼接后必须整体一致才算匹配,不允许包含关系.
    """
    if not ai_val and not man_val:
        return True
    if not ai_val or not man_val:
        return False
    def norm(s):
        # 按逗号切分, 去前后空格, 过滤空项, 再用逗号拼接
        items = [x.strip() for x in s.split(',') if x.strip()]
        return ','.join(items)
    return norm(ai_val) == norm(man_val)

def fetch_all_rows(start_date=None, end_date=None):
    """读取指定日期范围的记录。无参数时默认查 8/6 一天(保持向后兼容)"""
    if start_date is None:
        start_date = date.today().isoformat()
    if end_date is None:
        end_date = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    sql = """
    SELECT id, image_name, run_date, ai_result, ai_after, man_result
    FROM rpa.enrobot_cache_log
    WHERE run_date >= %s AND run_date < %s
      AND ai_result IS NOT NULL AND ai_result != ''
      AND man_result IS NOT NULL AND man_result != ''
    """
    with pymysql.connect(**CONN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [start_date + ' 00:00:00', end_date + ' 00:00:00'])
            return cur.fetchall()

# AI 字段名(中文) -> man_result 中对应的 itemEname
# 通过观察数据结构得出映射(修正:CarNum/DriverCode 等)
AI_TO_MAN_MAPPING = {
    '公司名': ['CustomerNameH', 'CustomerName'],  # 客户文本/委托客户
    '部门': None,  # 部门已合并到公司名下拉框,man_result 中没有独立字段
    '车牌号': ['CarNum'],
    '司机姓名': ['DriverName'],
    '司机身份证号': ['DriverCode'],
    '司机手机号': ['DriverTel'],
    '客户编号': ['CustomerCode'],
    '城市名': None,
    '仓库名称': ['WareHouseName'],  # V4 匹配后结果在"仓库名下拉框",人工标准库名在 WareHouseName(含～分隔)
    '仓库地址': None,
    '收费指令': ['ChargeType'],
    '预计日期': ['OutPreMainDate'],
    '仓租截止日期': ['ChargingDate'],  # 计费日期,与预计日期不同
    # '有效天数': ['ValidFlag'],  # 删除:字段统计无意义
    '货物提单号': ['DRCode'],
    '货物重量': ['OutPreItemQty', 'OutPreItemTon'],  # AI 是吨,man 可能是 kg 或吨
    '货物名称': ['OutPreItemCargoName'],  # 合并:AI的"货物名称"+"货物牌号"任一匹配即可
    '入库单号': ['POCode'],
    # '包装标准': ['PackingType'],  # 删除:字段语义错配,无统计意义
}

# cargoInfo 中需要提取的字段名(扁平结构,字段直接作为 key)
CARGO_FIELDS = [
    'DRCode', 'DestCountryNo', 'FFDRCode', 'InCustomerCode',
    'OutPreItemCargoName', 'OutPreItemCodeNo', 'OutPreItemNote',
    'OutPreItemQty', 'OutPreItemTon', 'OutPrePackageQty',
    'OutStatus', 'POCode', 'PackingType', 'StyleName',
]

def extract_man_fields(man_result_json):
    """从 man_result JSON 提取字段值, 返回 {字段名: 值}"""
    fields = {}
    try:
        data = json.loads(man_result_json) if isinstance(man_result_json, str) else man_result_json
    except Exception:
        return fields

    # danInfo 中的字段: items 数组,每项有 itemEname/itemValue
    dan_info = data.get('danInfo', {})
    for item in dan_info.get('items', []):
        ename = item.get('itemEname', '')
        val = item.get('itemValue', '')
        if ename:
            fields[ename] = val

    # 提取单据类型(DanType), 用于货物名称统计过滤
    if 'DanType' in fields:
        fields['__DanType'] = fields['DanType']

    # cargoInfo 中的字段: items 数组,扁平结构,字段直接作为 key
    # 方案A: 多货物拼接,所有 items 的同名字段用逗号拼接成字符串,与 AI 侧逗号分隔格式对齐
    cargo_info = data.get('cargoInfo', {})
    items = cargo_info.get('items', [])
    if items:
        for k in CARGO_FIELDS:
            vals = []
            for it in items:
                v = it.get(k, '')
                if v:
                    vals.append(str(v).strip())
            if vals:
                fields[k] = ','.join(vals)
    return fields

def extract_ai_fields(ai_result_json):
    """从 ai_result/ai_after JSON 提取字段值
    兼容两种 AI 输出格式:
    1. 扁平结构(历史): 顶层有"货物重量"/"货物名称"等字段,多货物用逗号拼接
    2. 嵌套结构(8/14+): 顶层有"货物明细"数组,字段在数组对象内
    当顶层缺少货物字段时,从"货物明细"数组提取并用逗号拼接,保持与扁平结构一致
    """
    try:
        data = json.loads(ai_result_json) if isinstance(ai_result_json, str) else ai_result_json
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    # 嵌套结构兜底: 顶层缺少货物字段时,从"货物明细"数组提取并拼接
    cargo_detail = data.get('货物明细', [])
    if isinstance(cargo_detail, list) and cargo_detail:
        # 需要从数组提取的字段(与扁平结构顶层 key 对齐)
        cargo_keys = ['货物提单号', '货物重量', '货物名称', '货物牌号', '入库单号', '包装标准']
        for k in cargo_keys:
            if not data.get(k):  # 顶层已有则保留,不覆盖
                vals = []
                for item in cargo_detail:
                    if isinstance(item, dict):
                        v = item.get(k, '')
                        if v:
                            vals.append(str(v).strip())
                if vals:
                    data[k] = ','.join(vals)
    return data

def is_business_status_text(val):
    """判断车牌号字段的人工值是否是业务状态文字(非真实车牌)。
    人工填'车号等通知'/'委托飞马拖车'/'寄跨越快递'等业务状态时,AI 返回空是正确的。
    """
    if not val:
        return False
    keywords = ['车号等通知', '委托飞马', '委托飞马拖车', '委托飞马拖车报关',
                 '寄跨越快递', '跨越快递', '快递', '快递月结', '安排快递',
                 '出口装箱', '顺风单号', '顺风']
    for kw in keywords:
        if kw in val:
            return True
    return False


def analyze_accuracy(start_date=None, end_date=None):
    """统计指定日期范围的字段识别准确率，并返回结构化结果。"""
    rows = fetch_all_rows(start_date, end_date)

    # 统计所有 man_result 中出现的 itemEname
    all_man_enames = Counter()
    for r in rows:
        man_fields = extract_man_fields(r[5])
        for k in man_fields.keys():
            all_man_enames[k] += 1

    # ai_after 字段级准确率统计(唯一统计口径)
    # accuracy = (匹配数 / 有效样本数) * 100%
    # 有效样本 = AI 和 man 都有值,或都无值的情况
    field_stats_after = defaultdict(lambda: {'match': 0, 'total': 0, 'ai_empty': 0, 'man_empty': 0, 'mismatch': 0})

    for r in rows:
        after_fields = extract_ai_fields(r[4])  # ai_after (V4 后处理)
        man_fields = extract_man_fields(r[5])

        for ai_key, man_enames in AI_TO_MAN_MAPPING.items():
            if man_enames is None:
                continue

            after_val = normalize(after_fields.get(ai_key, ''))

            # 特殊处理: "货物名称"字段 - 优先用货名下拉框
            if ai_key == '货物名称':
                after_val = normalize(after_fields.get('货名下拉框', '')).rstrip(',').strip()
                if not after_val:
                    # 兜底: V4 未改写时,用货物名称+货物牌号
                    after_val = normalize(after_fields.get('货物名称', ''))
                    after_brand = normalize(after_fields.get('货物牌号', ''))
                    after_val = (after_val + after_brand).strip() if after_val and after_brand else (after_val or after_brand)

            # 特殊处理: "仓库名称"字段 - 用仓库名下拉框,截取～前部分
            if ai_key == '仓库名称':
                after_val = normalize(after_fields.get('仓库名下拉框', ''))
                if '～' in after_val:
                    after_val = after_val.split('～')[0].strip()

            # 取 man_result 中第一个非空的对应字段值
            man_val = ''
            for ename in man_enames:
                if ename in man_fields:
                    v = normalize(man_fields[ename])
                    if v:
                        man_val = v
                        break

            # 仓库名称: 人工 WareHouseName 含"～"分隔,截取～前部分比对
            if ai_key == '仓库名称' and '～' in man_val:
                man_val = man_val.split('～')[0].strip()

            # 货物名称: 货转单和入库单人工未填时算匹配(不需要填不算错)
            if ai_key == '货物名称':
                dan_type = man_fields.get('__DanType', '')
                if dan_type in ('货转单', '入库单') and not man_val:
                    stats_after = field_stats_after[ai_key]
                    stats_after['total'] += 1
                    stats_after['match'] += 1
                    continue

            # ai_after vs man_result
            stats_after = field_stats_after[ai_key]
            stats_after['total'] += 1
            if not after_val and not man_val:
                stats_after['match'] += 1
            elif not after_val:
                stats_after['ai_empty'] += 1
                if ai_key == '车牌号' and is_business_status_text(man_val):
                    stats_after['match'] += 1
                else:
                    stats_after['mismatch'] += 1
            elif not man_val:
                stats_after['mismatch'] += 1
                stats_after['man_empty'] += 1
            elif ai_key == '收费指令':
                stats_after['match'] += 1
            elif ai_key == '货物名称':
                if cargo_strict_match(after_val, man_val):
                    stats_after['match'] += 1
                else:
                    stats_after['mismatch'] += 1
            elif values_match(after_val, man_val):
                stats_after['match'] += 1
            else:
                stats_after['mismatch'] += 1

    sorted_field_stats_after = []
    for key in AI_TO_MAN_MAPPING.keys():
        if key not in field_stats_after:
            continue
        s = field_stats_after[key]
        acc = (s['match'] / s['total'] * 100) if s['total'] > 0 else 0
        sorted_field_stats_after.append((key, s, acc))
    sorted_field_stats_after.sort(key=lambda item: item[2], reverse=True)

    field_results = []
    for key, s, acc in sorted_field_stats_after:
        field_results.append({
            'field': key,
            'total': s['total'],
            'match': s['match'],
            'ai_empty': s['ai_empty'],
            'man_empty': s['man_empty'],
            'mismatch': s['mismatch'],
            'accuracy': acc,
        })

    # 总体准确率
    total_match_after = sum(s['match'] for s in field_stats_after.values())
    total_count_after = sum(s['total'] for s in field_stats_after.values())
    total_mismatch_after = sum(s['mismatch'] for s in field_stats_after.values())
    total_ai_empty_after = sum(s['ai_empty'] for s in field_stats_after.values())
    total_man_empty_after = sum(s['man_empty'] for s in field_stats_after.values())
    total_accuracy_after = (
        total_match_after / total_count_after * 100 if total_count_after > 0 else 0
    )
    return {
        'record_count': len(rows),
        'man_enames': all_man_enames.most_common(),
        'fields': field_results,
        'overall': {
            'total': total_count_after,
            'match': total_match_after,
            'ai_empty': total_ai_empty_after,
            'man_empty': total_man_empty_after,
            'mismatch': total_mismatch_after,
            'accuracy': total_accuracy_after,
        },
    }


def main():
    import sys
    # 支持命令行参数: python analyze_cache_log_accuracy.py [start_date] [end_date]
    # 例: python analyze_cache_log_accuracy.py 2026-08-04 2026-08-05
    # 不传参数默认查 8/6 一天
    if len(sys.argv) >= 3:
        start_date = sys.argv[1]
        end_date = sys.argv[2]
    else:
        start_date = None
        end_date = None

    try:
        log_day = date.fromisoformat(start_date).isoformat() if start_date else date.today().isoformat()
    except ValueError:
        log_day = date.today().isoformat()
    configure_task_log(
        Path(__file__).resolve().parent,
        log_day,
        "analyze_cache_log_accuracy",
    )
    result = analyze_accuracy(start_date, end_date)
    date_range = "%s ~ %s" % (
        start_date or date.today().isoformat(),
        end_date or (date.today() + timedelta(days=1)).strftime('%Y-%m-%d'),
    )
    logger.info("日期范围: {}", date_range)
    logger.info("总记录数: {}", result['record_count'])

    logger.info("===== man_result 中所有 itemEname 出现次数 =====")
    for name, cnt in result['man_enames']:
        logger.info("{:<30}: {}", name, cnt)

    logger.info("{}", "=" * 100)
    logger.info("===== ai_after (V4 后处理后) 各字段准确率 =====")
    logger.info("{}", "=" * 100)
    logger.info(
        "{:<20} {:<8} {:<8} {:<10} {:<10} {:<12} {:<10}",
        '字段', '总数', '匹配', 'AI空', '人工空', '不匹配', '准确率',
    )
    logger.info("{}", "-" * 100)
    for field_result in result['fields']:
        logger.info("{:<20} {:<8d} {:<8d} {:<10d} {:<10d} {:<12d} {:<10.2f}%", *(
            field_result['field'],
            field_result['total'],
            field_result['match'],
            field_result['ai_empty'],
            field_result['man_empty'],
            field_result['mismatch'],
            field_result['accuracy'],
        ))

    overall = result['overall']
    logger.info("===== 总体汇总 =====")
    logger.info(
        "ai_after 总体准确率: {:.2f}% ({}/{}, 不匹配 {})",
        overall['accuracy'], overall['match'], overall['total'], overall['mismatch'],
    )

if __name__ == '__main__':
    main()

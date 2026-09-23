#!/usr/bin/env bash
# 飞马1 Docker 容器监测：单据 javaapi* 期望 3 个，邮件 pyservice* 期望 0 个，job javajob* 期望 1 个。

set -uo pipefail

HOST_LABEL="飞马1-47.101.41.88"
EXPECTED_JAVAAPI=3
EXPECTED_PYSERVICE=1
EXPECTED_JAVAJOB=1
STATE_FILE="/var/tmp/feima1_docker_monitor.state"
LOCK_DIR="/var/tmp/feima1_docker_monitor.lock"
WEBHOOK_URL="${FEISHU_WEBHOOK_URL:-https://open.feishu.cn/open-apis/bot/v2/hook/b0636974-8e68-471b-9b4f-7cb31ec6a68d}"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # 上一次检查尚未结束，避免多个 cron 任务重复发送消息。
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

send_feishu() {
    local message="$1"
    local escaped response

    # 将文本转换为 JSON 字符串，避免换行、反斜杠或双引号破坏请求体。
    escaped="$(printf '%s' "$message" | sed ':a;N;$!ba;s/\\/\\\\/g;s/"/\\"/g;s/\r/\\r/g;s/\n/\\n/g')"
    if ! response="$(curl -sS --connect-timeout 5 --max-time 15 \
        -H 'Content-Type: application/json' \
        -X POST \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"${escaped}\"}}" \
        "$WEBHOOK_URL" 2>&1)"; then
        echo "[$(date '+%F %T')] 飞书请求失败：$response" >&2
        return 1
    fi

    if ! printf '%s' "$response" | grep -Eq '"(code|StatusCode)"[[:space:]]*:[[:space:]]*0'; then
        echo "[$(date '+%F %T')] 飞书返回异常：$response" >&2
        return 1
    fi
    return 0
}

container_lines="$(docker container ls -a --format '{{.Names}}\t{{.State}}' 2>&1)"
docker_rc=$?

issues=()
if (( docker_rc != 0 )); then
    issues+=("Docker 查询失败：${container_lines}")
else
    javaapi_running=0
    javaapi_running_names=""
    javaapi_stopped=""
    pyservice_running=0
    pyservice_running_names=""
    pyservice_stopped=""
    javajob_running=0
    javajob_running_names=""
    javajob_stopped=""

    while IFS=$'\t' read -r name state; do
        [[ -z "$name" ]] && continue
        case "$name" in
            javaapi*)
                if [[ "$state" == "running" ]]; then
                    ((javaapi_running += 1))
                    [[ -n "$javaapi_running_names" ]] && javaapi_running_names+=", "
                    javaapi_running_names+="$name"
                else
                    [[ -n "$javaapi_stopped" ]] && javaapi_stopped+=$'\n'
                    javaapi_stopped+="${name}（${state}）"
                fi
                ;;
            pyservice*)
                if [[ "$state" == "running" ]]; then
                    ((pyservice_running += 1))
                    [[ -n "$pyservice_running_names" ]] && pyservice_running_names+=", "
                    pyservice_running_names+="$name"
                else
                    [[ -n "$pyservice_stopped" ]] && pyservice_stopped+=$'\n'
                    pyservice_stopped+="${name}（${state}）"
                fi
                ;;
            javajob*)
                if [[ "$state" == "running" ]]; then
                    ((javajob_running += 1))
                    [[ -n "$javajob_running_names" ]] && javajob_running_names+=", "
                    javajob_running_names+="$name"
                else
                    [[ -n "$javajob_stopped" ]] && javajob_stopped+=$'\n'
                    javajob_stopped+="${name}（${state}）"
                fi
                ;;
        esac
    done <<< "$container_lines"

    (( javaapi_running != EXPECTED_JAVAAPI )) && \
        issues+=("单据运行中容器数量异常：期望 ${EXPECTED_JAVAAPI} 个，实际运行中 ${javaapi_running} 个；运行中容器：${javaapi_running_names:-无}")
    if (( EXPECTED_JAVAAPI > 0 )) && [[ -n "$javaapi_stopped" ]]; then
        issues+=("单据容器存在但未运行：${javaapi_stopped}")
    fi
    (( pyservice_running != EXPECTED_PYSERVICE )) && \
        issues+=("邮件运行中容器数量异常：期望 ${EXPECTED_PYSERVICE} 个，实际运行中 ${pyservice_running} 个；运行中容器：${pyservice_running_names:-无}")
    if (( EXPECTED_PYSERVICE > 0 )) && [[ -n "$pyservice_stopped" ]]; then
        issues+=("邮件容器存在但未运行：${pyservice_stopped}")
    fi
    (( javajob_running != EXPECTED_JAVAJOB )) && \
        issues+=("job 运行中容器数量异常：期望 ${EXPECTED_JAVAJOB} 个，实际运行中 ${javajob_running} 个；运行中容器：${javajob_running_names:-无}")
    if (( EXPECTED_JAVAJOB > 0 )) && [[ -n "$javajob_stopped" ]]; then
        issues+=("job 容器存在但未运行：${javajob_stopped}")
    fi
fi

timestamp="$(date '+%F %T %Z')"
last_state="$(cat "$STATE_FILE" 2>/dev/null || true)"
# 兼容旧版本保存的 ALERT|详细信息格式，只保留状态，不因故障详情变化而重复告警。
case "$last_state" in
    ALERT|ALERT\|*) last_state="ALERT" ;;
    OK) last_state="OK" ;;
    *) last_state="" ;;
esac

if (( ${#issues[@]} == 0 )); then
    if [[ "$last_state" == "ALERT" ]]; then
        message="${HOST_LABEL} Docker 容器已恢复"
        message+=$'\n'
        message+="时间：${timestamp}"
        message+=$'\n'
        message+="状态：单据 javaapi* ${EXPECTED_JAVAAPI} 个、邮件 pyservice* ${EXPECTED_PYSERVICE} 个、job javajob* ${EXPECTED_JAVAJOB} 个，均存在且运行中。"
        if send_feishu "$message"; then
            printf '%s' "OK" > "$STATE_FILE"
        fi
    else
        printf '%s' "OK" > "$STATE_FILE"
    fi
else
    details=""
    for issue in "${issues[@]}"; do
        [[ -n "$details" ]] && details+=$'\n'
        details+="· ${issue}"
    done
    if [[ "$last_state" != "ALERT" ]]; then
        message="${HOST_LABEL} Docker 容器异常"
        message+=$'\n'
        message+="时间：${timestamp}"
        message+=$'\n'
        message+="${details}"
        if send_feishu "$message"; then
            printf '%s' "ALERT" > "$STATE_FILE"
        fi
    fi
    echo "[$timestamp] ${HOST_LABEL} Docker 容器异常：${details}" >&2
fi

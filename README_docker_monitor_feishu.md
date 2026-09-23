# 飞马1、飞马2 Docker 容器监测

两个脚本均使用 Bash 和 Docker CLI，通过容器名**前缀**识别容器；规定数量只统计状态为 `running` 的容器，停止、退出或创建未启动的容器不计入数量。只有规定运行数量大于 0 时，已存在但未运行的容器才会触发停止告警；规定数量为 0 时不报告此类异常：

- `javaapi*`：单据服务
- `pyservice*`：邮件服务
- `javajob*`：job 服务（仅飞马1）

监测配置如下：

| 服务器 | 单据 `javaapi*` | 邮件 `pyservice*` | job `javajob*` |
|---|---:|---:|---:|
| 飞马1 | 3 | 0 | 1 |
| 飞马2 | 3 | 3 | 0 |

## 部署

将以下文件分别复制到对应服务器：

- 飞马1：`monitor_feima1_docker.sh`
- 飞马2：`monitor_feima2_docker.sh`

执行：

```bash
chmod +x monitor_feima1_docker.sh
./monitor_feima1_docker.sh
```

飞马2使用同样方式执行 `monitor_feima2_docker.sh`。运行用户需要能够执行 Docker，例如加入 `docker` 用户组，或使用 root 执行。

## 定时监测

编辑当前用户的定时任务：

```bash
crontab -e
```

每分钟检查一次（请替换为实际脚本绝对路径）：

```cron
* * * * * /opt/docker-monitor/monitor_feima1_docker.sh >> /var/log/feima1_docker_monitor.log 2>&1
```

飞马2服务器使用：

```cron
* * * * * /opt/docker-monitor/monitor_feima2_docker.sh >> /var/log/feima2_docker_monitor.log 2>&1
```

## 如何发送飞书消息

脚本通过 `curl` 向飞书自定义机器人 Webhook 发送 HTTP POST 请求，请求体格式为：

```json
{
  "msg_type": "text",
  "content": {
    "text": "飞马1 Docker 容器异常\n单据容器数量异常：期望 3 个，实际 2 个"
  }
}
```

当前脚本已内置你提供的 Webhook 地址。也可以通过环境变量覆盖，避免把地址直接写在脚本中：

```bash
export FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/你的WebhookToken'
./monitor_feima1_docker.sh
```

脚本会在以下情况告警：容器数量不符合预期、容器存在但不是 `running` 状态、Docker 查询失败。同一次故障期间只发送一次异常消息，不会每分钟重复发送；恢复后只发送一次恢复通知。下次再次发生故障时，才会重新发送异常消息。

## 手工测试

可以先检查识别结果：

```bash
docker container ls -a --format '{{.Names}}\t{{.State}}'
```

然后停止一个测试容器：

```bash
docker stop <测试容器名>
./monitor_feima1_docker.sh
```

确认飞书收到消息后再启动：

```bash
docker start <测试容器名>
./monitor_feima1_docker.sh
```

> Webhook 属于敏感凭据。该地址已经出现在当前对话和脚本中，正式环境建议在飞书机器人后台重新生成 Token，并通过环境变量或受限权限配置文件提供。

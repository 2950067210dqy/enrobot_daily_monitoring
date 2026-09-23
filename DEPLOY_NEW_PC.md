# 新电脑一键部署

## 部署前准备

1. 将项目复制到新电脑任意目录，不要复制旧电脑的 `.venv`、`logs`、`result`、`txt` 和 `cache`。如果已经复制了 `.venv`，部署脚本会检测解释器是否可用；无效时会安全删除并重建项目内的 `.venv`。
2. 在新电脑生成 SSH 密钥，并把公钥加入三台服务器对应用户的 `~/.ssh/authorized_keys`。
3. 安全复制或重新填写 `server_health_report\secret.py`，包括三个 `scp_*` 地址、SSH 密钥路径和 `feishu_hook_url`；不要将 API 密钥、邮箱授权码或飞书 Webhook 提交到 Git。
4. 双击 `initialize_ssh_hosts.cmd`，逐台核对主机指纹并输入 `yes`。脚本会生成用户 `known_hosts`，并同步计划任务使用的 SYSTEM `known_hosts`。
5. 新电脑需要能够访问 Python 包下载源、三台服务器的 SSH 端口和 QQ 邮箱 SMTP。

## 一键安装

双击：

```text
deploy_new_pc.cmd
```

脚本会自动申请管理员权限，并执行：

- 检查或安装 Windows OpenSSH Client；
- 使用 Python 3.7 或更高版本创建 `.venv`，支持 32 位和 64 位 Python；
- 安装 `requirements.txt`；
- 校验三台服务器和邮箱配置；
- 将当前用户 SSH 密钥复制为 SYSTEM 专用副本，并限制 ACL；
- 创建 `ServerHealthDailyMonitoring` 计划任务；
- 每天 17:40 以 SYSTEM 身份执行一次，用户未登录也可运行。

## 使用非默认私钥

使用管理员 PowerShell 执行：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\deploy_new_pc.ps1 -UserPrivateKey "D:\keys\id_ed25519"
```

## 验证

```powershell
.\.venv\Scripts\python.exe .\run_daily_monitoring.py --dry-run
schtasks.exe /Query /TN "ServerHealthDailyMonitoring" /FO LIST /V
```

正式任务日志：

```text
项目目录\logs\daily_monitoring_task.log
```

## 修复 ReportLab 图表后端

如果生成报告时提示缺少 `rlPyCairo` 或 `renderPM backend`，双击：

```text
repair_reportlab.cmd
```

脚本会按当前虚拟环境的 Python 版本重新安装兼容的 ReportLab，并实际生成一张内存 PNG 验证渲染后端。

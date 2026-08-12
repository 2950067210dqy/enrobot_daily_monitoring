# 服务器巡检 TXT 与趋势 PDF 使用说明

## 1. 文件结构

PDF 功能已按模块拆分，部署时需要同时复制：

- `server_health_pdf_report.py`：命令行入口；
- `server_health_report/`：TXT 解析、趋势指标、日志分类去重、AI 评估、图表和 PDF 渲染；
- `requirements-server-health-pdf.txt`：Python 依赖。

`server_health_interface_mail.sh/.ini` 和 `server_health_client.sh/.ini` 仍负责只读采集 TXT，不会启停服务或修改业务数据。

- `server_health_client.sh` 是普通用户可运行的低权限轻量版，检查主机时间、CPU/负载、内存/Swap、磁盘/inode、磁盘IO及INI中指定的进程。进程明细包含PID、启动时间、CPU、内存和线程数。Docker只判断命令和只读查看能力；服务器没有`docker`命令时显示“不可判定并跳过”，不会尝试安装。它不检查systemd、端口、网络依赖、JVM、日志或业务目录。
- `server_health_interface_mail.sh` 保留完整服务器检查，并增加磁盘IO概要与明细。异常日志重点读取 `LOG_DIRS` 配置的业务日志目录，只选择最近24小时更新的最新文件；专用 `warn*.log`、`error*.log` 会完整输出，没有专用日志时才从普通日志输出完整异常匹配行，不再截断行数或单行内容。

## 2. 安装依赖

```bash
python -m pip install --force-reinstall -r requirements-server-health-pdf.txt
```

依赖文件会让 Python 3.7 使用兼容的 ReportLab 3.6.13，Python 3.8 及以上使用 ReportLab 4.x。

## 3. 单期或多期 TXT 汇总

不带任何参数运行时，程序以入口脚本所在目录为根目录，读取当天三个目录中的全部 `.txt` 文件：

- `txt/client/YYYY-MM-DD/`
- `txt/interface_mail_1/YYYY-MM-DD/`
- `txt/interface_mail_2/YYYY-MM-DD/`

并生成：`result/YYYY-MM-DD/服务器巡检报告.pdf`。

```bash
python server_health_pdf_report.py
```

需要重新生成指定日期时，可使用：

```bash
python server_health_pdf_report.py --date 2026-08-12
```

传入一个目录时，脚本会读取目录内所有 `server_health_*.txt`。相同主机名的多个 TXT 按巡检时间合并为一个服务器的时间序列：

```bash
python server_health_pdf_report.py ./巡检结果 -o 服务器巡检报告.pdf
```

也可以显式传入文件或通配符：

```bash
python server_health_pdf_report.py 飞马1_上午.txt 飞马1_下午.txt Client_上午.txt Client_下午.txt -o 服务器巡检报告.pdf
python server_health_pdf_report.py "./巡检结果/server_health_*.txt" -o 服务器巡检报告.pdf
```

PDF 固定结构：

1. 第一页只有每台服务器的直观图表概要；
2. 每台服务器的表格文字概要，包含巡检模式、采集覆盖度、检查项、进程线程数/PID、Docker、日志目录与业务目录名称；
3. CPU、内存、磁盘、磁盘 I/O（iowait/%util）、进程线程数和各日志目录大小的时间变化图；
4. AI/规则异常数量汇总；
5. 按系统/进程日志、Docker 日志、应用日志目录日志分类的去重明细，并按重要程度排序。

只有一份 TXT 时，图表会标记“单次采样”；至少两份同主机 TXT 时才形成变化折线。

新版 `server_health_client.sh` 是低权限轻量巡检，只采集 CPU、内存/Swap、磁盘/inode、磁盘 I/O、指定进程和 Docker 可见性。PDF 会将 systemd、端口、网络依赖、JVM、应用日志和业务目录标记为“脚本未纳入”，不会当作服务器异常；Docker 无权限、缺少 iostat 等会列入“采集受限”。

新版 Interface Shell 输出 `磁盘IO性能` 汇总行，PDF 会提取 iowait、最繁忙设备和最高 `%util`。旧 TXT 没有该字段时显示“旧版TXT未采集”，不补造数值。

## 4. AI 日志评估

AI 使用 SiliconFlow 的 `deepseek-ai/DeepSeek-V4-Flash`，请求签名沿用 `sign-ts/sign-key` 形式。API Key 不写入源码，使用环境变量：

AI HTTP 请求使用 Python 标准库，不额外依赖 `requests`。

PowerShell：

```powershell
$env:SILICONFLOW_API_KEY="实际密钥"
python server_health_pdf_report.py ./巡检结果 --ai required -o 服务器巡检报告.pdf
```

Linux：

```bash
export SILICONFLOW_API_KEY='实际密钥'
python server_health_pdf_report.py ./巡检结果 --ai required -o 服务器巡检报告.pdf
```

模式说明：

- `--ai auto`：默认；有 Key 时调用 AI，没有 Key 或调用失败时使用规则预分类并在 PDF 中标明；
- `--ai required`：AI 缺少 Key、请求失败或响应异常时终止，不生成伪造的 AI 结果；
- `--ai off`：不访问网络，只做规则预分类；
- `--ai-cache 文件.json`：缓存已经完成的日志组评估，避免相同异常反复计费；
- `--max-log-groups 300`：PDF 最多显示的去重异常组；`0` 表示全部。

模型只返回重要程度、是否立即处理、是否需要修改、异常标题和判断依据，不生成修复步骤或处置建议。

## 5. 数据边界

- 趋势图只使用 TXT 中真实采集到的数据，不补造缺失采样；
- 多期数据按 `主机名` 归并；主机名缺失时才使用服务器备注；
- 日志去重会归一化时间戳、PID、容器 ID、trace/span ID 和普通数字，再按异常模式分组；
- AI 评估是辅助分类，不等于已确认根因；报告会标明使用的是实时 AI、AI 缓存还是规则预分类；
- PDF 不输出原始采集明细全文，不再重复展示前置详细指标和原始异常线索。

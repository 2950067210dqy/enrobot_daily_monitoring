# 服务器巡检 TXT 与趋势 PDF 使用说明

## 1. 文件结构

PDF 功能已按模块拆分，部署时需要同时复制：

- `server_health_pdf_report.py`：命令行入口；
- `server_health_report/`：TXT 解析、趋势指标、日志分类去重、AI 评估、图表和 PDF 渲染；
- `requirements-server-health-pdf.txt`：Python 依赖。

`server_health_interface_mail.sh/.ini` 和 `server_health_client.sh/.ini` 仍负责只读采集 TXT，不会启停服务或修改业务数据。

两份Shell在没有使用 `-o` 指定输出文件时，都会在脚本所在目录创建当天的 `YYYY-MM-DD` 子目录，并把该日每次巡检生成的TXT放入其中。例如：`server-health/2026-08-12/server_health_服务器备注_主机名_20260812_083000.txt`。目录已存在时直接复用，不覆盖或删除已有报告。

Interface 两台服务器使用不同配置：飞马1使用 `server_health_interface_mail.ini`；飞马2使用 `server_health_interface_mail_2.ini`。飞马2不部署8089、8090、8800端口及 `javajob8089`、`rpaadmin8090`、`backendadmin8800` 容器，也不要求对应的 JOB、RPA管理端和后台管理端进程，巡检和PDF均不会将它们计为异常。

```bash
bash server_health_interface_mail.sh -c server_health_interface_mail_2.ini
```

- `server_health_client.sh` 是普通用户可运行的低权限轻量版，检查主机时间、CPU/负载、内存/Swap、磁盘/inode、磁盘IO及INI中指定的进程。进程明细包含PID、启动时间、CPU、内存和线程数。Docker只判断命令和只读查看能力；服务器没有`docker`命令时显示“不可判定并跳过”，不会尝试安装。它不检查systemd、端口、网络依赖、JVM、日志或业务目录。
- `server_health_interface_mail.sh` 保留完整服务器检查，并增加磁盘IO概要与明细。异常日志重点读取 `LOG_DIRS` 配置的业务日志目录，时间严格限制为脚本执行当天00:00:00至脚本启动时刻；专用 `warn*.log`、`error*.log` 会输出当天完整日志，没有专用日志时才从普通日志输出当天完整异常匹配行，不再截断行数或单行内容。
- Interface邮件脚本检查监听端口时优先使用 `ss`，服务器没有 `ss` 时自动使用 `netstat`；两者都没有才标记为不可判定。默认路由同样按 `ip`、`route`、`netstat -rn` 顺序降级，不再把命令缺失误报成网络或端口异常。
- 系统日志（`dmesg`、`journalctl`）只检索 `error/err` 及更严重级别，不检索 `warning/warn`。业务日志优先选择当天的 `warn*.log`、`error*.log`，但文件内容仍须命中 `ERROR、Exception、FATAL、Traceback、OutOfMemory、OOM` 等异常关键字才输出；命中记录后续的完整多行堆栈会一并保留，普通WARN不输出。
- 为避免历史时间混入日志分析，systemd状态改用不附带日志的 `systemctl show`；各服务的 `journalctl` 强制限制为执行当天且仅取error；Docker详情不再输出历史 `Started/Finished` 和健康检查历史；登录历史 `last` 不再采集。

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

并生成：

- `result/YYYY-MM-DD/服务器巡检报告_YYYY_MM_DD.pdf`；
- `result/YYYY-MM-DD/服务器巡检报告_YYYY_MM_DD_图表/`，保存本次PDF使用的全部PNG图表。

程序先把首页资源图、服务器进程/目录趋势图以及资源概要条输出为独立PNG，再把同一批PNG按原显示尺寸插入PDF。图片文件使用`chart_序号_业务名称.png`命名，命令行和Loguru执行日志都会给出图表目录及数量；再次生成同名报告时，只清理并重建该专用目录中由本工具生成的`chart_*.png`。

启动时会自动创建以上三个当天 TXT 目录以及 `result/YYYY-MM-DD/` 结果目录；目录已存在时不会改动其中内容。当天没有 TXT 时，目录仍会创建，然后程序提示没有可汇总文件。

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
3. 首页统一展示CPU、内存、磁盘、磁盘 I/O iowait 和 `%util` 图表，横坐标为巡检时间，飞马1、飞马2和Client作为图例系列；磁盘会从df明细中提取全部真实业务磁盘及NFS挂载点，排除`/boot/efi`、tmpfs和Docker overlay等非业务或临时挂载，概要卡为每个挂载点分别显示“挂载点：使用率”和独立进度条，趋势图也按挂载点分别绘制；各服务器页面不再重复这些资源图，只展示该服务器的进程线程数和各日志目录大小变化图，并位于文字概要表之前；
4. AI/规则异常数量汇总；
5. 按应用日志目录日志、Docker 日志、系统/进程日志顺序展示分类去重明细，并按重要程度排序。

各章节完全连续排版，不强制换页，也不为新章节预留最小起始空间；当前页剩余位置会先继续放置章节间距和标题，图表或表格确实放不下时再由ReportLab自然分页。

只有一份 TXT 时，图表会标记“单次采样”；至少两份同主机 TXT 时才形成变化折线。

新版 `server_health_client.sh` 是低权限轻量巡检，只采集 CPU、内存/Swap、磁盘/inode、磁盘 I/O、指定进程和 Docker 可见性。PDF 会将 systemd、端口、网络依赖、JVM、应用日志和业务目录标记为“脚本未纳入”，不会当作服务器异常；Docker 无权限、缺少 iostat 等会列入“采集受限”。

新版 Interface Shell 输出 `磁盘IO性能` 汇总行，PDF 会提取 iowait、最繁忙设备和最高 `%util`。旧 TXT 没有该字段时显示“旧版TXT未采集”，不补造数值。

服务器详情的“磁盘容量”和“inode”不再只复述最高值：磁盘容量会逐项显示挂载点、使用率、文件系统、总量、已用和可用容量，inode会逐项显示各真实挂载点的inode使用率。

## 4. AI 日志评估

AI 使用 DeepSeek 官方接口的 `deepseek-v4-flash`，用于日志语义去重、来源分类、重要程度、处理建议和操作分析。API Key 默认读取 `server_health_report/secret.py` 中的 `api_secret`，也可使用环境变量：

DeepSeek Prompt 保存在 `server_health_report/deepseek_prompt.txt`，程序运行时按 UTF-8 读取。调整分析规则或 JSON 输出约束时直接修改该 TXT，不需要修改 Python 代码。

每台服务器的 CPU、内存、磁盘使用率、IO iowait 和 IO util 时间序列也会交给 DeepSeek，PDF 展示各指标的重要程度、判断依据、建议与操作分析。资源指标并入该服务器第一批日志请求，不额外增加已有日志服务器的请求次数；服务器完全没有异常日志时才单独发送一次指标请求。

AI HTTP 请求使用 Python 标准库，不额外依赖 `requests`。

PowerShell：

```powershell
$env:DEEPSEEK_API_KEY="实际密钥"
python server_health_pdf_report.py ./巡检结果 --ai required -o 服务器巡检报告.pdf
```

Linux：

```bash
export DEEPSEEK_API_KEY='实际密钥'
python server_health_pdf_report.py ./巡检结果 --ai required -o 服务器巡检报告.pdf
```

模式说明：

- `--ai auto`：默认；有 Key 时调用 AI，没有 Key 或调用失败时使用规则预分类并在 PDF 中标明；
- `--ai required`：AI 缺少 Key、请求失败或响应异常时终止，不生成伪造的 AI 结果；
- `--ai off`：不访问网络，只做规则预分类；
- `--max-log-groups 300`：PDF 最多显示的去重异常组；`0` 表示全部。

模型返回语义重复关系、日志来源分类、重要程度、是否立即处理、是否需要修改、异常标题、判断依据、处理建议和操作分析。

## 5. 数据边界

- 趋势图只使用 TXT 中真实采集到的数据，不补造缺失采样；磁盘趋势按挂载点分别展示，旧TXT没有df明细时才回退到概要中的“最高使用率+挂载点”；
- Client TXT中的单路径容量/inode表和后续“全部可见文件系统”表会连续解析并按挂载点合并，确保 `/`、`/data`、`/attachment` 等真实挂载点不会因前置单路径表而遗漏；
- 多期数据按 `主机名` 归并；主机名缺失时才使用服务器备注；
- 日志去重会归一化时间戳、PID、容器 ID、trace/span ID 和普通数字，再按异常模式分组；相同日志只生成一个主事件，并在PDF“重复数量”列记录首次出现之后的重复次数；
- 监控TXT中的异常日志范围是当天00:00至该次脚本执行时间，因此每台服务器只从巡检时间最晚的一份TXT提取日志；更早TXT仅用于CPU、内存、磁盘、IO、进程线程和目录大小趋势。日志去重及DeepSeek分析只基于最新TXT中的累计日志，不再叠加所有小时TXT；
- PDF解析层按 `--date` 再做日志日期保险（未传时使用当天）：只有行首日期明确等于目标日期的日志才进入去重和DeepSeek分析；历史日期、无明确日期的状态说明、Docker Started/Finished等元数据均丢弃；
- AI 按服务器分批，每批最多15个去重日志组，并保留约5.5万字符的第二道上限；资源指标只随该服务器第一批发送。较小批次用于降低长JSON缺少逗号或被截断后再次拆批造成的重复费用；样例压缩至约800字符并使用紧凑输入JSON。JSON先在本地修复，确实失败时才继续拆批，不原样重发整批；
- DeepSeek成功返回且覆盖本批全部日志与资源指标时，结果写入`cache/deepseek_ai_cache.sqlite3`。缓存键同时包含模型、Prompt全文、日志批次和资源时间序列，因此修改Prompt、模型、日志或指标会自动形成新缓存，不会错误复用旧分析；完整命中时不发送API请求，部分命中时只请求未命中批次。可通过环境变量`SERVER_HEALTH_AI_CACHE_PATH`把数据库放到其他持久化目录；
- 缓存结果仍然是DeepSeek历史评估，恢复到PDF时“来源”列继续显示`AI实时评估`，不会显示“缓存”。执行日志会单独记录缓存命中批次、实际API请求次数以及缓存数据库位置；未配置API Key时仍可复用已有缓存，只有未命中项才降级为规则预分类（`--ai required`模式下未命中会终止）；
- AI 评估是辅助分类，不等于已确认根因；报告会标明使用的是实时 AI 还是规则预分类；
- PDF 不输出原始采集明细全文，不再重复展示前置详细指标和原始异常线索。

程序使用 Loguru 将执行阶段、当前动作、AI批次、JSON解析错误与重试结果写入 `logs/YYYY-MM-DD/server_health_pdf_YYYYMMDD_HHMMSS.log`，同时在控制台显示主要进度。日志不会记录 API Key。

每次 DeepSeek API 返回的完整响应正文、HTTP 状态、请求编号、日志组数量和日志组 ID 单独写入 `logs/YYYY-MM-DD/deepseek_YYYYMMDD_HHMMSS.log`，避免与普通执行进度混在一起；该文件同样不会记录 API Key。

DeepSeek 返回 JSON 使用可读字段名，例如 `log_assessments`、`metric_assessments`、`severity`、`recommendation` 和 `action_analysis`；请求输入仍使用紧凑字段以节省 Token。

Prompt 强制每条日志和每项资源指标都返回非空建议与操作分析；`action_analysis` 必须包含与问题相关的Linux命令步骤，并遵循“只读检查→定位→验证”的顺序。低风险项也必须给出只读核验命令；重启、删除或修改配置等操作只能作为经人工确认后的条件步骤，不允许直接给出破坏性命令。命令涉及目录或文件时不得输出具体绝对路径或精确日志文件名，统一使用“API日志目录下的log文件”等业务语义，或 `<API日志目录>`、`<配置目录>` 等待人工按实际配置替换的占位符。若模型仍遗漏，PDF 会使用明确的观察和核验兜底文本，不显示“未提供”。

异常日志明细按事件编号展示：第一行是服务器、重要度、判断、建议、操作、来源和重复数量；其后使用横跨其余列的宽幅区域完整展示“去重日志样例”。重复数量只计算首次出现之后的相同日志次数。考虑到ReportLab 3.6的单表格行跨页会产生整页空白，程序仅在原日志换行处建立内部分页块，但这些块不绘制内部横线、没有块间距、没有“续”标题，PDF视觉上仍是一个连续单元格，也不会把一条堆栈行从中间截断。

异常日志表格跨页时会在每一页顶部重复“编号、服务器、重要度、发现时间、判断、建议、操作、来源、重复数量”表头。无缝日志分页块只在实际换页时触发表头，不会在同一页中间重复显示。

## 6. 知识图谱运维执行履历

每次成功生成 PDF 后，程序会在同一目录生成同名的 `*_运维执行履历.json`。文件包含 `entities`、`relations` 和 `execution_history`，覆盖报告、服务器、巡检快照、TXT证据、资源评估、日志事件、建议与操作分析，可直接作为知识图谱 Agent 的结构化输入。

巡检采集和报告生成会标记为 `completed`；AI 给出的建议和操作分析统一标记为 `proposed_not_executed`，表示待执行方案，不代表服务器已经被修改。

WIKI内容生成要求、提取重点、知识库描述、页面模板和验收规则见 `WIKI_server_health_knowledge_base.md`。


## 7.自动化
先演练，不连接服务器：
```bash
.\.venv\Scripts\python.exe .\run_daily_monitoring.py --dry-run
```
正式运行：
```bash
.\.venv\Scripts\python.exe .\run_daily_monitoring.py
```
指定日期运行：
```bash
.\.venv\Scripts\python.exe .\run_daily_monitoring.py --date 2026-08-14
```
流程会依次：
下载三台服务器当天目录下的所有 TXT。
调用 server_health_pdf_report.py 生成报告。
将 result\YYYY-MM-DD 下的 PDF 发送到 QQ 邮箱。
任意一台服务器下载失败，流程都会停止，不会生成或发送不完整报告。secret.py 已加入 [.gitignore (line 1)](D:/dqy/workspace/monitoring_project/.gitignore:1)，避免账号、IP 和邮箱授权码被提交。语法检查和三条 SCP 命令的安全演练均已通过。

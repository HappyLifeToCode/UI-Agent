# UI-Agent：大模型 Agent 轨迹数据采集流水线

谷歌学术人物检索任务的 Agent 轨迹采集系统，用于生成 SFT/RL 训练数据。

## 项目目标

让 LLM Agent（Kimi Code）通过 MCP 协议操控浏览器，完成"谷歌学术搜人 → 进作者主页 → 抽取信息 → 整页截图"任务，完整记录 Agent 执行轨迹，供后续转换为训练数据。

## 目录结构

```
tasks/          任务清单（tasks.jsonl）
scripts/        执行脚本（run_tasks.py 批量执行器、run_cnki_tasks.py 知网执行器、
                prompt 模板、playwright_mcp_config.json / stealth_init.js 反检测配置）
server/         Web 演示（GS 学者检索 + CNKI 关键词检索两个独立页面）
data/           产出数据（不进 Git，仅保留结构）
docs/           文档（QA1.md 环境搭建 / log1.md 操作手册 / FORMAT.md 格式契约）
qa/             质检工具（validate_data.py）
logs/           执行日志（非交付物，不进 Git）
```

## 技术栈

- **Agent 框架**：Kimi Code
- **浏览器自动化**：Playwright（通过 playwright-mcp，锁定 0.0.64）
- **协议**：MCP（Model Context Protocol）
- **训练数据格式**：OpenAI messages 格式（JSONL）

## 快速开始

### 第 0 步：环境搭建

**先读 [docs/QA1.md](docs/QA1.md)** —— 里面有关键的版本注意事项
（playwright-mcp 必须锁定 0.0.64，`@latest` 拿不到 trace.zip），配错会导致产出不符合契约。

核心检查项：

- Node.js ≥ 18、Python 3.8+、Kimi Code CLI 已安装；
- `~/.kimi-code/mcp.json` 中 playwright 服务为
  `@playwright/mcp@0.0.64 --headless --save-trace --output-dir <项目>/.playwright-mcp`
  （完整配置原文见 QA1.md，改完需重启会话生效）。

### 第 1 步：准备任务清单

把任务写进 `tasks/tasks.jsonl`，每行一个 JSON，三个字段：

```json
{"task_id": "task_0001", "person_name": "Geoffrey Hinton", "affiliation_hint": "University of Toronto"}
{"task_id": "task_0002", "person_name": "Yann LeCun", "affiliation_hint": "New York University"}
```

`affiliation_hint` 用于在同名作者中挑人，没有线索可给空字符串。

### 第 2 步：单条验证

```bash
python scripts/run_tasks.py --limit 1 --no-delay
```

检查 `data/task_0001/` 的产物（三段式管线，2026-08-13 起）：

```
data/task_0001/
├── task.json        # 任务定义副本
├── phase1.json      # 阶段一产物：作者信息 + 近五年全部论文清单
├── ranked_papers.json  # 执行器按 GS 被引降序编号的清单（rank 1..N）
├── checks/          # 阶段二逐篇核查 fragment（paper_NN.json，每篇一个；
│                    #   合并进 result.json 后自动清理，仅运行中途可见）
├── result.json      # 合并后的契约结果（姓名/单位/引用数/代表作 + recent_papers
│                    #   近五年全部论文的核查数据；status 含 partial 态）
├── task_0001_report.docx  # Word 报告（逐篇详情 + 截图证据，export_word.py 生成）
├── wire.jsonl       # Agent 完整轨迹（各阶段 wire 按序拼接）
├── trace.zip        # 主会话浏览器侧轨迹（npx playwright show-trace 可回放）
├── trace_batchNN.zip    # 各核查批会话的 trace（附加产物）
└── screenshots/
    ├── task_0001_profile.png    # 作者主页整页截图
    ├── task_0001_paper_01.png   # rank 1 论文的核查页截图
    └── ...                      # 每篇 recent_papers 一张（not_found 为搜索页留证）
```

旧版单会话 top-10 链路仍可用 `python scripts/run_tasks.py --template
task_prompt_template_s2.md` 回退（不产生 phase1/checks/docx）。

### 第 3 步：全量跑批

```bash
python scripts/run_tasks.py                        # 跑全部任务
```

执行器自动完成：逐条拉起 Kimi 会话 → 收归轨迹/截图/trace → 写映射表。
内置反爬：任务间随机等待 30-90 秒，遇 CAPTCHA 自动等 2-5 分钟重试（默认最多 2 次）。
同一时刻只允许一个执行器实例（`data/.runner.lock`），不要在别处并发跑批。

常用参数：

```bash
python scripts/run_tasks.py --start-from task_0003   # 从某条开始补跑
python scripts/run_tasks.py --limit 3                # 只跑前 3 条
python scripts/run_tasks.py --dry-run                # 只打印计划，不真跑
python scripts/run_tasks.py --no-delay               # 关闭反爬延迟（仅测试）
python scripts/run_tasks.py --model kimi-for-coding/k1  # 指定模型（默认 k3）
```

**模型配置**：默认使用 `kimi-for-coding/k3`，可通过以下方式指定其他模型：
- 命令行参数：`--model <模型名>`（优先级最高）
- 环境变量：`set AGENT_MODEL=<模型名>`（Windows）或 `export AGENT_MODEL=<模型名>`（Linux/Mac）

### 第 4 步：质检

```bash
python qa/validate_data.py                         # 全部通过 exit 0
python qa/validate_data.py --task-id task_0003     # 只查某条
```

检查项：5 项产物齐全、result.json 字段与整数类型、截图大小、trace.zip 完整性、
**轨迹断档检测**（tool.call↔tool.result、step.begin↔step.end 配对）。
有 issue 的任务按报告原因补跑：`--start-from <task_id> --limit 1`。

### 第 5 步：交付下游

- 下游转换前必读 **[docs/FORMAT.md](docs/FORMAT.md)**——逐字段的格式契约
  （目录结构、result.json / wire.jsonl / mapping.jsonl / trace.zip 定义、
  断档红线、mapping 读取规则）。

## 知网（CNKI）关键词采集

与 GS 学者链路完全独立：按**关键词**（非人名）检索知网，逐篇采集题录 + 摘要全文。

**启动（独立端口 + 独立页面）：**

```bash
python server/app.py --cnki --port 8002      # 浏览器打开 http://localhost:8002
```

输入关键词与篇数（默认 20）即可；已采过的关键词自动走缓存。页面上：

- **文献总表**：序号/标题点击跳转到下方对应摘要全文（锚点 `abs_NN`）；
- **导出汇总表（Word）**：总表序号/标题为内部超链接，直跳摘要附录；
- **导出 JSON**：`papers.json` 逐篇含 rank + abstract，序号↔摘要关联以字段形式保留。

产物在 `data/cnki_<关键词>/`：`papers/paper_NN.json`（逐篇 fragment，断点续采）、
`papers.json`（合并结果）、`*_汇总表.docx`。批量跑批用
`python scripts/run_cnki_tasks.py --task <关键词> --limit 20`。

> GS 页面仍在 8001：`python server/app.py`。两个页面共用执行器锁
> （`data/.runner.lock`），同一时刻只有一边能采集，属反爬保护而非故障。

## 文档导航

| 文档 | 内容 | 谁该读 |
|---|---|---|
| [docs/QA1.md](docs/QA1.md) | 环境搭建、版本注意事项、并发约束 | 搭建环境的人（必读） |
| [docs/log1.md](docs/log1.md) | 操作手册：项目结构、工作流程、反爬策略、质检要点 | 日常跑批的人 |
| [docs/FORMAT.md](docs/FORMAT.md) | 交付数据格式契约（唯一权威） | 下游转换管线/质检（必读） |

## 常见问题

- **提示锁文件已存在**：有批次在跑，或上次异常退出。确认无在跑批次后删除 `data/.runner.lock`。
- **CAPTCHA 反复出现**：正常，执行器会自动重试；连续失败说明 IP 被限流，停几小时再跑，不要去掉 `--headless`。
- **某条任务缺 trace.zip**：说明 MCP 配置被改过（没用 0.0.64 或没加 `--save-trace`），按 QA1.md 修正后重跑该条。

# 数据格式契约

本页是阶段1交付数据的**唯一格式权威**。转换管线、质检页以本页为准；
格式有任何变更，24 小时内更新本页并同步。最后更新：2026-07-22。

## 1. 目录契约

每批交付 = `data/` 下一个 `mapping.jsonl` + 每个任务一个标准目录：

```
data/
├── mapping.jsonl                    # 批次台账（见 §5）
└── <task_id>/                       # 严格 5 项，无其他文件
    ├── task.json                    # 任务定义副本（见 §2）
    ├── result.json                  # Agent 抽取结果（见 §3）
    ├── wire.jsonl                   # Agent 完整轨迹（见 §4）
    ├── trace.zip                    # Playwright 浏览器侧轨迹（见 §6）
    └── screenshots/
        └── <task_id>_profile.png    # 作者主页整页截图（fullPage，离屏渲染）
```

注意：
- 执行日志**不在**交付目录内（在项目根 `logs/<task_id>.log`，仅供调试）。
- `data/` 整体不进 Git。
- wire.jsonl 中可能含本机绝对路径（如 `C:\Users\<用户名>`），
  脱敏在转换管线统一做（建议规则：`C:\Users\<用户名>` → `<HOME>`），
  原始轨迹保持原样交付，不脱敏后覆盖。

## 2. task.json

任务定义原样副本，字段：

```json
{"task_id": "task_0001", "person_name": "Geoffrey Hinton", "affiliation_hint": "University of Toronto"}
```

`affiliation_hint` 可能为空字符串（甲方未给单位线索时）。

## 3. result.json

Agent 从作者主页抽取的结构化结果：

```json
{
  "task_id": "task_0001",
  "person_name": "Geoffrey Hinton",
  "affiliation": "Emeritus Prof. Computer Science, University of Toronto",
  "interests": ["machine learning", "psychology", "artificial intelligence",
                "cognitive science", "computer science"],
  "total_citations": 1065520,
  "h_index": 196,
  "i10_index": 435,
  "top_papers": [
    {"title": "Imagenet classification with deep convolutional neural networks", "year": "2012", "citations": 198269},
    {"title": "Deep learning", "year": "2015", "citations": 117079},
    {"title": "Visualizing data using t-SNE", "year": "2008", "citations": 70111}
  ],
  "profile_url": "https://scholar.google.com/citations?user=JicYPdAAAAAJ",
  "status": "success"
}
```

字段规则：
- `status` ∈ `success` / `captcha` / `not_found`。非 success 时其余字段尽力填写，
  可能缺省或为空，并附带 `note` 字段说明情况。
- `total_citations` / `h_index` / `i10_index` / `citations`：**纯整数**
  （无逗号、无单位、非字符串），均取谷歌学术 "All" 列。
- `year`：**字符串**。
- `top_papers`：按被引数降序，最多 3 篇。
- `interests`：作者主页列出的全部研究兴趣标签，顺序与页面一致。

## 4. wire.jsonl（Agent 轨迹）

Kimi Code 会话的原始 wire 文件原样复制，一行一个 JSON 事件。
事件类型清单（以 task_0001 实测为示例）：

| 事件 type | 含义 | 配对关系（断档判据） |
|---|---|---|
| `metadata` | 协议版本等，首行 | — |
| `config.update` | system prompt、模型配置 | — |
| `turn.prompt` | 用户任务 prompt | — |
| `llm.request` | 一次模型请求 | 与 `step.end` 数量一致 |
| `context.append_loop_event`/`step.begin` | 步骤开始 | 与 `step.end` 一一配对 |
| `context.append_loop_event`/`content.part` | 模型输出（thinking / text） | — |
| `context.append_loop_event`/`tool.call` | 工具调用（含 browser_*） | 与 `tool.result` 一一配对 |
| `context.append_loop_event`/`tool.result` | 工具返回 | 与 `tool.call` 一一配对 |
| `context.append_loop_event`/`step.end` | 步骤结束 | 与 `step.begin` 一一配对 |
| `usage.record` | token 用量 | 与 `llm.request` 数量一致 |
| `mcp.tools_discovered` / `llm.tools_snapshot` | MCP 工具清单 | — |
| `context.append_message` | 消息落盘 | — |

浏览器类工具调用名形如 `mcp__playwright__browser_navigate` /
`browser_click` / `browser_type` / `browser_snapshot` / `browser_take_screenshot`，
每个都是独立的 `tool.call` + `tool.result`，构成"状态→动作→新状态"序列。

**轨迹断档红线**：`tool.call↔tool.result`、`step.begin↔step.end` 不配对的
样本即断档。`qa/validate_data.py` 会自动检出并以 issue 报出（exit 1），
不静默丢弃；转换管线侧请按"模型请求数（`llm.request`）与重放消息数"
再做一次对齐，对不上的样本记录原因后剔除，原因写入该样本 meta。

## 5. mapping.jsonl（批次台账）

每行一条执行记录，统一 schema：

```json
{
  "task_id": "task_0001",
  "person_name": "Geoffrey Hinton",
  "framework": "kimi-code",
  "model": "kimi-for-coding/k3",
  "session_id": "session_96e709e0-6c37-4a0d-9200-279fec5cb54d",
  "start_time": "2026-07-21T14:23:32.914672+00:00",
  "end_time": "2026-07-21T14:26:08.033439+00:00",
  "duration_seconds": 155.12,
  "returncode": 0,
  "status": "success",
  "has_result": true,
  "has_screenshot": true,
  "has_trace": true,
  "trajectory_collected": true,
  "collected_at": "2026-07-21T14:26:09.039967+00:00"
}
```

读取规则：
- 同一 `task_id` 可能有多条（CAPTCHA 重试、补跑），**按时间取最后一条
  `status=success` 且 `has_*` 全 true 的**作为有效样本来源，其余为执行历史。
- 训练样本 meta 所需字段直接取自这里：`task_id`、`session_id`、
  `agent`（=`framework`）、`source`（= `data/<task_id>/wire.jsonl`）、
  `sample_index`（转换时按有效记录顺序编号）。
- 刻意不含 `session_path`（本机用户名隐私）；需要回查原始会话时，
  用 `session_id` 在本机 `~/.kimi-code/sessions/wd_*/<session_id>/` 定位。
- `status` 另有 `captcha` / `not_found` / `no_result` / `invalid_result`，
  均不应进入训练集。

## 6. trace.zip（浏览器侧轨迹）

Playwright 标准 trace 布局，`npx playwright show-trace trace.zip` 可直接回放：

```
trace.zip
├── trace.trace      # 动作序列（每次浏览器操作的 before/after 快照）
├── trace.network    # 网络请求日志
├── trace.stacks     # 调用栈
└── resources/       # 快照引用的页面资源
```

用途：双轨对齐——wire.jsonl 里的每个 `browser_*` tool.call 在 trace 里
有对应动作与页面快照，可按时间顺序建索引（trace 动作带时间戳）。

## 7. 质检入口

```bash
python qa/validate_data.py      # 全部通过 exit 0；任一任务有 issue exit 1
```

检查项：5 项产物齐全、result.json 字段与整数类型、wire.jsonl 行数与
tool_call 数下限、**断档配对检测**（§4）、截图大小、trace.zip 完整性。

# Scholar 轨迹采集流水线 - 使用说明

> **交付数据格式契约见 [FORMAT.md](FORMAT.md)**（目录结构、result.json /
> wire.jsonl / mapping.jsonl / trace.zip 的逐字段定义、断档红线）——
> 下游转换管线以 FORMAT.md 为准，本页是操作手册。

## 项目结构

```
D:\Scholar\
├── tasks/
│   └── tasks.jsonl              # 任务清单（8个人物）
├── scripts/
│   ├── task_prompt_template.md  # Prompt 模板
│   └── run_tasks.py             # 批量执行器（拉起 kimi -p、收归产物、写映射，带并发锁）
├── data/
│   ├── task_0001/               # 每个任务的标准输出目录（严格 5 项）
│   │   ├── task.json            # 任务定义
│   │   ├── result.json          # Agent 抽取结果
│   │   ├── wire.jsonl           # 完整 Agent 轨迹
│   │   ├── trace.zip            # Playwright 浏览器侧轨迹（MCP --save-trace）
│   │   └── screenshots/
│   │       └── task_0001_profile.png  # 整页截图
│   └── mapping.jsonl            # task_id <-> session_id 映射表（统一 schema）
├── logs/                        # 执行日志（非交付物，仅供调试）
└── run_batch.ps1                # 批量执行入口（可选）
```

### Playwright MCP 配置（~/.kimi-code/mcp.json）

环境搭建与版本注意事项详见 [QA1.md](QA1.md)（**必读**：
playwright-mcp 必须锁定 0.0.64，`@latest` 拿不到 trace.zip）。

## 工作流程

### 批量自动执行

直接运行批量执行器（同一时刻只允许一个实例，靠 `data/.runner.lock` 防并发；
每条任务的轨迹收集、trace.zip 打包、截图归档、mapping 写入都由执行器一次完成）：

```bash
python scripts/run_tasks.py --limit 3                  # 前3条
python scripts/run_tasks.py                            # 全部任务
python scripts/run_tasks.py --start-from task_0003     # 从某条开始
python scripts/run_tasks.py --limit 1 --no-delay       # 单条测试（禁用反爬延迟）
```

## 反爬策略

- **任务间延迟**：30-90秒随机（在生成脚本时已加入）
- **CAPTCHA 处理**：
  - Agent 遇到验证码时会在 `result.json` 中标记 `status: "captcha"`
  - 手动重试：等待 2-5 分钟后重新执行该任务
  - 策略优化：首次直接访问作者搜索 URL 可能触发验证码，改走首页搜索框可规避

## 数据产出

### 单任务完整输出（`data/task_XXXX/`）
- ✅ `task.json` - 任务定义副本
- ✅ `result.json` - 结构化抽取结果（姓名、单位、引用数、代表作等）
- ✅ `wire.jsonl` - 完整 Agent 轨迹（Kimi 侧）
- ✅ `trace.zip` - Playwright 浏览器侧轨迹（可用 `npx playwright show-trace` 回放）
- ✅ `screenshots/task_XXXX_profile.png` - 作者主页整页截图

（执行日志写在项目根 `logs/<task_id>.log`，非交付物，仅供调试与解析 session_id。）

### 轨迹内容
每条轨迹包含完整的 Agent 操作序列：
- `browser_navigate` - 页面导航（5次）
- `browser_snapshot` - 读取页面状态（3次）
- `browser_type` - 输入文本（1次）
- `browser_click` - 点击元素（1次）
- `browser_take_screenshot` - 整页截图（1次）
- 每步的思考内容（`content.part`）
- `tool_call` / `tool_result` 完整配对

### 映射表（`data/mapping.jsonl`）
每条记录为统一 schema（手动补录等没有运行指标的场景，
`start_time`/`end_time`/`duration_seconds`/`returncode` 置 null）：
```json
{
  "task_id": "task_0001",
  "person_name": "Geoffrey Hinton",
  "framework": "kimi-code",
  "model": "kimi-for-coding/k3",
  "session_id": "session_5c294b90-...",
  "start_time": "2026-07-21T12:03:30Z",
  "end_time": "2026-07-21T12:09:50Z",
  "duration_seconds": 380.2,
  "returncode": 0,
  "status": "success",
  "has_result": true,
  "has_screenshot": true,
  "has_trace": true,
  "trajectory_collected": true,
  "collected_at": "2026-07-21T12:09:55Z"
}
```

## 质检要点

质检脚本：`python qa/validate_data.py`（全部通过 exit 0，任一 issue exit 1）。

### 自动校验
- ✅ 5 项产物齐全（task.json / result.json / wire.jsonl / trace.zip / screenshots/）
- ✅ `result.json` 格式正确（数值为纯整数）
- ✅ 截图文件存在且大小合理（>50KB）
- ✅ `wire.jsonl` 行数 >50（完整轨迹）
- ✅ **断档检测**：`tool.call`↔`tool.result`、`step.begin`↔`step.end` 配对（质量红线，见 FORMAT.md §4）
- ✅ `trace.zip` 完整性（可打开、含 trace.trace）

### 人工抽检（建议 20%）
- 截图内容是否为目标作者主页
- 抽取的引用数与截图是否一致
- 作者单位是否与 `affiliation_hint` 匹配

## 已验证的数据样本

### task_0001: Geoffrey Hinton ✅
- 状态：success
- 轨迹：163 行，19 次请求
- 截图：290KB
- 引用数：1,065,520
- 代表作：
  1. "Imagenet classification..." (2012) - 198,269 引用
  2. "Deep learning" (2015) - 117,079 引用
  3. "Visualizing data using t-SNE" (2008) - 70,111 引用

## 下一步工作

### 完成第一阶段产出
1. 执行剩余 7 条任务（task_0002 ~ task_0008）
2. 收集所有轨迹
3. 质检数据（自动 + 人工抽检）
4. 与谭同学对接：验证轨迹格式是否符合转换管线要求

### 待优化项
- [ ] 自动检测 CAPTCHA 并标记（目前靠 Agent 自行判断）
- [ ] 失败任务自动重试机制（目前需手动）
- [ ] 添加进度监控（实时显示任务完成数）

## 技术细节

### Kimi Code 会话目录结构
```
~/.kimi-code/sessions/
└── wd_scholar_XXXXX/
    └── session_UUID/
        └── agents/
            └── main/
                ├── wire.jsonl          # 完整轨迹
                └── blobs/              # 大对象存储
```


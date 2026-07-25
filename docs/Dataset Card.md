# Dataset Card:谷歌学术人物检索 Agent 轨迹数据集

> 版本 v0.1(初稿)| 维护:谭 | 最后更新:2026-07-25

## 1. 概述

本数据集包含 LLM Agent 在浏览器环境中执行"谷歌学术人物检索"任务的完整交互
轨迹,用于 Agent 模型训练(SFT)。每条样本为一次成功任务执行的完整对话,含
模型思考(reasoning)、工具调用与工具返回,构成"状态→动作→新状态"序列。

## 2. 数据来源

- **任务**:给定人物姓名(及单位线索),Agent 使用 playwright-mcp 浏览器工具
  在谷歌学术检索该人物,进入作者主页,抽取信息并整页截图;
- **执行框架**:Kimi Code CLI(模型 kimi-for-coding/k3);
- **浏览器**:Playwright + 无头 Chrome,经 playwright-mcp 调用;
- **原始轨迹**:Kimi Code wire 文件(事件流),`data/<task_id>/wire.jsonl`。

## 3. 数据格式

JSONL,每行一个样本:`{"messages": [...], "meta": {...}}`

### messages(OpenAI 格式)

| role      | 说明                                                 |
|-----------|----------------------------------------------------|
| system    | Agent 系统提示                                         |
| user      | 任务指令                                               |
| assistant | 模型输出;`reasoning_content`=思考(明文版);`tool_calls`=工具调用 |
| tool      | 工具返回;`tool_call_id` 与 assistant 的 tool_calls 配对且相邻 |

### meta 字段

| 字段                                                | 含义                | 来源                              |
|---------------------------------------------------|-------------------|---------------------------------|
| task_id                                           | 任务编号              | mapping.jsonl                   |
| session_id                                        | 执行会话 ID           | result.json#_run(缺失时回查 mapping) |
| agent / model                                     | 执行框架 / 模型         | 同上                              |
| source                                            | 原始 wire 路径        | data/<task_id>/wire.jsonl       |
| sample_index                                      | 样本序号              | 转换时编号                           |
| status                                            | 任务状态(success 才入库) | result.json                     |
| run_info_from                                     | 执行信息来源(追溯用)       | 管线标注                            |
| llm_request_count / assistant_msg_count / aligned | 断档对齐结果(契约§4)      | 管线计算                            |

## 4. 加工流程

```
wire.jsonl(原始事件流,原样保留、未脱敏)
  → 重组为 OpenAI messages(scripts/wire2messages.py)
  → 脱敏(本机用户路径 → <HOME>)
  → 断档对齐(llm.request 数 vs assistant 消息数;不等则剔除,留档 dropped.jsonl)
  → 结构校验(qa/validator.py:tool_call 配对、角色序列、meta 完整性)
  → train.jsonl
```

## 5. 质检

- **执行侧**:qa/validate_data.py(产物齐全、断档配对、captcha+截图人工复核提示);
- **样本侧**:qa/validator.py + qa/cli.py(批量校验,拒收报告 reject_report.json);
- **人工**:截图内容抽检(第 2 阶段可视化审查系统承接)。

## 6. 已知缺陷与说明

1. **reasoning_content 为明文版思考**:Kimi 平台对完整推理加密(wire 中
   encrypted 字段不可用),样本中的思考可能短于完整 CoT;
2. **断档样本不入库**:保留于 dropped.jsonl 并记录 drop_reason,不静默丢弃;
3. **脱敏范围**:目前覆盖本机用户路径;发现新泄露模式时更新清洗规则并重跑管线;
4. **数值为采集时点快照**:被引数等指标随时间变化,不保证与当前页面一致;
5. **规模**:当前 3 条样本(持续扩充中)。

## 7. 伦理与合规

- 数据来自谷歌学术公开页面,仅采集公开学术信息(姓名、单位、引用数等);
- 不含个人敏感信息;本机路径已脱敏;CAPTCHA 触发时任务即中止,未绕过任何验证;
- 仅供学术研究使用。
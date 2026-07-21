# UI-Agent：大模型 Agent 轨迹数据采集流水线

谷歌学术人物检索任务的 Agent 轨迹采集系统，用于生成 SFT/RL 训练数据。

## 项目目标

让 LLM Agent（Claude Code / Kimi Code）通过 MCP 协议操控浏览器，完成"谷歌学术搜人 → 进作者主页 → 抽取信息 → 整页截图"任务，完整记录 Agent 执行轨迹，供后续转换为训练数据。

## 目录结构

```
tasks/          任务清单（tasks.jsonl）
scripts/        执行与转换脚本
data/           产出数据（不进 Git，仅保留结构）
docs/           格式契约与文档
qa/             质检工具与审查系统
```

## 技术栈

- **Agent 框架**：Kimi Code
- **浏览器自动化**：Playwright（通过 playwright-mcp）
- **协议**：MCP（Model Context Protocol）
- **训练数据格式**：OpenAI messages 格式（JSONL）

## 快速开始

（待补充：环境配置、执行命令）

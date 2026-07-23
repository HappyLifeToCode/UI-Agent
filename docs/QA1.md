# 环境搭建与版本注意事项

## 依赖清单

- **Node.js**（≥18，playwright-mcp 要求）
- **Python 3.8+**（执行器/收集器脚本，Windows 下建议 Anaconda 环境）
- **Kimi Code CLI**（默认路径 `~/.kimi-code/bin/kimi.exe`）
- **playwright-mcp**（通过 npx 运行，无需全局安装）

## MCP 配置（~/.kimi-code/mcp.json）

```json
{
  "mcpServers": {
    "playwright": {
      "command": "cmd",
      "args": ["/c", "npx", "-y","@playwright/mcp@0.0.64",
               "--headless", "--save-trace",
               "--output-dir", "D:/Scholar/.playwright-mcp",
               "--config", "D:/Scholar/scripts/playwright_mcp_config.json",
               "--init-script", "D:/Scholar/scripts/stealth_init.js"] -> 路径要根据自己的项目调整
    }
  }
}
```

改完配置后需要重启 Kimi Code 会话才生效（MCP server 随会话启动）。

### 反检测配置（反爬三件套在 MCP 侧的落地）

浏览器由 playwright-mcp（Node 进程）启动，执行器碰不到浏览器实例，
所以反检测必须打在 MCP 启动参数上（已实测 0.0.64 支持以下两个参数）：

- **`--config scripts/playwright_mcp_config.json`**：浏览器启动与上下文配置——
  `launchOptions.args` 含 `--disable-blink-features=AutomationControlled`
  （禁用 Blink 自动化特征）、`--no-sandbox`、`--disable-setuid-sandbox`；
  `contextOptions` 固定 UA（Chrome/124, Windows）、视口 1280x800、
  locale `en-US`、时区 `America/New_York`（与 VPN 出口一致，不一致反而是信号）。
- **`--init-script scripts/stealth_init.js`**：每页面前置注入，抹除高维指纹——
  `navigator.webdriver`、plugins、languages、hardwareConcurrency/deviceMemory、
  WebGL 渲染器（headless 默认报 SwiftShader，是强 headless 信号）、
  permissions.query 的 notifications 分支。
  注：Python 的 playwright-stealth 库用不上（浏览器在 Node 侧），
  这个脚本是它在 MCP 链路上的等价实现。
- **行为拟人化**在 `scripts/task_prompt_template.md`「行为拟人化」节：
  强制 Agent 跳转/点击前 `browser_wait_for` 2~5 秒、滚动页面、悬停后点击；
  任务间 30-90 秒随机延迟由执行器控制（[M9]）。

`run_tasks.py` 跑批前会自动自检上述配置（`check_mcp_config`，只警告不阻断）。
UA 有改动时必须同步改 stealth_init.js 里的 WebGL/平台指纹，保持自洽。

## 版本注意事项（重要）

### playwright-mcp 必须锁定 0.0.64，不要用 @latest

- **`--save-trace` 在 0.0.65 起被官方移除**。用 `@latest` 启动则整个会话
  不会保存任何 Playwright trace，`data/<task_id>/trace.zip` 这一契约产物
  将永远缺失。已逐版本实测：0.0.64 是最后一个带 `--save-trace` 的版本。
- 0.0.64 的 `--save-trace` 落盘的是**裸 trace 文件**
  （`.playwright-mcp/traces/trace-<时间戳>.trace/.network/.stacks` +
  `resources/`），**不是 zip**。契约要求的 `trace.zip` 由
  `scripts/run_tasks.py` 在任务结束后按 Playwright 标准布局打包生成，
  可用 `npx playwright show-trace data/<task_id>/trace.zip` 回放。

### `--headless` 与反爬

- 方案要求无头离屏渲染（与窗口焦点/分辨率无关，可批量复现），
  故配置里固定 `--headless`。
- 代价是谷歌学术对 headless 的识别率略高，实测单条约 8 条任务中偶有
  一次 CAPTCHA。执行器已内置 CAPTCHA 自动重试（等 2-5 分钟重跑，
  默认最多 2 次），不要轻易去掉 `--headless` 换 headed。

## 执行器并发约束

- 同一时刻只允许一个执行器实例（`data/.runner.lock`）。并发跑批会导致
  同 IP 被谷歌限流、session 归属错乱（mapping.jsonl 里曾出现两条任务
  共用同一 session_id 的事故）。若上次异常退出残留锁文件，确认无在跑
  批次后手动删除即可。

## 首次搭建验证

```bash
python scripts/run_tasks.py --limit 1 --no-delay
```

通过后检查 `data/task_0001/` 应严格包含 5 项：`task.json`、`result.json`、
`wire.jsonl`、`trace.zip`、`screenshots/task_0001_profile.png`。
（执行日志在项目根 `logs/<task_id>.log`，不属于交付目录。）

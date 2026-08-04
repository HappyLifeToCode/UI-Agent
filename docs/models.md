# 多模型支持

本项目默认使用 Kimi K3 模型，但也支持其他 OpenAI 兼容接口的模型。已验证通过的模型如下。

## 已验证模型

| 模型 | 提供商 | 接口类型 | 状态 | 验证日期 | 验证人 |
|---|---|---|---|---|---|
| `kimi-for-coding/k3` | Kimi | Anthropic 兼容 | ✅ 默认 | 2026-07-21 | 同组 |
| `Qwen/Qwen3.5-27B` | 硅基流动 (SiliconFlow) | OpenAI 兼容 | ✅ 通过 | 2026-07-23 | Ye |
| `Qwen/Qwen3.6-27B` | 硅基流动 (SiliconFlow) | OpenAI 兼容 | ✅ 推荐 | 2026-08-02 | Ye |

## Qwen3.6-27B 验证结果（推荐）

2026-08-02 用 Qwen3.6-27B + Semantic Scholar 跑通 3 个任务，全部 success：

| 任务 | 人物 | 论文核查 | 备注 |
|---|---|---|---|
| task_0001 | Buzhou Tang | 10/10 matched | Semantic Scholar 完全可用 |
| task_0002 | Yann LeCun | 10/10 matched | 484,611 引用 |
| task_0003 | Yoshua Bengio | 10/10 matched | 1,128,235 引用 |

### 相比 Qwen3.5-27B 的关键改进

| | Qwen3.5-27B | Qwen3.6-27B |
|---|---|---|
| 模型代际 | 3.5 | 3.6（更新） |
| 论文核查源 | OpenAlex（每日配额限制） | Semantic Scholar（无日配额） |
| 截图模式 | fullPage=true（全页滚动） | fullPage=false（仅首屏，省上下文） |
| 上下文策略 | 256K + reserved 16K | 256K + reserved 32K + thinking=medium |
| 长任务稳定性 | 偶有 429 compaction | 3/3 一次跑通 |
| 单任务完成率 | 需要多次重试 | 首轮即成功 |

### Qwen3.6-27B 推荐配置

```toml
[models."qwen-maas/Qwen/Qwen3.6-27B"]
provider = "qwen-maas"
model = "Qwen/Qwen3.6-27B"
max_context_size = 262144
max_output_size = 8192
capabilities = ["image_in", "thinking", "tool_use"]
display_name = "Qwen3.6 27B"

[loop_control]
reserved_context_size = 32768

[thinking]
enabled = true
effort = "medium"
```

与 Qwen3.5-27B 的关键差异：
- `reserved_context_size=32768`（提前压缩，compaction 触发阈值=256K-32K=224K，避免上下文太满时压缩请求 429）
- `thinking=medium`（减少 Chain-of-Thought token 消耗，降低上下文增速）
- `max_output_size=8192`（限制单次输出大小。实测单轮输出很少超过 8K，但设为 16K 时模型偶有"填充式废话"占据宝贵上下文，设小反而紧凑）

## 配置方法

### Kimi Code 的 config.toml

在 `~/.kimi-code/config.toml` 中新增 provider 和 model（不删除原有 Kimi 配置）：

```toml
# 新增：硅基流动 Qwen 模型
[providers.qwen-maas]
type = "openai"
api_key = "你的硅基流动 API Key"
base_url = "https://api.siliconflow.cn/v1"

[models."qwen-maas/Qwen/Qwen3.5-27B"](Qwen3.6-27B同理)
provider = "qwen-maas"
model = "Qwen/Qwen3.5-27B"
max_context_size = 262144
max_output_size = 8192
capabilities = ["image_in", "thinking", "tool_use"]
display_name = "Qwen3.5 27B"

[loop_control]
reserved_context_size = 16384
```

切换模型只需改第一行 `default_model`：

```toml
# 使用 Qwen
default_model = "qwen-maas/Qwen/Qwen3.5-27B"

# 使用 K3（默认）
default_model = "kimi-for-coding/k3"
```

改完**重启 Kimi Code** 生效。

完整配置模板见 `scripts/config_qwen_example.toml`。

### 其他 OpenAI 兼容模型

任何支持 OpenAI Chat Completions API 的提供商均可按上述格式添加，需修改：
- `type = "openai"`
- `base_url`：提供商的 API 地址
- `api_key`：对应的 API Key
- `model`：模型 ID

## 注意事项

1. **小模型上下文配置（必须，否则任务必败）**：`max_context_size` 按模型真实
   上下文填写（Qwen3.5-27B 为 256K，填 131072 会白白浪费一半预算）；
   同时必须把 `[loop_control] reserved_context_size` 从默认的 50000 调小
   （建议 16384，≥ 2× max_output_size）。kimi-code 在「剩余上下文不足
   reserved_context_size」时触发 compaction（上下文压缩），默认配置下
   131K 窗口输入到 ~81K 就触发；而 compaction 请求一旦被 provider 限流
   （429），整个会话直接中止、任务归零。此项只能用户手动改
   `~/.kimi-code/config.toml`，执行器无法代劳——搭建环境时务必检查。
2. **`_run.model` / mapping 的 model 字段**：执行器记录的是 `--model` 参数或
   环境变量 `AGENT_MODEL` 的值（默认 `kimi-for-coding/k3`），与实际生效模型
   （由 config.toml 的 `default_model` 决定）是两条通道。用小模型跑批时
   【必须】加 `--model "qwen-maas/Qwen/Qwen3.5-27B"`，否则训练数据元信息错标。
3. **429 限流**：硅基流动等 provider 有 RPM 限制，多人共用 API Key 并发测试
   极易触发。执行器已内置 `--max-429-retry`（默认 2 次，冷却 2-5 分钟），
   但频繁 429 说明额度不足，应错峰或升级套餐。
4. **截图命名**：Qwen 在非 success 场景下偶有截图文件名与 prompt 要求不一致（如 `_captcha.png` 而非 `_profile.png`），脚本的 `collect_browser_artifacts` 有兜底匹配逻辑。
5. **超时设置**：单任务超时已调整为 45 分钟（`run_tasks.py` 的
   `subprocess.run timeout=2700`）。小模型在长列表页上可能陷入
   「提取不完整 → 重新提取」的循环，超时是最后一道防线。
6. **Google Scholar CAPTCHA 绕过策略（成功率极高）**：
   - 将 `playwright_mcp_config.json` 的 `headless` 改为 `false`（有头模式）
   - 同时确认 `mcp.json` 的 args 中**没有** `--headless` 参数
   - 启动任务后浏览器窗口弹出，遇到 reCAPTCHA 时手动点掉
   - prompt 模板已内置：有头模式下 Agent 会每 15 秒检查一次页面状态，
     等待 60 秒让用户完成验证，验证通过后自动继续任务
   - 手动过完 CAPTCHA 后无需等待整个任务跑完——后续步骤（进入主页、提取数据）
     Google Scholar 不再拦截，此时可切换到无头模式让 Agent 自行完成

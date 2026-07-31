# 多模型支持

本项目默认使用 Kimi K3 模型，但也支持其他 OpenAI 兼容接口的模型。已验证通过的模型如下。

## 已验证模型

| 模型 | 提供商 | 接口类型 | 状态 | 验证日期 | 验证人 |
|---|---|---|---|---|---|
| `kimi-for-coding/k3` | Kimi | Anthropic 兼容 | ✅ 默认 | 2026-07-21 | 同组 |
| `Qwen/Qwen3.5-27B` | 硅基流动 (SiliconFlow) | OpenAI 兼容 | ✅ 通过 | 2026-07-23 | Ye |

## Qwen3.5-27B 验证结果

在 3 个谷歌学术人物检索任务上，Qwen3.5-27B 与 K3 对比：

| 指标 | K3（参考） | Qwen3.5-27B |
|---|---|---|
| task_0001 Geoffrey Hinton | 386s | 271s |
| task_0002 Yann LeCun | 264s | 219s |
| task_0003 Yoshua Bengio | 274s | 408s |
| 数据准确度 | ✅ | ✅（与 K3 一致） |
| 反检测兼容 | ✅ | ✅ |
| 截图命名合规 | — | ⚠️ 偶有不一致（见下方注意事项） |

## 配置方法

### Kimi Code 的 config.toml

在 `~/.kimi-code/config.toml` 中新增 provider 和 model（不删除原有 Kimi 配置）：

```toml
# 新增：硅基流动 Qwen 模型
[providers.qwen-maas]
type = "openai"
api_key = "你的硅基流动 API Key"
base_url = "https://api.siliconflow.cn/v1"

[models."qwen-maas/Qwen/Qwen3.5-27B"]
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

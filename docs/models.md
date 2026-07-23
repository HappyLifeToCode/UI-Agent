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
max_context_size = 131072
max_output_size = 8192
capabilities = ["image_in", "thinking", "tool_use"]
display_name = "Qwen3.5 27B"
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

1. **`_run.model` 字段**：目前执行器脚本中 `MODEL` 常量写死为 `"kimi-for-coding/k3"`，使用其他模型时 `mapping.jsonl` 和 `result.json` 的 `_run.model` 仍显示该值。这只是日志标签，不影响实际使用的模型。后续版本会改为动态检测。
2. **截图命名**：Qwen 在非 success 场景下偶有截图文件名与 prompt 要求不一致（如 `_captcha.png` 而非 `_profile.png`），脚本的 `collect_browser_artifacts` 有兜底匹配逻辑。
3. **超时设置**：小模型可能响应较慢，必要时调大 `subprocess.run` 的 `timeout` 参数。

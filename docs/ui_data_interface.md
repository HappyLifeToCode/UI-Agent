# 审查/演示系统 数据接口约定

> 版本 v0.1 | 维护:谭 | 最后更新:2026-08-04
> 面向第二阶段可视化审查系统与演示系统的数据读取约定。接口保持稳定,
> 有任何变更 24 小时内更新本页并群内同步。上游格式以 docs/FORMAT.md(契约)为最终权威,
> 本页是"怎么用"的索引,字段定义以契约为准。

## 1. 目录总览

```
data/
├── mapping.jsonl            # 批次台账:task_id ↔ session_id ↔ 框架/模型(§2)
├── <task_id>/               # 任务目录,task_XXXX 为批处理,web_XXXX 为演示现场采集
│   ├── task.json            # 任务定义副本
│   ├── result.json          # 抽取结果(契约 §3)
│   ├── wire.jsonl           # Agent 原始轨迹(契约 §4)
│   ├── trace.zip            # Playwright 浏览器侧轨迹(契约 §6)
│   ├── screenshots/         # 契约截图(§4)
│   ├── alignment.json       # 动作索引(§5)
│   ├── frames.json + frames/# 每动作画面帧(§6)
│   ├── replay.html          # 自包含轨迹回放页(§7)
│   └── review.json          # 审查结论(docs/review_schema.md)
```

注意:`web_*` 目录是演示系统现场采集产物,**不进训练集**;训练与质检相关脚本
只处理 `task_*` 前缀目录。

## 2. 任务列表页字段口径(任务书要求逐项对应)

| 页面字段 | 数据源 | 说明 |
|---|---|---|
| 任务 ID / 人物 | mapping.jsonl `task_id` / `person_name` | |
| 状态 | mapping.jsonl `status` | **按 task_id 取 start_time 最新一行**为最终状态(任务书口径);success/failed/captcha/not_found |
| 框架 | mapping.jsonl `framework` | 如 kimi-code |
| 模型 | mapping.jsonl `model` | 如 kimi-for-coding/k3、Qwen/Qwen3.6-27B |
| 耗时 | mapping.jsonl `duration_seconds` | 秒,浮点 |
| 步数 | `<task_id>/alignment.json` 的 `action_count` | 浏览器动作数;mapping 无此字段,从 alignment 补 |
| 失败原因 | mapping.jsonl `failure_reason` | 可为 null |
| 质检状态 | `<task_id>/review.json` 的 `review_status` | 文件缺失 = 待质检(pending),见 review_schema.md |

mapping.jsonl 完整字段:task_id / person_name / framework / model / session_id /
start_time / end_time / duration_seconds / returncode / status / failure_reason /
has_result / has_screenshot / has_trace / trajectory_collected / collected_at。
追加写,同一 task 可能多行(重跑),**取最新行**。

## 3. result.json(回放页/结果展示用)

字段定义见契约 §3,不重复。要点:作者信息 + top_papers + recent_papers
(rank/title/year/gs_citations/match_status/openalex_* 或 s2_*/doi/journal/
screenshot)+ status + note + `_run`(执行器补写的运行信息:session_id/model/
start_time 等)。核查字段存在 openalex_* 与 s2_* 两种变体,读取时按键是否存在判断。

## 4. screenshots/(契约截图)

`<task_id>_profile.png`(作者主页整页)+ `<task_id>_paper_NN.png`
(NN = recent_papers 的 rank,两位数字;not_found 篇目为搜索结果页留证)。
fullPage 整页,清晰度高,适合展示大图。

## 5. alignment.json(动作索引,回放页主索引)

由 `scripts/build_alignment.py` 生成。顶层:task_id / source / action_count /
screenshot_count / trace_zip / orphan_tool_calls(应为空)/ actions[]。

actions[] 每项:

| 字段 | 含义 |
|---|---|
| seq | 动作序号(1 起,回放按此顺序) |
| tool | 工具名(去掉 mcp__playwright__browser_ 前缀) |
| tool_call_id | wire 中 call/result 配对 ID |
| wire_line / result_line | 调用/返回在 wire.jsonl 的行号 |
| args | 关键参数(url/filename/ref/text 等) |
| result_summary | 返回文本摘要(≤200 字符) |
| has_image | 返回是否带截图图片 |
| screenshot / screenshot_exists | 仅截图动作:契约 PNG 文件名及存在性 |

## 6. frames.json + frames/(每动作画面)

由 `scripts/extract_trace_frames.py` 从 trace.zip 提取(screencast 帧按动作
endTime 匹配;空白帧后取 3s 内最充分帧)。frames.json 顶层:task_id / source /
action_count / matched_count / actions[];每项:seq / tool / call_id / method /
frame(文件名,seq_NN_tool.jpeg)/ matched。
帧为视口大小(1280×800)JPEG,位于 frames/ 子目录。
wait_for/snapshot 等不产生浏览器画面的动作沿用它前一动作同一张帧。

## 7. replay.html(自包含回放页)

由 `scripts/build_replay.py` 生成,单文件 HTML(数据内嵌,图片用相对路径
frames/ 与 screenshots/),双击即开。按步展示:思考(reasoning_content)/
输出文本/工具调用(名称+参数)/工具返回,右侧该步画面;左栏步序导航,
支持上一步/下一步/序号跳转/键盘 ←→。

## 8. trace.zip(浏览器侧原始轨迹)

结构见契约 §6。要点:trace.trace 为事件流(before/after 按 callId 配对,
screencast-frame 引用 resources/ 内 JPEG);可用 `npx playwright show-trace`
回放,或用 `scripts/extract_trace_frames.py` 离线提取每动作画面。

## 9. 生成顺序(本地有任务数据即可复现)

```bash
python scripts/build_alignment.py data/<task_id>      # → alignment.json
python scripts/extract_trace_frames.py data/<task_id> # → frames/ + frames.json
python scripts/build_replay.py data/<task_id>         # → replay.html
# 三个脚本均支持参数 data/ 批量;训练样本转换:python scripts/wire2messages.py data/
```

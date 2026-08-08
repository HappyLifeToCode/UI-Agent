# 审查标注 schema(review.json)

> 版本 v0.1 | 维护:谭 | 最后更新:2026-08-08
> 第二阶段可视化审查系统的审查结论格式。与 docs/cleaning_rules.md §3
> 人工复核规则配套:复核表里的每一类复核项,审查后都在这里落结论。

## 1. 文件位置与基本规则

- 每个任务一个:`data/<task_id>/review.json`;
- **文件缺失 = 待质检(pending)**,任务列表页的"待质检"状态由此推导;
- 审查系统在页面上产生的标注由后端写入此文件;离线人工审查也可直接手写;
- 与训练样本的关系:**主训练集(train.jsonl)只应收 approved 的任务**;
  rejected / needs_rerun 的任务样本须从训练集剔除(转换时检查)。

## 2. 状态流转

```
pending(默认,无文件) ──审查──> approved   合格,可入库
                        ├──> rejected   不合格,剔除(原因必填)
                        └──> needs_rerun 内容错误需返工重跑(如论文错配),
                                        重跑后回到 pending
```

## 3. 文件格式

```json
{
  "task_id": "task_0001",
  "review_status": "approved",
  "reviewer": "谭",
  "reviewed_at": "2026-08-04T15:30:00+08:00",
  "issues": [
    {
      "type": "fake_not_found",
      "target": "recent_papers[10]",
      "detail": "OpenAlex 额度耗尽导致的假 not_found,已人工补查更正",
      "resolved": true
    }
  ],
  "notes": "自由备注"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| task_id | string | 与目录名一致 |
| review_status | string | pending / approved / rejected / needs_rerun |
| reviewer | string | 审查人 |
| reviewed_at | string | ISO 8601 时间戳 |
| issues | array | 发现的问题列表,无问题为 `[]`;见 §4 类型枚举 |
| notes | string | 自由备注,可空 |

## 4. issues[].type 枚举(与清洗规则复核表一一对应)

| type | 对应复核项(cleaning_rules §3) | 说明 |
|---|---|---|
| captcha_screenshot | captcha 但有产物 | 截图是验证页而非目标页面 |
| fake_not_found | not_found 篇目留证(非错误页) | 额度耗尽/加载失败造成的假 not_found |
| screenshot_abnormal | 论文截图 / profile 截图 | 截图空白、非目标页面、内容不符 |
| content_mismatch | (内容抽查) | result.json 内容错误,如错配非论文条目 |
| garbled_text | 乱码修复样本 | 编码问题 |
| other | — | 其他,需在 detail 说明 |

issues[].resolved:true 表示问题已在数据侧修复(如假 not_found 已补查更正),
不影响 approved 判定;false 的问题决定了 rejected / needs_rerun。

## 5. 示例:三种终态

```json
{"task_id": "task_0001", "review_status": "approved", "reviewer": "谭",
 "reviewed_at": "2026-08-04T16:00:00+08:00", "issues": [], "notes": "抽检 3 步回放无误"}
```

```json
{"task_id": "task_0002", "review_status": "needs_rerun", "reviewer": "谭",
 "reviewed_at": "2026-08-04T16:05:00+08:00",
 "issues": [{"type": "content_mismatch", "target": "recent_papers[1]",
             "detail": "rank 1 错配为 CVPR 会议论文集条目,非论文", "resolved": false}],
 "notes": "小模型批,返工重查 rank 1"}
```

## 6. 与现有质检的分工

- 自动校验(`qa/validate_data.py` 执行侧、`qa/validator.py` 样本侧)不变,
  在审查**之前**运行,其结果作为审查输入;
- review.json 记录**人**的结论,是入库的最后一道闸;
- 审查页面可按 review_status 过滤任务列表(待质检/合格/剔除/返工)。

## 7. 数据版本与审查时效

- review.json 结论**只对审查当时的数据版本有效**;data/ 不进 Git,审查结论
  存于各人本机,不随仓库共享;
- 上游重跑某任务并下发新数据后,该任务的 review.json 即失效:**重跑方在群里
  同步,审查方删除对应 review.json**,任务回到 pending 重新审查;
- 若后续团队需要共享审查结论,可将 review.json 单独纳入版本管理(不含敏感
  信息),当前有意不提交。

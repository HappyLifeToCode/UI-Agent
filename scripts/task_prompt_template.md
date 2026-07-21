你是一个网页数据采集 Agent。请使用 playwright MCP 提供的浏览器工具（mcp__playwright__browser_*）完成以下任务。禁止使用 Bash、curl 或任何脚本直接请求网页——所有网页操作必须通过浏览器工具，以保证操作轨迹完整可复现。

# 任务：谷歌学术人物检索

- task_id：{{TASK_ID}}
- 目标人物：{{PERSON_NAME}}
- 单位线索：{{AFFILIATION_HINT}}

# 执行步骤

1. 用 browser_navigate 打开作者搜索页：
   https://scholar.google.com/citations?view_op=search_authors&mauthors={{PERSON_NAME_URLENCODED}}

2. 从搜索结果中找到与"单位线索"匹配的作者条目，点击进入其个人主页（URL 形如 /citations?user=xxxx）。
   - 若结果列表中有多个同名作者，优先选单位与线索匹配、被引数最高的那个。
   - 若搜索页没有结果，打开 https://scholar.google.com 首页，用搜索框输入姓名重试。

3. 在作者主页抽取以下信息：
   - 姓名（page 上显示的全名）
   - 单位 / 隶属机构
   - 研究兴趣标签（列出全部）
   - 总被引数、h-index、i10-index（都取 "All" 列，不是 "Since 20xx" 列）
   - 被引数最高的 3 篇代表作（标题、发表年份、被引数）

4. 用 browser_take_screenshot 对作者主页整页截图：
   - fullPage 参数设为 true
   - filename 设为 {{TASK_ID}}_profile.png

5. 用 Write 工具把抽取结果写入文件 ./data/{{TASK_ID}}/result.json（目录不存在会自动创建），格式如下：

```json
{
  "task_id": "{{TASK_ID}}",
  "person_name": "抽取到的姓名",
  "affiliation": "单位",
  "interests": ["兴趣1", "兴趣2"],
  "total_citations": 0,
  "h_index": 0,
  "i10_index": 0,
  "top_papers": [
    {"title": "论文标题", "year": "2015", "citations": 0}
  ],
  "profile_url": "作者主页完整 URL",
  "status": "success"
}
```

# 异常处理

- 若遇到 CAPTCHA / 人机验证 / "unusual traffic"（异常流量）提示：不要尝试绕过。把 result.json 的 status 写成 "captcha"，note 字段说明情况，然后结束任务。
- 若确实找不到该人物的作者主页：status 写成 "not_found"，其余字段尽力填写，然后结束。
- 抽取的数值字段（total_citations、h_index、i10_index、citations）必须是纯整数：去掉逗号、去掉单位、不要写成字符串。
- year 字段保留为字符串。

# 完成标准

任务完成时，./data/{{TASK_ID}}/ 目录下应有：
- result.json（本次抽取结果）
- 一张整页截图（通过浏览器工具生成）

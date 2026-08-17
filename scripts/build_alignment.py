"""wire.jsonl → alignment.json 双轨对齐索引(契约外补充产物)

用途:把 wire 中每个 browser_* 工具调用与它的返回、截图文件对齐,
供第 2 阶段轨迹回放页按序定位动作与截图。

用法:
    python scripts/build_alignment.py data/task_0001   # 单任务
    python scripts/build_alignment.py data/            # 批量

输出:data/<task_id>/alignment.json
"""
import json
import sys
from pathlib import Path

BROWSER_PREFIX = "mcp__playwright__browser_"

# 每种工具提取哪些关键参数(其余参数对回放定位帮助不大,不抄)
# 键为去掉 mcp__playwright__browser_ 前缀后的名字
KEY_ARGS = {
    "navigate": ["url"],
    "click": ["ref", "element"],
    "hover": ["ref", "element"],
    "type": ["ref", "element", "text", "submit"],
    "take_screenshot": ["filename", "fullPage"],
    "evaluate": ["function"],
    "wait_for": ["text", "textGone", "time"],
    "snapshot": [],
    "press_key": ["key"],
    "select_option": ["ref", "element", "values"],
    "tab_list": [],
    "tab_new": ["url"],
    "tab_select": ["index"],
    "tab_close": ["index"],
    "go_back": [],
    "go_forward": [],
    # run_code 的 code 文本用于和 trace 动作做方法级对齐(hover+click 等多
    # 动作组合若只匹配第一个,后续动作会连环错位)
    "run_code": ["code"],
}


def summarize_result(result, limit=200):
    """tool.result 摘要:取文本部分截断;截图另记 has_image"""
    output = result.get("output")
    has_image = False
    if isinstance(output, str):
        text = output
    elif isinstance(output, list):
        parts = []
        for item in output:
            if item.get("type") == "text":
                parts.append(item["text"])
            elif item.get("type") == "image_url":
                has_image = True
        text = "\n".join(parts)
    else:
        text = json.dumps(output, ensure_ascii=False)
    text = " ".join(text.split())  # 压成一行
    return text[:limit], has_image


def build(task_dir):
    """解析单个任务目录,返回 alignment 字典"""
    task_dir = Path(task_dir)
    task_id = task_dir.name
    wire_path = task_dir / "wire.jsonl"

    pending = {}  # toolCallId -> action 条目(等 result 回填)
    actions = []
    session_idx = -1  # 三段式管线的 wire.jsonl 是多会话拼接的,metadata 事件 = 会话边界
    with open(wire_path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev["type"] == "metadata":
                session_idx += 1  # 每个 kimi 会话的 wire 以 metadata 开头
                continue
            if ev["type"] != "context.append_loop_event":
                continue
            e = ev["event"]
            if e["type"] == "tool.call" and e["name"].startswith(BROWSER_PREFIX):
                tool = e["name"][len(BROWSER_PREFIX):]
                key_names = KEY_ARGS.get(tool, [])
                args = {k: v for k, v in e.get("args", {}).items()
                        if k in key_names}
                action = {
                    "seq": len(actions) + 1,
                    "session_idx": max(session_idx, 0),  # 动作属于第几个会话
                    "tool": tool,
                    "tool_call_id": e["toolCallId"],
                    "wire_line": line_no,
                    "args": args,
                    "result_line": None,
                    "result_summary": None,
                    "has_image": False,
                    "screenshot": None,
                    "screenshot_exists": None,
                }
                actions.append(action)
                pending[e["toolCallId"]] = action
            elif e["type"] == "tool.result" and e["toolCallId"] in pending:
                action = pending.pop(e["toolCallId"])
                action["result_line"] = line_no
                summary, has_image = summarize_result(e["result"])
                action["result_summary"] = summary
                action["has_image"] = has_image
                # 截图动作:参数里的 filename ↔ screenshots/ 实文件
                if action["tool"] == "take_screenshot":
                    filename = action["args"].get("filename")
                    if filename:
                        action["screenshot"] = filename
                        action["screenshot_exists"] = (
                            task_dir / "screenshots" / filename).exists()

    orphaned = [a["tool_call_id"] for a in actions if a["result_line"] is None]
    return {
        "task_id": task_id,
        "source": f"data/{task_id}/wire.jsonl",
        "session_count": session_idx + 1,  # 会话数(三段式: 1 个主会话 + N 个核查批)
        "action_count": len(actions),
        "screenshot_count": sum(1 for a in actions if a["screenshot"]),
        "trace_zip": (task_dir / "trace.zip").exists(),
        "orphan_tool_calls": orphaned,  # 有 call 无 result(正常应为空)
        "actions": actions,
    }


if __name__ == "__main__":
    root = Path(sys.argv[1])
    if (root / "wire.jsonl").exists():
        # 单任务模式
        alignment = build(root)
        out = root / "alignment.json"
        out.write_text(json.dumps(alignment, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"生成 {out}: {alignment['action_count']} 个动作,"
              f" {alignment['screenshot_count']} 张截图,"
              f"孤儿调用 {len(alignment['orphan_tool_calls'])} 个")
    else:
        # 批量模式
        for task_dir in sorted(root.iterdir()):
            # 只处理 task_* 目录;web_* 是演示系统产物,不做索引
            if (task_dir.is_dir() and task_dir.name.startswith("task_")
                    and (task_dir / "wire.jsonl").exists()):
                try:
                    alignment = build(task_dir)
                    out = task_dir / "alignment.json"
                    out.write_text(json.dumps(alignment, ensure_ascii=False,
                                              indent=2), encoding="utf-8")
                    warn = (f",孤儿调用 {len(alignment['orphan_tool_calls'])} 个"
                            if alignment["orphan_tool_calls"] else "")
                    shots_missing = [a["screenshot"] for a in alignment["actions"]
                                     if a["screenshot_exists"] is False]
                    if shots_missing:
                        warn += f",截图缺失 {shots_missing}"
                    print(f"✅ {task_dir.name}: {alignment['action_count']} 个动作,"
                          f" {alignment['screenshot_count']} 张截图{warn}")
                except Exception as e:
                    print(f"❌ {task_dir.name}: {e}")

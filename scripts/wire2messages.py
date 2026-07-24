"""wire.jsonl → OpenAI messages 转换器(初版)
用法: python scripts/wire2messages.py data/task_0001
"""
import json
import sys
from pathlib import Path
import re
def sanitize(text):
    """脱敏:本机用户路径 → <HOME>(契约 §1)"""
    if not isinstance(text, str):
        return text
    # Windows: C:\Users\xxx 或 C:/Users/xxx
    text = re.sub(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+", "<HOME>", text)
    # Linux / Mac
    text = re.sub(r"/(?:Users|home)/[^/\s\"']+", "<HOME>", text)
    return text
def sanitize_obj(obj):
    """递归脱敏:把对象里所有字符串都过一遍(防漏)"""
    if isinstance(obj, str):
        return sanitize(obj)
    if isinstance(obj, list):
        return [sanitize_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: sanitize_obj(v) for k, v in obj.items()}
    return obj

def load_events(wire_path):
    """① 解析:逐行读取事件"""
    events = []
    with open(wire_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def extract_system_user(events):
    """②a 提取 system 和 user"""
    system, user = None, None
    for ev in events:
        if ev["type"] == "config.update" and "systemPrompt" in ev:
            system = ev["systemPrompt"]
        elif ev["type"] == "turn.prompt":
            for part in ev["input"]:
                if part.get("type") == "text":
                    user = part["text"]
    return system, user


def extract_tool_content(result):
    """tool.result 的 output 可能是字符串或列表(截图),统一转文本"""
    output = result.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        texts = []
        for item in output:
            if item.get("type") == "text":
                texts.append(item["text"])
            elif item.get("type") == "image_url":
                texts.append("[截图已保存,见 screenshots/]")
        return "\n".join(texts)
    return json.dumps(output, ensure_ascii=False)


def build_dialogue(events):
    """②b 从 loop_event 重组 assistant/tool 消息序列"""
    messages = []
    cur = {"think": [], "text": [], "tool_calls": []}  # 当前 step 的积累

    def flush():
        """把当前 step 的积累落盘为一条 assistant 消息"""
        if not (cur["think"] or cur["text"] or cur["tool_calls"]):
            return
        msg = {"role": "assistant"}
        reasoning = "\n".join(cur["think"]).strip()
        if reasoning:
            msg["reasoning_content"] = reasoning  # 思考内容放这
        text = "".join(cur["text"]).strip()
        msg["content"] = text if text else None
        if cur["tool_calls"]:
            msg["tool_calls"] = cur["tool_calls"]
        messages.append(msg)
        cur["think"], cur["text"], cur["tool_calls"] = [], [], []

    for ev in events:
        if ev["type"] != "context.append_loop_event":
            continue
        e = ev["event"]
        if e["type"] == "content.part":
            part = e["part"]
            if part["type"] == "think" and part.get("think"):
                cur["think"].append(part["think"])
            elif part["type"] == "text":
                cur["text"].append(part["text"])
        elif e["type"] == "tool.call":
            cur["tool_calls"].append({
                "id": e["toolCallId"],
                "type": "function",
                "function": {"name": e["name"],
                             "arguments": json.dumps(e["args"], ensure_ascii=False)},
            })
        elif e["type"] == "tool.result":
            flush()  # 先落盘 assistant(带 tool_calls),再紧跟 tool 消息
            messages.append({
                "role": "tool",
                "tool_call_id": e["toolCallId"],
                "content": extract_tool_content(e["result"]),
            })
        elif e["type"] == "step.end":
            flush()
    flush()
    return messages


def convert(task_dir):
    """③ 组装完整样本"""
    task_dir = Path(task_dir)
    task_id = task_dir.name
    events = load_events(task_dir / "wire.jsonl")
    system, user = extract_system_user(events)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    messages += build_dialogue(events)
    messages = sanitize_obj(messages)
    # meta:新批次的 result.json 里有 _run;旧的没有,会是 None(正常现象)
    result, run, run_source = get_run_info(task_dir, task_id)
    meta = {
        "task_id": task_id,
        "session_id": run.get("session_id"),
        "agent": run.get("framework"),
        "model": run.get("model"),
        "source": f"data/{task_id}/wire.jsonl",
        "sample_index": 0,
        "status": result.get("status"),
        "run_info_from": run_source,  # 记录执行信息来源,便于追溯
    }
    return {"messages": messages, "meta": meta}

def get_run_info(task_dir, task_id):
    """执行信息:优先 result.json 的 _run,缺失时回查 mapping.jsonl"""
    result = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
    run = result.get("_run")
    if run:
        return result, run, "result.json#_run"
    # 兜底:从 mapping 取该 task 最新的 success 行
    mapping_path = task_dir.parent / "mapping.jsonl"
    latest = None
    if mapping_path.exists():
        for line in mapping_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if (row.get("task_id") == task_id
                    and row.get("status") == "success"
                    and not row.get("deprecated")):
                row_time = row.get("start_time") or ""        # None → ""
                latest_time = (latest.get("start_time") or "") if latest else ""
                if latest is None or row_time > latest_time:
                    latest = row
    return result, (latest or {}), "mapping.jsonl" if latest else None


if __name__ == "__main__":
    root = Path(sys.argv[1])
    if (root / "wire.jsonl").exists():
        # 单任务模式:python scripts/wire2messages.py data/task_0001
        sample = convert(root)
        out = root / "sample.json"
        out.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"生成 {out}:共 {len(sample['messages'])} 条消息")
    else:
        # 批量模式:python scripts/wire2messages.py data/
        samples = []
        for task_dir in sorted(root.iterdir()):
            if task_dir.is_dir() and (task_dir / "wire.jsonl").exists():
                try:
                    s = convert(task_dir)
                    samples.append(s)
                    print(f"✅ {task_dir.name}: {len(s['messages'])} 条消息")
                except Exception as e:
                    print(f"❌ {task_dir.name}: {e}")
        out = root / "train.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for s in samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"\n共 {len(samples)} 条样本 → {out}")

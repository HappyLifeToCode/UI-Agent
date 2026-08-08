"""trace.zip 帧提取:为每个浏览器动作找到对应画面(第二阶段回放页"图片回捞")

原理(2026-08-04 侦察结论,详见 AGENTS.md 第二阶段一节):
- trace.trace 里 before/after 事件按 callId 配对,带 class/method/params/
  startTime/endTime;screencast-frame 事件引用 resources/ 里的 JPEG 帧
  (带时间戳),整个会话被逐帧记录;
- 动作的对应画面 = 下一个动作开始前最近的一帧(动作生效后的稳定页面;
  在 endTime 取帧会拿到生效前的旧页面,造成回放画面慢一步);
- wire 动作 ↔ trace 动作按顺序匹配:工具名 → class.method 兼容表
  (实测自 task_0001);take_screenshot 用 params.path==filename 做强校验;
- snapshot/tab_list 等纯读取类工具不产生 trace 动作,沿用前一动作的画面。

用法:
    python scripts/extract_trace_frames.py data/task_0001   # 单任务
    python scripts/extract_trace_frames.py data/            # 批量

输出:data/<task_id>/frames/seq_NN_tool.jpeg + frames.json
"""
import json
import sys
import zipfile
from bisect import bisect_right
from pathlib import Path

# wire 工具名 → trace class.method 兼容集合(实测自 task_0001,新工具出现再补)
COMPAT = {
    "navigate": {"Frame.goto"},
    "click": {"Frame.click"},
    "hover": {"Frame.hover"},
    "type": {"Frame.fill", "Frame.press", "Frame.type",
             "Keyboard.press", "Keyboard.type", "Page.keyboardPress"},
    "take_screenshot": {"Page.screenshot"},
    "evaluate": {"Frame.evaluateExpression", "Frame.evaluate"},
    # run_code 执行任意 Playwright 代码,可能产生任何方法的动作(实测有点击):
    # 用 None 作通配,匹配下一个 trace 动作(内部噪声除外)
    "run_code": None,
}
# mcp 内部动作,不对应任何 wire 工具,匹配时跳过
INTERNAL = {"Page.requests", "Page.waitForEventInfo"}
# 不产生 trace 动作的工具:沿用前一帧。
# wait_for(time) 实测不产生 before/after 事件;snapshot/tab_* 是纯读取
NO_TRACE = {"snapshot", "tab_list", "tab_select", "tab_close", "wait_for"}

# run_code 代码文本 → 可解释的 trace 方法(用于吃掉一次 run_code 产生的
# 全部连续 trace 动作,如 hover+waitForTimeout+click 组合)。
# 方法名为 trace.trace 里的真实 class.method(实测自 task_0001 的 trace.zip)
RUN_CODE_METHODS = [
    ("page.goto", "Frame.goto"),
    (".click(", "Frame.click"),
    (".hover(", "Frame.hover"),
    (".fill(", "Frame.fill"),
    ("keyboard.press", "Page.keyboardPress"),
    ("keyboard.type", "Page.keyboardType"),
    ("page.evaluate", "Frame.evaluateExpression"),
    ("page.evaluate", "Frame.evaluate"),
    ("waitForTimeout", "Frame.waitForTimeout"),
    ("waitForSelector", "Frame.waitForSelector"),
    ("page.reload", "Page.reload"),
]


def code_allowed_methods(code: str) -> set:
    """从 run_code 的代码文本推出它可能产生的 trace 方法集合。"""
    return {m for key, m in RUN_CODE_METHODS if key in code}


def key_ok(tool, wa_args, ta_params):
    """强校验:参数层面确认 wire 动作与 trace 动作是同一个(防错位)"""
    if tool == "navigate":
        return ta_params.get("url") == wa_args.get("url")
    if tool in ("click", "hover"):
        ref = wa_args.get("ref", "")
        sel = ta_params.get("selector", "")
        if "aria-ref=" not in sel:
            return True  # role 选择器等兜底写法无法核对,按顺序采信
        return bool(ref) and f"aria-ref={ref}" in sel
    if tool == "evaluate":
        fn = (wa_args.get("function") or "")[:40]
        return bool(fn) and fn in ta_params.get("expression", "")
    return True  # take_screenshot / run_code 等:顺序匹配


def parse_trace(zip_path):
    """解析 trace.zip,返回 (actions, frames)
    actions: [{call_id, method, params, start, end}],按 startTime 排序
    frames:  (timestamps 升序列表, sha1 列表)"""
    before = {}
    after = {}
    frames = []
    with zipfile.ZipFile(zip_path) as z:
        with z.open("trace.trace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "before":
                    before[ev["callId"]] = ev
                elif t == "after":
                    after[ev["callId"]] = ev
                elif t == "screencast-frame":
                    frames.append((ev["timestamp"], ev["sha1"]))
    actions = []
    for cid, b in before.items():
        a = after.get(cid, {})
        actions.append({
            "call_id": cid,
            "method": f"{b.get('class')}.{b.get('method')}",
            "params": b.get("params") or {},
            "start": b.get("startTime", 0),
            "end": a.get("endTime", b.get("startTime", 0)),
        })
    actions.sort(key=lambda x: x["start"])
    frames.sort()
    return actions, ([t for t, _ in frames], [s for _, s in frames])


def frame_at(frames, ts, z=None, lookahead_ms=3000):
    """ts 之前最近的一帧;没有则给最早一帧。
    若取到的帧疑似空白(刚跳转未绘制,1280x800 白图 JPEG 约 2.7KB),
    向后看 lookahead_ms 内第一张有内容的帧。"""
    times, names = frames
    if not names:
        return None
    i = bisect_right(times, ts) - 1
    name = names[max(i, 0)]
    if z is not None:
        size = z.getinfo(f"resources/{name}").file_size
        if size < 8000:
            # 窗口内取体积最大的一帧(内容渲染最充分),而不是第一张过阈值的
            best, best_size = None, 0
            for j in range(max(i, 0) + 1, len(times)):
                if times[j] - ts > lookahead_ms:
                    break
                cand_size = z.getinfo(f"resources/{names[j]}").file_size
                if cand_size > best_size:
                    best, best_size = names[j], cand_size
            if best is not None and best_size >= 8000:
                return best
    return name


def match_actions(wire_actions, trace_actions):
    """wire 动作按序匹配 trace 动作,返回 {seq: trace_action|None}"""
    result = {}
    pos = 0  # trace_actions 扫描指针
    for wa in wire_actions:
        tool = wa["tool"]
        if tool in NO_TRACE or tool not in COMPAT:
            result[wa["seq"]] = None
            continue
        compat = COMPAT[tool]
        found = None
        while pos < len(trace_actions):
            ta = trace_actions[pos]
            pos += 1
            if ta["method"] in INTERNAL:
                continue  # mcp 内部动作,跳过(不消耗匹配)
            # compat 为 None 表示通配(run_code 可产生任意动作)
            if compat is None or (ta["method"] in compat
                                  and key_ok(tool, wa["args"], ta["params"])):
                found = ta
                # type 一次调用产生 fill+press 两个 trace 动作,吃掉紧邻的取最后
                # 一个;其他工具(如连续多次 evaluate)不能这么吃
                if tool == "type":
                    while (pos < len(trace_actions)
                           and trace_actions[pos]["method"] in compat):
                        found = trace_actions[pos]
                        pos += 1
                # run_code 一次调用可产生多个 trace 动作(如 hover+click 组合),
                # 只匹配第一个会让后续动作连环错位;吃掉代码能解释的连续动作
                if tool == "run_code":
                    allowed = code_allowed_methods(wa["args"].get("code", ""))
                    if allowed:
                        while (pos < len(trace_actions)
                               and trace_actions[pos]["method"] in allowed):
                            found = trace_actions[pos]
                            pos += 1
                break
        result[wa["seq"]] = found
    return result


def extract(task_dir):
    task_dir = Path(task_dir)
    task_id = task_dir.name
    alignment = json.loads((task_dir / "alignment.json").read_text(encoding="utf-8"))
    trace_actions, frames = parse_trace(task_dir / "trace.zip")
    matched = match_actions(alignment["actions"], trace_actions)

    # 每个动作的稳定时刻 = 下一个匹配到 trace 动作的开始时间
    # （此时浏览器空闲、页面渲染完毕，代表本动作生效后的页面状态）。
    # 直接在动作 endTime 取帧会拿到动作生效前的旧页面(点击/跳转的
    # 视觉变化发生在 endTime 之后),这就是回放"画面慢一步"的来源。
    seqs = [wa["seq"] for wa in alignment["actions"]]
    matched_seqs = [s for s in seqs if matched[s] is not None]
    settle_ts = {}
    for i, s in enumerate(matched_seqs):
        if i + 1 < len(matched_seqs):
            settle_ts[s] = matched[matched_seqs[i + 1]]["start"] - 1
        else:
            settle_ts[s] = float("inf")  # 最后一个动作:取全程最后一帧

    frames_dir = task_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    out_actions = []
    last_frame = None  # NO_TRACE 工具沿用前一帧
    with zipfile.ZipFile(task_dir / "trace.zip") as z:
        for wa in alignment["actions"]:
            ta = matched[wa["seq"]]
            if ta is not None:
                # z=None:稳定帧不做空白前瞻(向前看会越过 settle_ts
                # 拿错下一个动作的帧; settle 语义下空白即真实空白)
                frame_name = frame_at(frames, settle_ts[wa["seq"]])
                last_frame = frame_name or last_frame
            else:
                frame_name = last_frame
            out_name = None
            if frame_name:
                out_name = f"seq_{wa['seq']:02d}_{wa['tool']}.jpeg"
                data = z.read(f"resources/{frame_name}")
                (frames_dir / out_name).write_bytes(data)
            out_actions.append({
                "seq": wa["seq"],
                "tool": wa["tool"],
                "call_id": ta["call_id"] if ta else None,
                "method": ta["method"] if ta else None,
                "frame": out_name,
                "matched": ta is not None,
            })

    n_matched = sum(1 for a in out_actions if a["matched"])
    out = {
        "task_id": task_id,
        "source": f"data/{task_id}/trace.zip",
        "action_count": len(out_actions),
        "matched_count": n_matched,
        "actions": out_actions,
    }
    (task_dir / "frames.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    root = Path(sys.argv[1])
    if (root / "trace.zip").exists():
        # 单任务模式
        out = extract(root)
        print(f"生成 {root/'frames.json'}: {out['action_count']} 个动作,"
              f" {out['matched_count']} 个匹配到 trace 动作")
    else:
        # 批量模式(只处理 task_* 目录)
        for task_dir in sorted(root.iterdir()):
            if (task_dir.is_dir() and task_dir.name.startswith("task_")
                    and (task_dir / "trace.zip").exists()
                    and (task_dir / "alignment.json").exists()):
                try:
                    out = extract(task_dir)
                    print(f"✅ {task_dir.name}: {out['action_count']} 个动作,"
                          f"匹配 {out['matched_count']}")
                except Exception as e:
                    print(f"❌ {task_dir.name}: {e}")

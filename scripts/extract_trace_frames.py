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

多 trace 支持(2026-08-15,三段式管线):核查批会话的 trace 存为
trace_batchNN.zip。本脚本自动按 trace.zip → trace_batch01 → 02… 的顺序
解析并拼接动作列表(与 wire.jsonl 的阶段拼接顺序一致,单扫匹配即可),
帧按时间戳归并;zip 文件本身不合并(callId 跨会话会撞车,不能拼字节)。
老数据只有 trace.zip 时行为与之前完全一致。

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


def find_trace_zips(task_dir):
    """按回放顺序列出任务的 trace 文件:trace.zip(phase1 主会话) +
    trace_batchNN.zip(核查批,按编号升序)。"""
    task_dir = Path(task_dir)
    zips = []
    if (task_dir / "trace.zip").exists():
        zips.append(task_dir / "trace.zip")
    zips += sorted(task_dir.glob("trace_batch*.zip"),
                   key=lambda p: int("".join(c for c in p.stem if c.isdigit()) or 0))
    return zips


def parse_traces(task_dir):
    """解析任务的全部 trace zip,返回 (per_zip, frame_src)

    per_zip:   [(zip文件名, actions, frames)]——每段独立。actions 供按会话
               匹配;frames 是 ((timestamps 升序), (sha1))。注意:trace 内的
               时间戳是【会话内相对毫秒】,不同会话不可比,帧严禁跨 zip 归并
               (实测:归并后 bisect 会拿到别的会话的帧,画面错乱)。
    frame_src: sha1 -> zip 路径(读帧字节用;sha1 按内容寻址,同名即同内容)。
    """
    per_zip = []
    frame_src = {}
    for zp in find_trace_zips(task_dir):
        actions, frames = parse_trace(zp)
        for a in actions:
            a["_zip"] = zp.name  # 记录动作来自哪段 trace(QA 可查)
        per_zip.append((zp.name, actions, frames))
        for name in frames[1]:
            frame_src.setdefault(name, zp)
    return per_zip, frame_src


def zip_screenshot_names(actions):
    """一段 trace 里 Page.screenshot 动作的文件名集合(配对证据)。"""
    return {Path(a["params"].get("path", "")).name
            for a in actions
            if a["method"] == "Page.screenshot" and a["params"].get("path")}


def pair_sessions_to_zips(sessions: dict, per_zip: list):
    """wire 会话 ↔ trace zip 配对,返回 {session_idx: zip 下标或 None}。

    优先按截图文件名重合度配对(抗批次重试错位);但实测 0.0.64 的 trace
    Page.screenshot 事件不含保存路径,此时配对退化为按顺序取第一个空闲
    zip——批次重试(重试会话多一个、zip 被覆盖)的场景会错配,待遇到再增强
    (可用的证据:wire run_code 里的 goto URL ↔ trace goto params.url)。
    """
    zip_used = [False] * len(per_zip)
    zip_shots = [zip_screenshot_names(acts) for _, acts, _ in per_zip]
    pairing = {}
    for si in sorted(sessions):
        want = {wa["args"].get("filename") for wa in sessions[si]
                if wa["tool"] == "take_screenshot" and wa["args"].get("filename")}
        best, best_score = None, -1
        for zi in range(len(per_zip)):
            if zip_used[zi]:
                continue
            score = len(want & zip_shots[zi]) if want else 0
            if score > best_score:
                best, best_score = zi, score
        if best is not None:
            zip_used[best] = True
            pairing[si] = best
        else:
            pairing[si] = None  # 没有可用 trace(如该批 trace 缺失)
    return pairing


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
    """wire 动作按序匹配 trace 动作,返回 {seq: (first, last)|None}

    first = 本次匹配吃掉的第一个 trace 动作,last = 最后一个(二者通常相同;
    type/run_code 的连吃逻辑会让 last 落后于 first)。取画面 settle 时刻必须
    用下一个动作的 first.start——用 last.start 会越过页面加载,拿到下一页的
    画面(实测截图动作帧错页的来源)。"""
    result = {}
    pos = 0  # trace_actions 扫描指针
    for wa in wire_actions:
        tool = wa["tool"]
        if tool in NO_TRACE or tool not in COMPAT:
            result[wa["seq"]] = None
            continue
        compat = COMPAT[tool]
        found = None
        first = None
        while pos < len(trace_actions):
            ta = trace_actions[pos]
            pos += 1
            if ta["method"] in INTERNAL:
                continue  # mcp 内部动作,跳过(不消耗匹配)
            # compat 为 None 表示通配(run_code 可产生任意动作)
            if compat is None or (ta["method"] in compat
                                  and key_ok(tool, wa["args"], ta["params"])):
                found = ta
                first = ta
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
        result[wa["seq"]] = (first, found) if found else None
    return result


def extract(task_dir):
    task_dir = Path(task_dir)
    task_id = task_dir.name
    alignment = json.loads((task_dir / "alignment.json").read_text(encoding="utf-8"))
    zips = find_trace_zips(task_dir)
    per_zip, frame_src = parse_traces(task_dir)

    # wire 按会话分组(老数据无 session_idx 全部归 0),逐会话匹配对应的 trace
    sessions = {}
    for wa in alignment["actions"]:
        sessions.setdefault(wa.get("session_idx", 0), []).append(wa)
    pairing = pair_sessions_to_zips(sessions, per_zip)

    matched = {}     # seq -> (first, last) | None
    settle_ts = {}   # seq -> 时间戳(该会话自己的相对坐标系)
    frames_of = {}   # seq -> 该会话的 frames(取帧只在会话自己的帧序列里找)
    shot_anchor = {}  # seq -> "ok"(锚定) / "missing"(该会话 trace 里截图事件不够)
    for si in sorted(sessions):
        wa_list = sessions[si]
        zi = pairing.get(si)
        trace_acts = per_zip[zi][1] if zi is not None else []
        sess_frames = per_zip[zi][2] if zi is not None else ([], [])
        m = match_actions(wa_list, trace_acts)

        # 截图动作按序锚定:实测 @playwright/mcp@0.0.64 的 Page.screenshot 事件
        # params 不含保存路径(文件由 MCP 服务端自己落盘),无法按文件名强校验;
        # 同一会话内第 i 个 wire 截图 ↔ 第 i 个 trace 截图事件,按序 1:1 锚定。
        # (顺序单扫对截图不可靠:run_code 通配可能吃掉 screenshot 事件)
        shot_events = [a for a in trace_acts if a["method"] == "Page.screenshot"]
        wire_shots = [wa for wa in wa_list if wa["tool"] == "take_screenshot"]
        for wa, ta in zip(wire_shots, shot_events):
            m[wa["seq"]] = (ta, ta)  # (first, last) 同体
            shot_anchor[wa["seq"]] = "ok"
        for wa in wire_shots[len(shot_events):]:
            m[wa["seq"]] = None
            shot_anchor[wa["seq"]] = "missing"
        matched.update(m)

        # 每个动作的稳定时刻 = 会话内下一个匹配动作的【第一个】trace 动作
        # start - 1(此时页面尚未开始变化,代表本动作生效后的稳定画面);
        # 会话内最后一个匹配动作取本会话最后一帧。时间戳跨会话不可比,
        # 严禁跨会话取 settle/帧。
        ms = [wa["seq"] for wa in wa_list if m[wa["seq"]] is not None]
        for i, s in enumerate(ms):
            if i + 1 < len(ms):
                settle_ts[s] = m[ms[i + 1]][0]["start"] - 1
            else:
                settle_ts[s] = float("inf")
        for wa in wa_list:
            frames_of[wa["seq"]] = sess_frames

    frames_dir = task_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    out_actions = []
    last_frame = None  # NO_TRACE 工具沿用前一帧
    # 帧可能来自不同 zip(phase1 / 各核查批),句柄按需打开并缓存
    zip_handles = {}

    def read_frame(name):
        zp = frame_src.get(name)
        if zp is None:
            return None
        if zp not in zip_handles:
            zip_handles[zp] = zipfile.ZipFile(zp)
        return zip_handles[zp].read(f"resources/{name}")

    try:
        for wa in alignment["actions"]:
            pair = matched[wa["seq"]]
            ta = pair[1] if pair else None  # (first, last) 取 last 作为本动作的代表
            if ta is not None:
                # z=None:稳定帧不做空白前瞻(向前看会越过 settle_ts
                # 拿错下一个动作的帧; settle 语义下空白即真实空白)
                frame_name = frame_at(frames_of[wa["seq"]], settle_ts[wa["seq"]])
                last_frame = frame_name or last_frame
            else:
                frame_name = last_frame
            out_name = None
            if frame_name:
                data = read_frame(frame_name)
                if data is not None:
                    out_name = f"seq_{wa['seq']:02d}_{wa['tool']}.jpeg"
                    (frames_dir / out_name).write_bytes(data)
            # 截图动作的校验状态来自按序锚定(trace 事件无文件名可核对,
            # "ok"=同会话内按序锚定成功,"missing"=该会话 trace 截图事件不足)
            verify = shot_anchor.get(wa["seq"])
            out_actions.append({
                "seq": wa["seq"],
                "tool": wa["tool"],
                "call_id": ta["call_id"] if ta else None,
                "method": ta["method"] if ta else None,
                "trace_zip": ta.get("_zip") if ta else None,
                "verify": verify,
                "frame": out_name,
                "matched": ta is not None,
            })
    finally:
        for z in zip_handles.values():
            z.close()

    n_matched = sum(1 for a in out_actions if a["matched"])
    n_mismatch = sum(1 for a in out_actions if a.get("verify") == "missing")
    out = {
        "task_id": task_id,
        "source": [f"data/{task_id}/{zp.name}" for zp in zips],
        "session_count": len(sessions),
        "action_count": len(out_actions),
        "matched_count": n_matched,
        "screenshot_mismatch_count": n_mismatch,
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

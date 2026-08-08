"""生成轨迹回放页(第二阶段核心页的数据侧原型)

每个任务输出一个自包含的 data/<task_id>/replay.html:
按步展示 思考(reasoning_content)/ 输出文本 / 工具调用(名称+参数) / 工具返回,
浏览器动作旁边显示 trace 提取的对应画面(frames/)。
数据源:wire.jsonl(思考与文本)+ alignment.json(动作与返回)+ frames.json(画面)。

前置:先跑 wire2messages 不需要,但需要 alignment.json 和 frames.json:
    python scripts/build_alignment.py data/task_0001
    python scripts/extract_trace_frames.py data/task_0001

用法:
    python scripts/build_replay.py data/task_0001   # 单任务
    python scripts/build_replay.py data/            # 批量
"""
import json
import sys
from pathlib import Path


def build_steps(task_dir):
    """从 wire 按 step 重组:思考/文本/工具调用,再挂上返回摘要与画面"""
    task_dir = Path(task_dir)
    alignment = json.loads((task_dir / "alignment.json").read_text(encoding="utf-8"))
    frames = json.loads((task_dir / "frames.json").read_text(encoding="utf-8"))
    by_call_id = {a["tool_call_id"]: a for a in alignment["actions"]}
    frame_by_seq = {x["seq"]: x["frame"] for x in frames["actions"]}

    steps = []
    cur = None
    with open(task_dir / "wire.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev["type"] != "context.append_loop_event":
                continue
            e = ev["event"]
            if e["type"] == "step.begin":
                cur = {"think": [], "text": [], "calls": []}
            elif e["type"] == "content.part" and cur is not None:
                part = e["part"]
                if part["type"] == "think" and part.get("think"):
                    cur["think"].append(part["think"])
                elif part["type"] == "text" and part.get("text"):
                    cur["text"].append(part["text"])
            elif e["type"] == "tool.call" and cur is not None:
                a = by_call_id.get(e["toolCallId"])
                frame = None
                if a:
                    # 截图动作优先用契约 PNG(fullPage 整页,比视口帧清晰)
                    if a["tool"] == "take_screenshot":
                        png = a["args"].get("filename")
                        if png and (task_dir / "screenshots" / png).exists():
                            frame = "screenshots/" + png
                    if frame is None and frame_by_seq.get(a["seq"]):
                        frame = "frames/" + frame_by_seq[a["seq"]]
                cur["calls"].append({
                    "tool": a["tool"] if a else e["name"],
                    "args": a["args"] if a else e.get("args", {}),
                    "result": (a["result_summary"] if a else "") or "",
                    "frame": frame,
                })
            elif e["type"] == "step.end" and cur is not None:
                steps.append({
                    "no": len(steps) + 1,
                    "reasoning": "\n".join(cur["think"]).strip(),
                    "text": "".join(cur["text"]).strip(),
                    "calls": cur["calls"],
                })
                cur = None
    return steps


def assign_frames(steps):
    """给每步配右侧画面:本步思考时 Agent 正看着的页面。

    步序结构是「思考 → 工具调用 → step.end → 工具返回 → 下一步思考」,
    所以第 N 步的思考评论的是第 N-1 步动作完成后的页面——画面必须取
    上一个浏览器动作的稳定帧,不能取本步动作的结果帧(否则思考永远
    比画面快一步)。规则:
    - 本步有 take_screenshot:直接用契约 PNG(截图不改变页面,PNG 就是
      当前状态,且 fullPage 比视口帧清晰);
    - 本步只有变更类动作:用上一个动作的稳定帧(本步动作的结果属于
      下一步的画面);
    - 首个浏览器动作步(无历史帧):退回用本步动作的稳定帧。
    """
    last = None  # 截至上一步的浏览器稳定画面
    for s in steps:
        png = None
        settled = None
        for c in s["calls"]:
            f = c.get("frame")
            if not f:
                continue
            if f.startswith("screenshots/"):
                png = f
            else:
                settled = f  # 本步最后一个动作的稳定帧
        s["frame"] = png or last or settled
        if settled:
            last = settled
        if png:
            last = png
    return steps


def step_frame(step):
    """本步代表画面:取最后一个有帧的工具调用(已废弃,逻辑见 assign_frames)"""
    for c in reversed(step["calls"]):
        if c["frame"]:
            return c["frame"]
    return None


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>__TITLE__ 轨迹回放</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' fill='%23e60012'/%3E%3Ctext x='16' y='23' font-family='Arial Black' font-size='19' font-weight='900' fill='white' text-anchor='middle'%3ES%3C/text%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--red:#e60012;--red-dark:#b0000e;--ink:#111114;--paper:#f4f2ee;--surface:#fff;
--line:#d8d4cc;--text-2:#6b6560;--ok:#0f7a3d;
--stripe:repeating-linear-gradient(-45deg,var(--red) 0 14px,transparent 14px 28px)}
*{box-sizing:border-box}
html,body{height:100%}
body{font-family:"Inter","PingFang SC","Microsoft YaHei",system-ui,sans-serif;margin:0;
  background:var(--paper);color:var(--ink);-webkit-font-smoothing:antialiased}
.layout{display:flex;height:100vh;overflow:hidden}

/* 左侧:品牌 + 步序(黑) */
#steps{width:280px;flex-shrink:0;background:var(--ink);color:#fff;overflow-y:auto;
  padding:32px 22px 32px;position:relative}
#steps::before{content:"";position:absolute;top:0;left:0;right:0;height:8px;background:var(--stripe)}
.brand .slash{display:inline-block;background:var(--red);color:#fff;font-size:11px;
  font-weight:700;padding:3px 10px;transform:skew(-12deg);letter-spacing:.2em;margin-bottom:10px}
.brand h1{font-family:"Archivo Black",sans-serif;font-size:24px;text-transform:uppercase;line-height:1.1}
.brand h1 em{color:var(--red);font-style:normal}
.brand p{color:#8b8b90;font-size:11.5px;margin-top:8px;line-height:1.6}
.brand a{color:#e8e8ea;font-size:12px;text-decoration:none}
.brand a:hover{text-decoration:underline}
#steps .list{margin-top:20px}
#steps .item{padding:6px 9px;cursor:pointer;font-size:12.5px;margin-bottom:2px;color:#c9c9cf;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-left:2px solid transparent}
#steps .item:hover{background:#1d1d22}
#steps .item.active{background:#1d1d22;border-left-color:var(--red);color:#fff;font-weight:600}

/* 中间:步内容(浅) */
#center{flex:1.05;overflow-y:auto;padding:28px 30px;min-width:400px}
.nav{display:flex;gap:8px;align-items:center;margin-bottom:16px;position:sticky;top:0;
  background:var(--paper);padding:6px 0;z-index:5}
button{cursor:pointer;font-family:inherit;font-size:13px;padding:8px 16px;border:1px solid var(--line);
  background:#fff;clip-path:polygon(0 0,100% 0,100% calc(100% - 8px),calc(100% - 8px) 100%,0 100%)}
button:hover{border-color:var(--ink)}
#pos{font-size:13px;color:var(--text-2);font-weight:600}
input{width:76px;padding:8px;border:1px solid var(--line);font-family:inherit;font-size:13px;outline:none}
input:focus{border-color:var(--red)}
.block{background:var(--surface);border:1px solid var(--line);padding:14px 16px;margin-bottom:12px}
.block h3{margin:0 0 8px;font-size:11px;font-weight:600;color:var(--text-2);letter-spacing:.14em;text-transform:uppercase}
.reasoning{background:#fdf8ec;border-left:3px solid #d8a012}
.text-block{border-left:3px solid var(--red)}
pre{white-space:pre-wrap;word-break:break-all;font-size:13px;margin:0;font-family:inherit;line-height:1.65}
.args{background:#f4f2ee;padding:10px;font-family:Consolas,monospace;font-size:12px}
.toolname{font-family:Consolas,monospace;color:var(--red);font-weight:700}
.result{color:#444;font-size:12.5px}
.empty{color:#999;font-size:13px}
.framepath{font-size:11px;color:#999;margin-top:6px}

/* 右侧:画面 */
#right{flex:1.3;display:flex;align-items:flex-start;justify-content:center;padding:28px;overflow-y:auto}
#right img{max-width:100%;box-shadow:0 2px 16px rgba(17,17,20,.28);background:#fff;cursor:zoom-in}
#overlay{position:fixed;inset:0;background:rgba(17,17,20,.85);display:none;align-items:flex-start;
  justify-content:center;overflow:auto;z-index:20;cursor:zoom-out}
#overlay img{max-width:96%;margin:20px auto;display:block;background:#fff}
</style></head><body>
<div class="layout">
<div id="steps">
  <div class="brand">
    <div class="slash">REPLAY</div>
    <h1>__TITLE__<br><em>轨迹回放</em></h1>
    <p><a id="backlink" href="/review" onclick="goBack();return false;">← 任务列表</a></p>
  </div>
  <div class="list" id="steplist"></div>
</div>
<div id="center">
  <div class="nav">
    <button onclick="go(-1)">← 上一步</button>
    <button onclick="go(1)">下一步 →</button>
    <span id="pos"></span>
    <input id="jump" type="number" min="1" placeholder="跳转" onchange="jumpTo(this.value)">
  </div>
  <div id="content"></div>
</div>
<div id="right"><img id="frame" alt="" onclick="zoom()"></div>
</div>
<div id="overlay" onclick="this.style.display='none'"><img id="ovimg"></div>
<script>
const STEPS = __STEPS__;
let i = 0;
const $ = id => document.getElementById(id);
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function renderList(){
  $('steplist').innerHTML = STEPS.map((s,j)=>{
    const label = s.calls.length ? s.calls.map(c=>c.tool).join(', ') : (s.text?'文本':'思考');
    return `<div class="item" id="it${j}" onclick="jumpTo(${s.no})">${s.no}. ${esc(label)}</div>`;
  }).join('');
}
function show(){
  const s = STEPS[i];
  document.querySelectorAll('.item').forEach(e=>e.classList.remove('active'));
  const it = $('it'+i); if(it){it.classList.add('active');it.scrollIntoView({block:'nearest'});}
  $('pos').textContent = `第 ${s.no} / ${STEPS.length} 步`;
  $('jump').value = s.no;
  let h = '';
  h += s.reasoning
    ? `<div class="block reasoning"><h3>思考</h3><pre>${esc(s.reasoning)}</pre></div>` : '';
  h += s.text
    ? `<div class="block text-block"><h3>输出</h3><pre>${esc(s.text)}</pre></div>` : '';
  for (const c of s.calls){
    h += `<div class="block"><h3>工具调用</h3>
      <div class="toolname">${esc(c.tool)}</div>
      <pre class="args">${esc(JSON.stringify(c.args,null,1))}</pre>
      ${c.result?`<h3 style="margin-top:8px">返回</h3><pre class="result">${esc(c.result)}</pre>`:''}
      ${c.frame?`<div style="font-size:12px;color:#888;margin-top:4px">画面: ${esc(c.frame)}</div>`:''}
    </div>`;
  }
  if (!s.reasoning && !s.text && !s.calls.length) h = '<div class="empty">(本步无内容)</div>';
  $('content').innerHTML = h;
  const f = s.frame;
  if (f){ $('frame').style.display=''; $('frame').src = f; }
  else $('frame').style.display='none';
}
function go(d){ i = Math.min(Math.max(i+d,0), STEPS.length-1); show(); }
function jumpTo(v){ const j = STEPS.findIndex(s=>s.no==v); if(j>=0){i=j;show();} }
function zoom(){
  const img = $('frame');
  if (!img.src || img.style.display==='none') return;
  $('ovimg').src = img.src;  // 同一张 JPEG 原尺寸(1280x800)展示
  $('overlay').style.display = 'flex';
}
document.addEventListener('keydown', e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.key==='ArrowLeft')go(-1);
  if(e.key==='ArrowRight')go(1);
});
// 每步代表画面由 build_replay.py 的 assign_frames 在生成时预计算
// (本步思考所见页面 = 上一个动作的稳定帧;截图动作用契约 PNG),
// 前端直接使用 s.frame,不再自行顺延。
// 智能返回:从列表页来的回退(保留筛选状态),直接打开的跳 /review
function goBack(){
  if (document.referrer && document.referrer.indexOf('/review') >= 0
      && history.length > 1) history.back();
  else location.href = '/review';
}
// 双击本地打开(file://)时没有服务端,隐藏返回链接
if (location.protocol === 'file:') document.getElementById('backlink').style.display = 'none';
renderList(); show();
</script></body></html>"""


def build(task_dir):
    task_dir = Path(task_dir)
    task_id = task_dir.name
    steps = assign_frames(build_steps(task_dir))
    html = (HTML_TEMPLATE
            .replace("__TITLE__", task_id)
            .replace("__STEPS__", json.dumps(steps, ensure_ascii=False)))
    out = task_dir / "replay.html"
    out.write_text(html, encoding="utf-8")
    return out, len(steps)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    if (root / "wire.jsonl").exists():
        out, n = build(root)
        print(f"生成 {out}: {n} 步")
    else:
        for task_dir in sorted(root.iterdir()):
            if (task_dir.is_dir() and task_dir.name.startswith("task_")
                    and (task_dir / "alignment.json").exists()
                    and (task_dir / "frames.json").exists()):
                try:
                    out, n = build(task_dir)
                    print(f"✅ {task_dir.name}: {n} 步 → {out.name}")
                except Exception as e:
                    print(f"❌ {task_dir.name}: {e}")

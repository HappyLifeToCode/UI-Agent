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


def step_frame(step):
    """本步代表画面:取最后一个有帧的工具调用"""
    for c in reversed(step["calls"]):
        if c["frame"]:
            return c["frame"]
    return None


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>__TITLE__ 轨迹回放</title>
<style>
:root{--border:#e0e0e0;--bg:#f7f7f8;--accent:#2563eb}
*{box-sizing:border-box}
body{font-family:system-ui,"Segoe UI",sans-serif;margin:0;height:100vh;display:flex;background:var(--bg)}
#steps{width:230px;background:#fff;border-right:1px solid var(--border);overflow-y:auto;padding:8px}
#steps .item{padding:6px 8px;border-radius:6px;cursor:pointer;font-size:13px;margin-bottom:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#steps .item:hover{background:#eef2ff}
#steps .item.active{background:var(--accent);color:#fff}
#center{flex:0.9;overflow-y:auto;padding:20px 24px;min-width:380px}
#right{flex:1.4;display:flex;align-items:flex-start;justify-content:center;padding:20px;overflow-y:auto}
#right img{max-width:100%;box-shadow:0 2px 14px rgba(0,0,0,.22);border-radius:4px;background:#fff;cursor:zoom-in}
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;align-items:flex-start;
  justify-content:center;overflow:auto;z-index:20;cursor:zoom-out}
#overlay img{max-width:96%;margin:20px auto;display:block;background:#fff}
.nav{display:flex;gap:8px;align-items:center;margin-bottom:14px;position:sticky;top:0;
  background:var(--bg);padding:6px 0;z-index:5}
button{padding:6px 14px;cursor:pointer;border:1px solid var(--border);border-radius:6px;background:#fff}
button:hover{background:#eef2ff}
input{width:70px;padding:6px;border:1px solid var(--border);border-radius:6px}
.block{background:#fff;border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:12px}
.block h3{margin:0 0 8px;font-size:13px;color:#666;letter-spacing:.5px}
.reasoning{color:#7c5a00;background:#fffbea;border-left:3px solid #f0b429}
.text-block{border-left:3px solid var(--accent)}
pre{white-space:pre-wrap;word-break:break-all;font-size:13px;margin:0;font-family:inherit}
.args{background:#f0f4ff;border-radius:6px;padding:8px;font-family:Consolas,monospace;font-size:12px}
.toolname{font-family:Consolas,monospace;color:var(--accent);font-weight:600}
.result{color:#444;font-size:12.5px}
.empty{color:#999;font-size:13px}
</style></head><body>
<div id="steps"></div>
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
<div id="overlay" onclick="this.style.display='none'"><img id="ovimg"></div>
<script>
const STEPS = __STEPS__;
let i = 0;
const $ = id => document.getElementById(id);
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function renderList(){
  $('steps').innerHTML = STEPS.map((s,j)=>{
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
// 每步代表画面预计算:取本步最后一个有帧的调用;整步无帧(非浏览器步)沿用前步
let lastFrame = null;
for (const s of STEPS){
  s.frame = null;
  for (let k = s.calls.length-1; k>=0; k--) if (s.calls[k].frame){ s.frame = s.calls[k].frame; break; }
  if (s.frame) lastFrame = s.frame; else s.frame = lastFrame;
}
renderList(); show();
</script></body></html>"""


def build(task_dir):
    task_dir = Path(task_dir)
    task_id = task_dir.name
    steps = build_steps(task_dir)
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人物检索 Web 演示后端：前端搜索 -> 后台 Agent 现场采集 -> 展示结果。

启动（prim 环境，含 fastapi + uvicorn）：
    /d/Anaconda/envs/prim/python.exe -m uvicorn server.app:app --port 8000

设计要点：
- 复用 scripts/run_tasks.py 的 run_one_task（同契约产物：result.json +
  screenshots/ + trace.zip，mapping.jsonl 同样追加记录）；
- 单 worker 线程串行执行 + acquire_lock，与批处理执行器互斥；
- task_id 用 web_NNNN 前缀（与批处理 task_XXXX 区分，qa 质检不扫描）；
- 缓存优先：data/ 下已有该人物的成功结果（status=success）直接返回。
"""
import json
import queue
import re
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import run_tasks  # noqa: E402  （复用执行器：run_one_task / acquire_lock / release_lock）

DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Scholar Agent Demo")

# ---- 任务队列与状态 --------------------------------------------------------
_job_queue: "queue.Queue[dict]" = queue.Queue()
_jobs: "dict[str, dict]" = {}          # task_id -> {state, detail, task}
_jobs_lock = threading.Lock()
_counter = 0
_counter_lock = threading.Lock()


def _next_task_id() -> str:
    """生成 web_NNNN 形式的 task_id，避开已存在的目录。"""
    global _counter
    with _counter_lock:
        while True:
            _counter += 1
            tid = f"web_{_counter:04d}"
            if not (DATA_DIR / tid).exists() and tid not in _jobs:
                return tid


def _find_cache(person_name: str):
    """在 data/ 下找该人物已有的成功结果，命中返回 task_id，否则 None。"""
    needle = person_name.strip().lower()
    if not needle or not DATA_DIR.exists():
        return None
    for rj in sorted(DATA_DIR.glob("*/result.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("status") != "success":
            continue
        name = str(data.get("person_name", "")).lower()
        if needle in name or name in needle:
            return rj.parent.name
    return None


class SearchRequest(BaseModel):
    person_name: str
    affiliation_hint: str = ""


@app.post("/api/search")
def search(req: SearchRequest):
    name = req.person_name.strip()
    if not name:
        raise HTTPException(400, "person_name 不能为空")

    cached = _find_cache(name)
    if cached:
        return {"task_id": cached, "cached": True}

    task = {
        "task_id": _next_task_id(),
        "person_name": name,
        "affiliation_hint": req.affiliation_hint.strip(),
    }
    with _jobs_lock:
        _jobs[task["task_id"]] = {"state": "queued", "detail": "排队中", "task": task}
    _job_queue.put(task)
    return {"task_id": task["task_id"], "cached": False}


@app.get("/api/status/{task_id}")
def status(task_id: str):
    with _jobs_lock:
        job = _jobs.get(task_id)
    if job:
        return {"task_id": task_id, "state": job["state"], "detail": job["detail"]}
    # 缓存命中的任务不在 _jobs 里，直接看磁盘
    if (DATA_DIR / task_id / "result.json").exists():
        return {"task_id": task_id, "state": "done", "detail": "已有结果"}
    raise HTTPException(404, "未知任务")


@app.get("/api/result/{task_id}")
def result(task_id: str):
    rj = DATA_DIR / task_id / "result.json"
    if not rj.exists():
        raise HTTPException(404, "结果不存在")
    return json.loads(rj.read_text(encoding="utf-8"))


_SHOT_RE = re.compile(r"^[A-Za-z0-9_]+_(profile|paper_\d{2})\.png$")


@app.get("/shots/{task_id}/{filename}")
def shots(task_id: str, filename: str):
    if not _SHOT_RE.match(filename):
        raise HTTPException(400, "非法文件名")
    p = DATA_DIR / task_id / "screenshots" / filename
    if not p.exists():
        raise HTTPException(404, "截图不存在")
    return FileResponse(str(p))


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---- 后台 worker -----------------------------------------------------------

def _set_job(task_id: str, state: str, detail: str):
    with _jobs_lock:
        if task_id in _jobs:
            _jobs[task_id]["state"] = state
            _jobs[task_id]["detail"] = detail


def _log_tail(task_id: str) -> str:
    """取执行日志最后一段非空文本作为进度摘要。"""
    log = LOG_DIR / f"{task_id}.log"
    if not log.exists():
        return ""
    try:
        lines = [l.strip(" •\r\n") for l in log.read_text(
            encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return ""
    lines = [l for l in lines if l]
    return lines[-1][:120] if lines else ""


def _progress_reporter(task_id: str, stop: threading.Event):
    """任务运行期间定期把日志末尾刷进状态。"""
    while not stop.wait(3):
        tail = _log_tail(task_id)
        _set_job(task_id, "running",
                 f"采集中（完整链路约 20-40 分钟）{('｜' + tail) if tail else ''}")


def _worker():
    while True:
        task = _job_queue.get()
        tid = task["task_id"]
        if not run_tasks.acquire_lock():
            _set_job(tid, "failed", "检测到有批处理执行器在运行，请稍后再试")
            continue
        stop = threading.Event()
        reporter = threading.Thread(target=_progress_reporter, args=(tid, stop), daemon=True)
        try:
            _set_job(tid, "running", "采集中（完整链路约 20-40 分钟）")
            reporter.start()
            record = run_tasks.run_one_task(task)
            if record.get("status") == "success":
                _set_job(tid, "done", "完成")
            else:
                reason = record.get("failure_reason") or record.get("status") or "未知原因"
                _set_job(tid, "failed", f"任务未成功：{reason}")
        except Exception as e:  # worker 不能死
            _set_job(tid, "failed", f"执行器异常：{e}")
        finally:
            stop.set()
            reporter.join(timeout=5)
            run_tasks.release_lock()
            _job_queue.task_done()


threading.Thread(target=_worker, daemon=True).start()

print(f"[scholar-web] 模板={run_tasks.TEMPLATE_PATH.name} 模型标签={run_tasks.MODEL}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)

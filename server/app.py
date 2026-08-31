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
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
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
    """在 data/ 下找该人物已有的结果，命中返回 task_id，否则 None。

    优先返回 status=success 的完整结果；没有 success 时退而返回
    partial 的（前端只展示已核查篇目），都没有才算未采集。
    """
    needle = person_name.strip().lower()
    if not needle or not DATA_DIR.exists():
        return None
    partial_hit = None
    for rj in sorted(DATA_DIR.glob("*/result.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        status = data.get("status")
        if status not in ("success", "partial"):
            continue
        name = str(data.get("person_name", "")).lower()
        if needle in name or name in needle:
            if status == "success":
                return rj.parent.name
            if partial_hit is None:
                partial_hit = rj.parent.name
    return partial_hit


class SearchRequest(BaseModel):
    person_name: str
    affiliation_hint: str = ""
    year_from: int = 0               # 论文年份:只统计该年份(0 = 默认近五年)
    # 可选的模型配置：非空时通过 KIMI_MODEL_* 环境变量临时指定模型
    # （kimi-code 官方通道，不改 config.toml；api_key 只存在于进程内存，
    #  不落盘、不写进 task.json / mapping.jsonl）
    model_name: str = ""
    api_key: str = ""
    provider_type: str = ""        # kimi / anthropic / openai
    base_url: str = ""
    max_context_size: int = 0


@app.post("/api/search")
def search(req: SearchRequest):
    name = req.person_name.strip()
    if not name:
        raise HTTPException(400, "person_name 不能为空")

    model_cfg = None
    if req.model_name.strip():
        if not req.api_key.strip():
            raise HTTPException(400, "指定了模型就必须提供 api_key")
        model_cfg = {
            "model_name": req.model_name.strip(),
            "api_key": req.api_key.strip(),
            "provider_type": req.provider_type.strip(),
            "base_url": req.base_url.strip(),
            "max_context_size": req.max_context_size,
        }

    cached = _find_cache(name)
    if cached:
        return {"task_id": cached, "cached": True}

    task = {
        "task_id": _next_task_id(),
        "person_name": name,
        "affiliation_hint": req.affiliation_hint.strip(),
    }
    if req.year_from:
        if not (1990 <= req.year_from <= 2030):
            raise HTTPException(400, "year_from 年份不合法")
        task["year_exact"] = req.year_from
    with _jobs_lock:
        _jobs[task["task_id"]] = {
            "state": "queued", "detail": "排队中", "task": task,
            "model_cfg": model_cfg,
        }
    _job_queue.put(task)
    return {"task_id": task["task_id"], "cached": False}


class CnkiRequest(BaseModel):
    """CNKI 关键词采集(药企横向链路,与 GS 学者链路独立)。"""
    keyword: str
    limit: int = 20
    # 模型配置同 /api/search(KIMI_MODEL_* 通道)
    model_name: str = ""
    api_key: str = ""
    provider_type: str = ""
    base_url: str = ""
    max_context_size: int = 0


def _cnki_tid(keyword: str) -> str:
    """清洗关键词并生成任务 id(与 run_cnki_tasks.task_id_of 同规则,
    但额外把空白/特殊字符替换掉,防止路径问题)。"""
    kw = re.sub(r"[^\w一-鿿-]+", "_", keyword.strip())
    return f"cnki_{kw}"


@app.post("/api/cnki_search")
def cnki_search(req: CnkiRequest):
    kw = req.keyword.strip()
    if not kw:
        raise HTTPException(400, "keyword 不能为空")
    limit = req.limit or 20
    if not (1 <= limit <= 100):
        raise HTTPException(400, "limit 需在 1~100 之间")

    import run_cnki_tasks
    tid = _cnki_tid(kw)
    task_dir = DATA_DIR / tid
    if (task_dir / "papers.json").exists() and \
            run_cnki_tasks.fragments_collected(task_dir) >= limit:
        return {"task_id": tid, "cached": True}

    model_cfg = None
    if req.model_name.strip():
        if not req.api_key.strip():
            raise HTTPException(400, "指定了模型就必须提供 api_key")
        model_cfg = {
            "model_name": req.model_name.strip(),
            "api_key": req.api_key.strip(),
            "provider_type": req.provider_type.strip(),
            "base_url": req.base_url.strip(),
            "max_context_size": req.max_context_size,
        }

    task = {"keyword": kw, "limit": limit, "task_id": tid}
    with _jobs_lock:
        _jobs[tid] = {"state": "queued", "detail": "排队中",
                      "task": task, "model_cfg": model_cfg, "kind": "cnki"}
    _job_queue.put(task)
    return {"task_id": tid, "cached": False}


@app.get("/api/cnki_result/{keyword}")
def cnki_result(keyword: str):
    tid = _cnki_tid(keyword)
    pj = DATA_DIR / tid / "papers.json"
    if not pj.exists():
        raise HTTPException(404, "结果不存在（任务可能未完成）")
    return json.loads(pj.read_text(encoding="utf-8"))


@app.get("/api/cnki_word/{keyword}")
def cnki_word(keyword: str):
    """生成并下载 CNKI 汇总 Word（总表内部链接 + 摘要全文附录）。"""
    import export_cnki_word
    tid = _cnki_tid(keyword)
    task_dir = DATA_DIR / tid
    if not (task_dir / "papers.json").exists():
        raise HTTPException(404, "缺少 papers.json")
    try:
        out = export_cnki_word.generate_report(task_dir)
    except Exception as e:
        raise HTTPException(500, f"生成 Word 失败: {e}")
    if not out or not Path(out).exists():
        raise HTTPException(500, "Word 生成失败")
    return FileResponse(
        str(out), filename=Path(out).name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/api/status/{task_id}")
def status(task_id: str):
    with _jobs_lock:
        job = _jobs.get(task_id)
    if job:
        return {"task_id": task_id, "state": job["state"], "detail": job["detail"]}
    # 缓存命中的任务不在 _jobs 里，直接看磁盘
    if task_id.startswith("cnki_"):
        if (DATA_DIR / task_id / "papers.json").exists():
            return {"task_id": task_id, "state": "done", "detail": "已有结果"}
    elif (DATA_DIR / task_id / "result.json").exists():
        return {"task_id": task_id, "state": "done", "detail": "已有结果"}
    raise HTTPException(404, "未知任务")


@app.get("/api/result/{task_id}")
def result(task_id: str):
    # L3 查询时懒恢复：result.json 缺失或比最新 fragment 旧时先重建再返回
    if run_tasks.result_needs_rebuild(DATA_DIR / task_id):
        run_tasks.merge_result(DATA_DIR / task_id)
    rj = DATA_DIR / task_id / "result.json"
    if not rj.exists():
        raise HTTPException(404, "结果不存在")
    return json.loads(rj.read_text(encoding="utf-8"))


_SHOT_RE = re.compile(r"^[A-Za-z0-9_]+_(profile|paper_\d{2,3})\.png$")


@app.get("/shots/{task_id}/{filename}")
def shots(task_id: str, filename: str):
    if not _SHOT_RE.match(filename):
        raise HTTPException(400, "非法文件名")
    p = DATA_DIR / task_id / "screenshots" / filename
    if not p.exists():
        raise HTTPException(404, "截图不存在")
    return FileResponse(str(p))


# 根页面:默认 GS 学者检索;--cnki 启动时换成 CNKI 专用页(独立端口独立 UI)
PAGE = "index.html"


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / PAGE))


# ---- 可视化审查系统(第二阶段;数据口径见 docs/ui_data_interface.md) ----------

# 静态托管 data/:回放页 replay.html、frames/、screenshots/ 直接可访问
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")


def _build_replay_chain(task_id: str):
    """跑回放三件套:alignment.json -> frames.json -> replay.html。

    直接 import scripts/ 下的三个构建脚本复用其 build/extract 函数
    (scripts 目录已在 sys.path)。trace.zip 大时 extract 需数十秒到几分钟。
    """
    import build_alignment
    import extract_trace_frames
    import build_replay as br

    task_dir = DATA_DIR / task_id
    if not (task_dir / "wire.jsonl").exists():
        raise HTTPException(404, "缺少 wire.jsonl，无法生成回放")
    a = build_alignment.build(task_dir)
    (task_dir / "alignment.json").write_text(
        json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")
    if not (task_dir / "trace.zip").exists():
        raise HTTPException(400, "缺少 trace.zip，无法提取画面帧")
    f = extract_trace_frames.extract(task_dir)
    _, n = br.build(task_dir)
    return {"actions": a["action_count"], "matched": f["matched_count"],
            "steps": n, "replay": f"/data/{task_id}/replay.html"}


@app.post("/api/replay/{task_id}")
def build_replay_api(task_id: str):
    """按 task_id 生成回放三件套(幂等,重复调用覆盖重建)。"""
    if not (DATA_DIR / task_id).is_dir():
        raise HTTPException(404, "任务不存在")
    try:
        return {"ok": True, **_build_replay_chain(task_id)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"生成回放失败: {e}")


@app.get("/api/export/{task_id}")
def export_task(task_id: str, include_trace: int = 0):
    """打包下载整个任务目录(zip)。默认不含 trace.zip(可达数百 MB),
    include_trace=1 时包含。"""
    import tempfile
    import zipfile
    from starlette.background import BackgroundTask

    if not re.fullmatch(r"[A-Za-z0-9_]+", task_id):
        raise HTTPException(400, "非法 task_id")
    task_dir = DATA_DIR / task_id
    if not task_dir.is_dir():
        raise HTTPException(404, "任务不存在")

    tmp = tempfile.NamedTemporaryFile(prefix=f"{task_id}_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(task_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(task_dir)
            if rel.parts[0] == "__pycache__":
                continue
            if not include_trace and p.name == "trace.zip":
                continue
            zf.write(p, f"{task_id}/{rel}")
    return FileResponse(tmp.name, filename=f"{task_id}.zip",
                        media_type="application/zip",
                        background=BackgroundTask(os.remove, tmp.name))


@app.get("/api/export_word/{task_id}")
def export_word_api(task_id: str):
    """生成并下载该任务的 Word 报告(复用 scripts/export_word.py 的 generate_report)。"""
    if not re.fullmatch(r"[A-Za-z0-9_]+", task_id):
        raise HTTPException(400, "非法 task_id")
    task_dir = DATA_DIR / task_id
    if not (task_dir / "result.json").exists():
        raise HTTPException(404, "缺少 result.json，无法生成报告")
    try:
        import export_word
        out = export_word.generate_report(task_dir)
    except Exception as e:
        raise HTTPException(500, f"生成 Word 报告失败: {e}")
    if not out or not Path(out).exists():
        raise HTTPException(500, "报告生成失败")
    return FileResponse(
        str(out), filename=Path(out).name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def _latest_records():
    """mapping.jsonl 按 task_id 取 start_time 最新一行(任务书口径)"""
    mapping = DATA_DIR / "mapping.jsonl"
    latest = {}
    if mapping.exists():
        for line in mapping.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = row.get("task_id")
            if not tid:
                continue
            if tid not in latest or (row.get("start_time") or "") > \
                    (latest[tid].get("start_time") or ""):
                latest[tid] = row
    return latest


@app.get("/api/review/tasks")
def review_tasks():
    """任务列表页数据:状态/框架/模型/耗时/步数/质检状态(仅 task_*)"""
    latest = _latest_records()
    tasks = []
    for d in sorted(DATA_DIR.iterdir()):
        if not (d.is_dir() and d.name.startswith("task_")):
            continue
        # L3 查询时懒恢复：中断残留的任务先重建 result.json 再汇总
        if run_tasks.result_needs_rebuild(d):
            run_tasks.merge_result(d)
        rec = latest.get(d.name, {})
        steps = None
        aj = d / "alignment.json"
        if aj.exists():
            try:
                steps = json.loads(aj.read_text(encoding="utf-8"))["action_count"]
            except (json.JSONDecodeError, KeyError):
                pass
        rj = d / "review.json"
        review = None
        if rj.exists():
            try:
                review = json.loads(rj.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        review_status = (review or {}).get("review_status", "pending")
        # 任务书口径:状态一列三态 成功/失败/待质检
        # 执行失败或审查剔除→失败;执行成功+审查合格→成功;其余(未审查/返工)→待质检
        if rec.get("status") != "success" or review_status == "rejected":
            display_status = "failed"
        elif review_status == "approved":
            display_status = "success"
        else:
            display_status = "pending_review"
        tasks.append({
            "task_id": d.name,
            "person_name": rec.get("person_name"),
            "status": rec.get("status"),
            "display_status": display_status,
            "framework": rec.get("framework"),
            "model": rec.get("model"),
            "duration_seconds": rec.get("duration_seconds"),
            "failure_reason": rec.get("failure_reason"),
            "steps": steps,
            "has_replay": (d / "replay.html").exists(),
            "review_status": review_status,
            "reviewed_at": (review or {}).get("reviewed_at"),
        })
    return {"tasks": tasks}


class ReviewRequest(BaseModel):
    task_id: str
    review_status: str          # approved / rejected / needs_rerun
    notes: str = ""
    issues: list = []
    reviewer: str = ""


@app.post("/api/review")
def submit_review(req: ReviewRequest):
    """写入审查结论 data/<task_id>/review.json(schema 见 docs/review_schema.md)"""
    if req.review_status not in ("approved", "rejected", "needs_rerun"):
        raise HTTPException(400, "review_status 非法")
    task_dir = DATA_DIR / req.task_id
    if not (task_dir.is_dir() and req.task_id.startswith("task_")):
        raise HTTPException(404, "任务不存在")
    if req.review_status in ("rejected", "needs_rerun") and not req.issues \
            and not req.notes:
        raise HTTPException(400, "剔除/返工必须填写问题或备注")
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    payload = {
        "task_id": req.task_id,
        "review_status": req.review_status,
        "reviewer": req.reviewer,
        "reviewed_at": now,
        "issues": req.issues,
        "notes": req.notes,
    }
    (task_dir / "review.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "reviewed_at": now}


@app.get("/review")
def review_page():
    return FileResponse(str(STATIC_DIR / "review.html"))


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


_MODEL_ENV_KEYS = ("KIMI_MODEL_NAME", "KIMI_MODEL_API_KEY", "KIMI_MODEL_PROVIDER_TYPE",
                   "KIMI_MODEL_BASE_URL", "KIMI_MODEL_MAX_CONTEXT_SIZE")


def _apply_model_env(cfg: dict):
    """把 KIMI_MODEL_* 写入进程环境（子进程 kimi CLI 继承），返回旧值供恢复。

    worker 单线程串行执行，env 的修改-恢复不会交错。api_key 只进内存与环境变量，
    不落盘。
    """
    saved = {k: os.environ.get(k) for k in _MODEL_ENV_KEYS}
    os.environ["KIMI_MODEL_NAME"] = cfg["model_name"]
    os.environ["KIMI_MODEL_API_KEY"] = cfg["api_key"]
    if cfg.get("provider_type"):
        os.environ["KIMI_MODEL_PROVIDER_TYPE"] = cfg["provider_type"]
    if cfg.get("base_url"):
        os.environ["KIMI_MODEL_BASE_URL"] = cfg["base_url"]
    if cfg.get("max_context_size"):
        os.environ["KIMI_MODEL_MAX_CONTEXT_SIZE"] = str(cfg["max_context_size"])
    return saved


def _restore_model_env(saved: dict):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _cnki_reporter(tid: str, task_dir: Path, stop: threading.Event):
    """CNKI 任务进度：日志摘要 + 已沉淀 fragment 数(逐篇即写,数字实时增长)。"""
    import run_cnki_tasks
    while not stop.wait(3):
        tail = _log_tail(tid)
        n = run_cnki_tasks.fragments_collected(task_dir)
        _set_job(tid, "running",
                 f"采集中（已沉淀 {n} 篇）{('｜' + tail) if tail else ''}")


def _worker():
    while True:
        task = _job_queue.get()
        tid = task["task_id"]
        if not run_tasks.acquire_lock():
            _set_job(tid, "failed", "检测到有批处理执行器在运行，请稍后再试")
            continue
        with _jobs_lock:
            job = _jobs.get(tid, {})
        model_cfg = job.get("model_cfg")
        is_cnki = job.get("kind") == "cnki" or task.get("keyword")
        saved_env = None
        saved_model = run_tasks.MODEL
        stop = threading.Event()
        reporter_args = (tid, stop) if not is_cnki else (tid, DATA_DIR / tid, stop)
        reporter_target = _progress_reporter if not is_cnki else _cnki_reporter
        reporter = threading.Thread(target=reporter_target, args=reporter_args, daemon=True)
        try:
            if model_cfg:
                saved_env = _apply_model_env(model_cfg)
                run_tasks.MODEL = model_cfg["model_name"]  # mapping/_run 的模型标签
            if is_cnki:
                import run_cnki_tasks
                _set_job(tid, "running", "采集中")
                reporter.start()
                ok = run_cnki_tasks.run_one(task, timeout=run_cnki_tasks.DEFAULT_TIMEOUT,
                                            delay=False)
                _set_job(tid, "done" if ok else "failed",
                         "完成" if ok else "采集未完全成功（可重试补齐缺失篇目）")
            else:
                _set_job(tid, "running", "采集中（完整链路约 20-40 分钟）")
                reporter.start()
                record = run_tasks.run_one_task(task)
                if record.get("status") == "success":
                    try:
                        info = _build_replay_chain(tid)
                        _set_job(tid, "done", f"完成，回放已生成（{info['steps']} 步）")
                    except Exception as e:  # 回放生成失败不影响采集结果本身
                        _set_job(tid, "done", f"完成（回放生成失败：{e}，可在结果页手动点「生成回放」）")
                else:
                    reason = record.get("failure_reason") or record.get("status") or "未知原因"
                    _set_job(tid, "failed", f"任务未成功：{reason}")
        except Exception as e:  # worker 不能死
            _set_job(tid, "failed", f"执行器异常：{e}")
        finally:
            stop.set()
            reporter.join(timeout=5)
            if saved_env is not None:
                _restore_model_env(saved_env)
            run_tasks.MODEL = saved_model
            run_tasks.release_lock()
            _job_queue.task_done()


# L2 启动兜底：服务启动时自动重建中断/崩溃残留的 result.json
run_tasks.recover_interrupted()

threading.Thread(target=_worker, daemon=True).start()

print(f"[scholar-web] 模板={run_tasks.TEMPLATE_PATH.name} 模型标签={run_tasks.MODEL}")

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Scholar Web 服务")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--cnki", action="store_true",
                        help="CNKI 专用模式:根页面 served 为 cnki.html(建议配独立端口,如 --port 8002)")
    args = parser.parse_args()
    if args.cnki:
        PAGE = "cnki.html"  # 模块级变量,直接赋值即可
        print("[scholar-web] CNKI 模式:根页面 = cnki.html")
    uvicorn.run(app, host="127.0.0.1", port=args.port)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CNKI 关键词文献采集执行器：任务队列 → kimi -p 采集会话 → 合并 papers.json。

与 GS 学者采集链路（run_tasks.py）完全独立：单独的任务文件
（tasks_cnki.jsonl）、模板（task_prompt_cnki.md）、数据目录
（data/cnki_<关键词>/）、台账（data/mapping_cnki.jsonl），互不读写对方文件。

管线（对照 GS 三段式的简化版，采集与核查合一）：
    采集  Agent 单会话：首页搜索 → 逐篇详情页（点"更多"拿摘要全文）
           → 每篇即写 data/cnki_<kw>/papers/paper_NN.json fragment
    合并  执行器纯 Python：meta.json + papers/*.json → papers.json
           （缺 fragment 记 note，状态 partial；崩溃后 --merge-only 重建）

用法：
    python scripts/run_cnki_tasks.py                          # 跑 tasks_cnki.jsonl 全部
    python scripts/run_cnki_tasks.py --task 芍药甘草汤 --limit 20
    python scripts/run_cnki_tasks.py --start-from 补中益气汤   # 从某关键词开始（含）
    python scripts/run_cnki_tasks.py --merge-only 芍药甘草汤   # 只合并已有 fragment
    python scripts/run_cnki_tasks.py --timeout 3000           # 单任务超时秒数（默认 2400）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "scripts" / "task_prompt_cnki.md"
TASKS_PATH = PROJECT_ROOT / "tasks_cnki.jsonl"
MAPPING_PATH = PROJECT_ROOT / "data" / "mapping_cnki.jsonl"
LOCK_PATH = PROJECT_ROOT / "data" / ".cnki-runner.lock"
LOG_DIR = PROJECT_ROOT / "logs"

DEFAULT_LIMIT = 20
DEFAULT_TIMEOUT = 2400  # 40 分钟（20 篇详情页 + 首页搜索，实测约 15~25 分钟）
FRAMEWORK = "kimi-code"
MODEL = os.environ.get("AGENT_MODEL", "kimi-for-coding/k3")


def _detect_path(*candidates, which=None):
    """依次探测路径候选；都失败时回退 PATH 查找（which 为可执行名）。"""
    for cand in candidates:
        if cand and Path(cand).exists():
            return str(cand)
    return shutil.which(which) if which else None


KIMI_BIN = _detect_path(
    os.environ.get("KIMI_BIN"),
    "D:/KimiCode/bin/kimi.exe",
    Path.home() / ".kimi-code" / "bin" / "kimi.exe",
    which="kimi",
)
SESSIONS_ROOT = Path(_detect_path(
    "D:/KimiCode/sessions",
    Path.home() / ".kimi-code" / "sessions",
) or Path.home() / ".kimi-code" / "sessions")


def task_id_of(keyword: str) -> str:
    return f"cnki_{keyword.strip()}"


def task_dir_of(keyword: str) -> Path:
    return PROJECT_ROOT / "data" / task_id_of(keyword)


def load_tasks(path: Path):
    """读任务队列 jsonl：每行 {"keyword": "...", "limit": N}（limit 可省）。"""
    tasks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def render_prompt(task: dict) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{TASK_ID}}": task_id_of(task["keyword"]),
        "{{KEYWORD}}": task["keyword"],
        "{{LIMIT}}": str(task.get("limit") or DEFAULT_LIMIT),
    }
    for k, v in replacements.items():
        template = template.replace(k, v)
    return template


def run_kimi_session(prompt: str, log_path: Path, timeout: int, label: str):
    """起一次 kimi -p 会话，输出捕获到 log_path。返回 (returncode, start, end)。"""
    start = datetime.now(timezone.utc)
    returncode = 0
    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            result = subprocess.run(
                [KIMI_BIN, "-p", prompt],
                cwd=str(PROJECT_ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        print(f"[ERROR] {label} 超时（{timeout // 60} 分钟）")
        returncode = -1
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {label} 执行失败: {e}")
        returncode = -2
    return returncode, start, datetime.now(timezone.utc)


def find_session_dir(after_ts: float):
    """best-effort：返回会话开始后最新出现的 session 目录名（写入台账用）。"""
    try:
        cands = [d for d in SESSIONS_ROOT.iterdir() if d.is_dir()
                 and d.stat().st_mtime > after_ts]
        if cands:
            return max(cands, key=lambda d: d.stat().st_mtime).name
    except Exception:  # noqa: BLE001
        pass
    return None


def fragments_collected(task_dir: Path) -> int:
    papers = task_dir / "papers"
    return len(list(papers.glob("paper_*.json"))) if papers.exists() else 0


def merge_cnki(keyword: str, expected_limit: int):
    """meta.json + papers/*.json → papers.json。返回 (status, collected, missing)。"""
    task_dir = task_dir_of(keyword)
    meta = {}
    meta_path = task_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("keyword", keyword)
    meta.setdefault("source", "中国知网（CNKI）")
    meta.setdefault("sort", "默认相关度排序")

    papers_dir = task_dir / "papers"
    frags = sorted(papers_dir.glob("paper_*.json")) if papers_dir.exists() else []
    papers = [json.loads(f.read_text(encoding="utf-8")) for f in frags]
    papers.sort(key=lambda p: p.get("rank") or 0)
    collected = len(papers)
    missing = max(expected_limit - collected, 0)

    meta["collected"] = f"前 {collected} 篇" + (f"（目标 {expected_limit} 篇）" if missing else "")
    meta["papers"] = papers
    (task_dir / "papers.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "success" if not missing else "partial"
    return status, collected, missing


def append_mapping(record: dict):
    with open(MAPPING_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_one(task: dict, timeout: int, delay: bool):
    keyword = task["keyword"]
    limit = int(task.get("limit") or DEFAULT_LIMIT)
    tid = task_id_of(keyword)
    task_dir = task_dir_of(keyword)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "papers").mkdir(exist_ok=True)

    already = fragments_collected(task_dir)
    if already >= limit and (task_dir / "papers.json").exists():
        print(f"[跳过] {tid}：已有 {already} 篇 fragment 和 papers.json（--merge-only 可重建）")
        return True

    print(f"\n{'=' * 60}\n[{tid}] {keyword} @ \n{'=' * 60}")
    if already:
        print(f"[续跑] 检测到 {already} 篇已有 fragment，会话可从中断处继续（缺失由 Agent 补齐）")
    if delay:
        time.sleep(5)

    prompt = render_prompt(task)
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{tid}.log"
    rc, start, end = run_kimi_session(prompt, log_path, timeout, tid)

    status, collected, missing = merge_cnki(keyword, limit)
    session_id = find_session_dir(start.timestamp())
    append_mapping({
        "task_id": tid, "keyword": keyword, "limit": limit,
        "framework": FRAMEWORK, "model": MODEL, "session_id": session_id,
        "start_time": start.isoformat(), "end_time": end.isoformat(),
        "duration_seconds": round((end - start).total_seconds(), 2),
        "returncode": rc, "status": status,
        "collected": collected, "missing": missing,
    })
    print(f"[合并] {tid}: {status}（采集 {collected} 篇，缺 {missing} 篇）→ {task_dir / 'papers.json'}")
    return rc == 0 and status == "success"


def main():
    parser = argparse.ArgumentParser(description="CNKI 关键词文献采集执行器（与 GS 链路独立）")
    parser.add_argument("--tasks", default=str(TASKS_PATH), help="任务队列 jsonl（默认 tasks_cnki.jsonl）")
    parser.add_argument("--task", action="append", help="直接指定关键词（可多次），跳过任务文件")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="每关键词采集篇数（默认 20）")
    parser.add_argument("--start-from", help="从该关键词开始（含）跑任务文件里的任务")
    parser.add_argument("--merge-only", metavar="KEYWORD", help="不跑会话，只把该关键词的 fragment 合并成 papers.json")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单任务超时秒数（默认 2400）")
    parser.add_argument("--no-delay", action="store_true", help="任务间不睡眠")
    args = parser.parse_args()

    if args.merge_only:
        status, collected, missing = merge_cnki(args.merge_only, DEFAULT_LIMIT)
        print(f"[合并] {task_id_of(args.merge_only)}: {status}，采集 {collected} 篇，缺 {missing} 篇")
        sys.exit(0 if collected else 1)

    tasks = [{"keyword": k, "limit": args.limit} for k in args.task or []]
    if not tasks:
        tasks = load_tasks(Path(args.tasks))
        if args.start_from:
            idx = next((i for i, t in enumerate(tasks)
                        if t["keyword"] == args.start_from), None)
            if idx is None:
                parser.error(f"--start-from 的关键词 {args.start_from} 不在任务文件里")
            tasks = tasks[idx:]

    if not tasks:
        parser.error("没有任务：用 --task 指定关键词或在任务文件里添加")

    if LOCK_PATH.exists():
        print(f"[ERROR] 锁文件存在：{LOCK_PATH}（另一个采集进程在跑？确认后删除再跑）")
        sys.exit(1)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")

    ok, fail = 0, 0
    try:
        for task in tasks:
            try:
                if run_one(task, args.timeout, not args.no_delay):
                    ok += 1
                else:
                    fail += 1
            except Exception as e:  # noqa: BLE001
                print(f"[ERROR] {task.get('keyword')} 任务异常: {e}")
                fail += 1
    finally:
        LOCK_PATH.unlink(missing_ok=True)
    print(f"\n完成：成功 {ok} 个，失败/部分 {fail} 个")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

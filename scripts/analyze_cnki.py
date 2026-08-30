#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CNKI 文献整理总结：data/cnki_<关键词>/papers.json -> <关键词>_summary.md

与采集解耦（沿用 analyze_research.py 的原则）：读取采集沉淀的 papers.json
（题录 + 完整摘要），构建上下文后经一次 kimi -p 会话产出独立 Markdown 总结：
    - 总体概览（主题方向归纳表）
    - 分方向要点
    - 横向总结（研究热点/方法学特征/证据强度提示）
    - 文献清单表
用法：
    python scripts/analyze_cnki.py --task 四君子汤            # 单个关键词
    python scripts/analyze_cnki.py --task A --task B          # 多个
    python scripts/analyze_cnki.py --all                      # data/ 下全部 cnki_*
    python scripts/analyze_cnki.py --task 四君子汤 --dry-run  # 只构建上下文不调模型
    python scripts/analyze_cnki.py --task 四君子汤 --timeout 1800
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"

# Windows CreateProcess 命令行上限约 32767 字符（prompt 作为 kimi -p 参数传递），留余量
PROMPT_MAX_CHARS = 30000


def _find_kimi_bin() -> str | None:
    """定位 kimi CLI：环境变量 > 本机约定路径 > ~/.kimi-code > PATH。"""
    env = os.environ.get("KIMI_BIN")
    if env and Path(env).exists():
        return env
    for cand in (Path("D:/KimiCode/bin/kimi.exe"),
                 Path.home() / ".kimi-code" / "bin" / "kimi.exe"):
        if cand.exists():
            return str(cand)
    return shutil.which("kimi")


def _build_context(data: dict, abstract_cap: int | None = None) -> str:
    """把 papers.json 整理成喂给模型的 Markdown 上下文。

    abstract_cap：单篇摘要字符上限（超出截断并标注），用于 prompt 超 Windows
    命令行上限时整体压缩 —— 始终保留全部标题，只缩摘要。
    """
    lines = [
        f"## 检索信息",
        f"- 关键词：{data.get('keyword') or '-'}",
        f"- 来源：{data.get('source') or '中国知网（CNKI）'}（{data.get('sort') or '默认相关度排序'}）",
        f"- 命中 {data.get('total_results') or '-'} 条，采集 {len(data.get('papers') or [])} 篇",
        "",
        f"## 文献清单（按相关度排序，含完整摘要）",
    ]
    for p in data.get("papers") or []:
        lines.append(
            f"\n**#{p.get('rank')} {p.get('title') or '(无标题)'}**"
            f"（{p.get('authors') or '-'}；{p.get('source') or '-'}；{p.get('date') or '-'}）")
        kw = "；".join(p.get("keywords") or [])
        if kw:
            lines.append(f"关键词：{kw}")
        abstract = (p.get("abstract") or "").strip()
        if abstract_cap and len(abstract) > abstract_cap:
            abstract = abstract[:abstract_cap] + "……[摘要截断]"
        lines.append(abstract if abstract else "（无摘要）")
    return "\n".join(lines)


ANALYZE_INSTRUCTIONS = """\
# 你的任务

你是中医药文献整理专家。基于上面给出的知网文献清单（题录 + 完整摘要），
对该关键词下的文献做整理与总结，输出中文 Markdown。

## 输出结构（严格遵守，只输出 Markdown 正文，不要用代码块包裹，不要有寒暄）

## 一、总体概览
用一张 Markdown 表格归纳文献的主题方向分类（方向 | 篇目编号 | 代表文献）。

## 二、分方向要点
按方向逐节展开（`### 方向名`），每节概述该方向的研究内容、代表文献的结论。

## 三、横向总结
- 研究热点与应用场景
- 方法学特征（研究类型、样本量、常见联合干预/技术范式）
- 值得注意的发现（如剂量/配伍/剂型方面的具体结论）
- 证据强度提示（样本量、单中心、发表偏倚等局限）

## 四、文献清单
一张 Markdown 表格：序号 | 标题 | 作者 | 来源 | 时间。

## 注意事项
- 所有论断必须能在上面的清单中找到依据，不要虚构文献内容；
- 篇目编号用清单中的 #N 引用，方便回溯；
- 标注"（无摘要）"的条目仅依据题录推断，结论相应保守。"""


def analyze_keyword(keyword: str, kimi_bin: str, timeout: int, dry_run: bool) -> Path | None:
    """对单个关键词做整理总结，产出 <关键词>_summary.md；返回路径或 None。"""
    task_dir = DATA_DIR / f"cnki_{keyword}"
    pj = task_dir / "papers.json"
    if not pj.exists():
        print(f"[跳过] cnki_{keyword}：没有 papers.json")
        return None
    data = json.loads(pj.read_text(encoding="utf-8"))

    def _make_prompt(ctx: str) -> str:
        return (f"请对以下知网文献做整理与总结，输出中文 Markdown。\n\n"
                f"{ctx}\n\n{ANALYZE_INSTRUCTIONS}")

    context = _build_context(data)
    prompt = _make_prompt(context)
    if len(prompt) > PROMPT_MAX_CHARS:
        # prompt 超 Windows 命令行上限：保留全部标题，按比例压缩每篇摘要
        papers = data.get("papers") or []
        overhead = len(prompt) - sum(len((p.get("abstract") or "").strip())
                                     for p in papers)
        n_abs = sum(1 for p in papers if (p.get("abstract") or "").strip())
        # 截断时每篇会追加 "……[摘要截断]" 后缀，预算里预先扣除
        cap = max(300, (PROMPT_MAX_CHARS - overhead - 200 - 10 * n_abs) // max(n_abs, 1))
        context = _build_context(data, abstract_cap=cap)
        prompt = _make_prompt(context)
        print(f"[警告] cnki_{keyword}：prompt 超长，摘要已压缩至每篇 ≤{cap} 字符"
              f"（总长 {len(prompt)}）")

    if dry_run:
        out = task_dir / f"{keyword}_summary_context.md"
        out.write_text(context, encoding="utf-8")
        print(f"[DRY-RUN] cnki_{keyword}：上下文已写入 {out.name}（{len(context)} 字符）")
        return out

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"analyze_cnki_{keyword}.log"
    print(f"[总结] cnki_{keyword}：调用 kimi（超时 {timeout // 60} 分钟）...")
    try:
        proc = subprocess.run(
            [kimi_bin, "-p", prompt],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",  # kimi 输出 UTF-8，Windows 默认 GBK 会解码崩
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"[ERROR] cnki_{keyword} 总结超时（{timeout // 60} 分钟）")
        return None
    log_path.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr,
                        encoding="utf-8")
    body = proc.stdout.strip()
    if proc.returncode != 0 or not body:
        print(f"[ERROR] cnki_{keyword} 总结失败（returncode={proc.returncode}），"
              f"详见 {log_path}")
        return None

    # 规整模型输出：去掉模型爱加的 1~3 空格行首缩进（避免被渲染成代码块）、
    # 标题行前的多余项目符号；模型若自己又写了一行一级标题（脚本头部已带标题）则丢弃
    lines = [re.sub(r"^\s{1,3}(?=\S)", "", ln) for ln in body.splitlines()]
    lines = [re.sub(r"^[•·\-]\s+(?=#)", "", ln) for ln in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    body = "\n".join(lines).strip()

    n_papers = len(data.get("papers") or [])
    collected = data.get("collected") or f"{n_papers} 篇"
    header = (f"# “{data.get('keyword') or keyword}”知网文献整理与总结\n\n"
              f"- 数据来源：{data.get('source') or '中国知网（CNKI）'}"
              f"（{data.get('sort') or '默认相关度排序'}）\n"
              f"- 命中 {data.get('total_results') or '-'} 条，采集 {collected}\n"
              f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}\n\n---\n\n")
    out = task_dir / f"{keyword}_summary.md"
    out.write_text(header + body + "\n", encoding="utf-8")
    print(f"[OK] cnki_{keyword}：{out}")
    return out


def main():
    parser = argparse.ArgumentParser(description="CNKI 文献整理总结（产出独立 Markdown）")
    parser.add_argument("--task", action="append", help="关键词（可多次指定）")
    parser.add_argument("--all", action="store_true", help="data/ 下所有有 papers.json 的 cnki_* 任务")
    parser.add_argument("--dry-run", action="store_true",
                        help="只构建上下文写入 *_summary_context.md，不调用模型")
    parser.add_argument("--timeout", type=int, default=1200, help="单任务超时秒数（默认 1200）")
    args = parser.parse_args()

    if not args.task and not args.all:
        parser.error("请指定 --task <关键词> 或 --all")

    kimi_bin = None if args.dry_run else _find_kimi_bin()
    if not args.dry_run and not kimi_bin:
        print("[ERROR] 找不到 kimi CLI（可设环境变量 KIMI_BIN 指定路径）")
        sys.exit(1)

    keywords = list(args.task or [])
    if args.all:
        keywords += sorted(p.parent.name.removeprefix("cnki_")
                           for p in DATA_DIR.glob("cnki_*/papers.json"))

    ok, skip = 0, 0
    for kw in dict.fromkeys(keywords):  # 去重保序
        out = analyze_keyword(kw, kimi_bin or "", args.timeout, args.dry_run)
        if out:
            ok += 1
        else:
            skip += 1
    print(f"完成：成功 {ok} 个，跳过/失败 {skip} 个")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""近五年论文研究方向离线分析：result.json -> <task_id>_summary.md

与采集/核查完全解耦（师兄的建议：先沉淀原料、后独立分析）：
读取 data/<task_id>/result.json 里沉淀的论文标题/年份/期刊/摘要
（abstract 自 Phase 2 核查起沉淀进 fragment 并透传到 result.json），
按年份分组构建上下文，通过一次 kimi -p 会话产出独立 Markdown 分析：
    - 近五年研究脉络总述
    - 各年份小节：研究方向、研究思路、代表性工作
    - 研究趋势小结（演变 / 转向）

无摘要的旧数据（模板加 abstract 字段之前采集的任务）自动降级为
仅基于标题/期刊的分析，并在输出中注明。

用法：
    python scripts/analyze_research.py --task task_0009            # 单个任务
    python scripts/analyze_research.py --task task_0001 --task task_0003
    python scripts/analyze_research.py --all                       # data/ 下全部
    python scripts/analyze_research.py --task task_0009 --dry-run  # 只构建上下文，不调模型
    python scripts/analyze_research.py --task task_0009 --timeout 1800
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"


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


def _build_context(result: dict, abstract_cap: int | None = None) -> str:
    """把 result.json 按年份分组整理成喂给模型的 Markdown 上下文。

    abstract_cap：单篇摘要字符上限（超出截断并标注），用于 prompt 超 Windows
    命令行上限时整体压缩 —— 始终保留全部标题，只缩摘要。
    """
    by_year = defaultdict(list)
    for p in result.get("recent_papers") or []:
        by_year[str(p.get("year") or "未知年份")].append(p)

    lines = [
        f"## 学者信息",
        f"- 姓名：{result.get('person_name') or '-'}",
        f"- 单位：{result.get('affiliation') or '-'}",
        f"- 研究兴趣（GS 主页自报）：{'、'.join(result.get('interests') or []) or '-'}",
        f"- 总被引 {result.get('total_citations') or '-'}，"
        f"h-index {result.get('h_index') or '-'}，"
        f"i10-index {result.get('i10_index') or '-'}",
        "",
        f"## 近五年论文清单（按年份分组，年内按 GS 被引降序）",
    ]
    for year in sorted(by_year, reverse=True):
        lines.append(f"\n### {year} 年（{len(by_year[year])} 篇）")
        for p in by_year[year]:
            status = "已核查" if p.get("match_status") == "matched" else "未找到外部记录"
            lines.append(
                f"\n**{p.get('title') or '(无标题)'}**"
                f"（GS 被引 {p.get('gs_citations') if p.get('gs_citations') is not None else '-'}，"
                f"{p.get('journal') or '来源未知'}，{status}）")
            abstract = (p.get("abstract") or "").strip()
            if abstract_cap and len(abstract) > abstract_cap:
                abstract = abstract[:abstract_cap] + "……[摘要截断]"
            lines.append(abstract if abstract else "（无摘要）")
    return "\n".join(lines)


# Windows CreateProcess 命令行上限约 32767 字符（prompt 作为 kimi -p 参数传递），留余量
PROMPT_MAX_CHARS = 30000


ANALYZE_INSTRUCTIONS = """\
# 你的任务

你是学术研究方向分析专家。基于上面给出的学者信息与近五年论文清单
（含标题、期刊、被引、核查状态，多数论文附完整摘要），分析该学者近五年的
研究方向与研究思路，输出中文 Markdown。

## 输出结构（严格遵守，只输出 Markdown 正文，不要用代码块包裹，不要有寒暄）

## 一、近五年研究脉络总述
（3-6 句话概括整体研究方向与主线）

## 二、各年度研究方向分析
按年份降序每年一个小节：`### YYYY 年`，每节包含：
- **研究方向**：该年主要研究主题/领域
- **研究思路**：方法论层面的特点（如高通量计算、机器学习辅助、实验/理论结合等）
- **代表性工作**：1-3 篇关键论文及其贡献（引用标题，可缩写）

## 三、研究趋势小结
（研究方向随时间的演变、转向、持续性，以及近一两年的新动向）

## 注意事项
- 标注"（无摘要）"的论文只能依据标题与期刊推断，结论相应保守；
- 论文清单按 Google Scholar 近五年收录，未必穷尽，避免绝对化表述；
- 不要虚构论文内容，所有论断必须能在上面的清单中找到依据。"""


def analyze_task(task_dir: Path, kimi_bin: str, timeout: int, dry_run: bool) -> Path | None:
    """对单个任务做离线分析，产出 <task_id>_summary.md；返回路径或 None。"""
    rj = task_dir / "result.json"
    if not rj.exists():
        print(f"[跳过] {task_dir.name}：没有 result.json")
        return None
    result = json.loads(rj.read_text(encoding="utf-8"))

    def _make_prompt(ctx: str) -> str:
        return (f"请分析以下学者的近五年论文，输出研究方向分析（Markdown）。\n\n"
                f"{ctx}\n\n{ANALYZE_INSTRUCTIONS}")

    context = _build_context(result)
    prompt = _make_prompt(context)
    if len(prompt) > PROMPT_MAX_CHARS:
        # prompt 超 Windows 命令行上限：保留全部标题，按比例压缩每篇摘要
        papers = result.get("recent_papers") or []
        overhead = len(prompt) - sum(len((p.get("abstract") or "").strip())
                                     for p in papers)
        n_abs = sum(1 for p in papers if (p.get("abstract") or "").strip())
        # 截断时每篇会追加 "……[摘要截断]" 后缀，预算里预先扣除
        cap = max(300, (PROMPT_MAX_CHARS - overhead - 200 - 10 * n_abs) // max(n_abs, 1))
        context = _build_context(result, abstract_cap=cap)
        prompt = _make_prompt(context)
        print(f"[警告] {task_dir.name}：prompt 超长，摘要已压缩至每篇 ≤{cap} 字符"
              f"（总长 {len(prompt)}）")
    if dry_run:
        out = task_dir / f"{task_dir.name}_summary_context.md"
        out.write_text(context, encoding="utf-8")
        print(f"[DRY-RUN] {task_dir.name}：上下文已写入 {out.name}（{len(context)} 字符）")
        return out

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"analyze_{task_dir.name}.log"
    print(f"[分析] {task_dir.name}：调用 kimi（超时 {timeout // 60} 分钟）...")
    try:
        proc = subprocess.run(
            [kimi_bin, "-p", prompt],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",  # kimi 输出 UTF-8，Windows 默认 GBK 会解码崩
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"[ERROR] {task_dir.name} 分析超时（{timeout // 60} 分钟）")
        return None
    log_path.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr,
                        encoding="utf-8")
    body = proc.stdout.strip()
    if proc.returncode != 0 or not body:
        print(f"[ERROR] {task_dir.name} 分析失败（returncode={proc.returncode}），"
              f"详见 {log_path}")
        return None

    # 规整模型输出：模型常给每行加 1~3 空格公共缩进（混合无缩进的标题行时
    # dedent 失效，且带缩进的段落会被 Markdown 渲染成代码块），逐行剥掉；
    # 标题行前的多余项目符号一并去掉
    body = "\n".join(re.sub(r"^\s{1,3}(?=\S)", "", ln)
                     for ln in proc.stdout.strip().splitlines())
    body = "\n".join(re.sub(r"^[•·\-]\s+(?=#)", "", ln)
                     for ln in body.splitlines()).strip()

    header = (f"# {result.get('person_name') or task_dir.name} 近五年论文研究方向分析\n\n"
              f"- 任务编号：{task_dir.name}\n"
              f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}\n"
              f"- 数据基础：Google Scholar 近五年论文清单 + Semantic Scholar/OpenAlex 核查数据"
              f"（含摘要原文）\n\n---\n\n")
    out = task_dir / f"{task_dir.name}_summary.md"
    out.write_text(header + body + "\n", encoding="utf-8")
    print(f"[OK] {task_dir.name}：{out}")
    return out


def main():
    parser = argparse.ArgumentParser(description="近五年论文研究方向离线分析（产出独立 Markdown）")
    parser.add_argument("--task", action="append", help="task_id（可多次指定）")
    parser.add_argument("--all", action="store_true", help="data/ 下所有有 result.json 的任务")
    parser.add_argument("--dry-run", action="store_true",
                        help="只构建上下文写入 *_summary_context.md，不调用模型")
    parser.add_argument("--timeout", type=int, default=1200, help="单个任务分析超时秒数（默认 1200）")
    args = parser.parse_args()

    if not args.task and not args.all:
        parser.error("请指定 --task <task_id> 或 --all")

    kimi_bin = None if args.dry_run else _find_kimi_bin()
    if not args.dry_run and not kimi_bin:
        print("[ERROR] 找不到 kimi CLI（可设环境变量 KIMI_BIN 指定路径）")
        sys.exit(1)

    task_ids = list(args.task or [])
    if args.all:
        task_ids += sorted(p.parent.name for p in DATA_DIR.glob("*/result.json"))

    ok, skip = 0, 0
    for tid in dict.fromkeys(task_ids):  # 去重保序
        out = analyze_task(DATA_DIR / tid, kimi_bin or "", args.timeout, args.dry_run)
        if out:
            ok += 1
        else:
            skip += 1
    print(f"完成：成功 {ok} 个，跳过/失败 {skip} 个")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

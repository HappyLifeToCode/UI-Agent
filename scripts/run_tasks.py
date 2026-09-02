#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量执行器：读 tasks.jsonl，逐条拉起 Kimi Code 会话执行谷歌学术人物检索任务。

依赖：
    - Kimi Code CLI 已安装（通常在 ~/.kimi-code/bin/kimi.exe）
    - Playwright MCP 已配置（~/.kimi-code/mcp.json，需固定 @playwright/mcp@0.0.64
      并带 --headless --save-trace，0.0.65 起官方移除了 --save-trace）
    - Python 3.8+

用法：
    python scripts/run_tasks.py                          # 跑全部任务（三段式管线）
    python scripts/run_tasks.py --limit 3                # 只跑前 3 条
    python scripts/run_tasks.py --start-from task_0003   # 从某条开始（含）
    python scripts/run_tasks.py --dry-run                # 只打印将执行的命令，不真跑
    python scripts/run_tasks.py --model kimi-for-coding/k1  # 指定模型
    python scripts/run_tasks.py --merge-only task_0001     # 不跑任务，用磁盘上已有的
                                                         # phase1.json + checks/ 重建
                                                         # result.json（批内被硬杀后恢复）
    python scripts/run_tasks.py --template task_prompt_template_s2.md
                                                         # 旧单会话模式（top-10 回退路径）

三段式管线（默认，2026-08 起）：
    Phase 1  主会话：谷歌学术部分（检索→主页→profile 截图→近五年【全部】论文
             清单），Agent 写 data/<task_id>/phase1.json。
    Phase 2  核查会话：执行器把清单按 GS 被引全局降序编 rank 1..N，每
             BATCH_SIZE 篇切成一批，每批起一个全新 Kimi 会话做 Semantic
             Scholar 逐篇核查（上下文与论文总数 N 无关），Agent 每篇即写
             data/<task_id>/checks/paper_NN.json fragment。
    Phase 3  合并：执行器纯 Python 把 phase1.json + checks/*.json 合并成
             契约 result.json（缺 fragment 的记 not_found + note），随后
             调 export_word 生成 <task_id>_report.docx。
    旧的单会话 top-10 链路仍可用 --template 指定旧模板回退。

产出（每条任务，data/<task_id>/ 标准目录）：
    task.json                     任务定义副本（执行器写入）
    phase1.json                   阶段一产物：作者信息 + 近五年全部论文清单（Agent 写入）
    ranked_papers.json            执行器排序编号后的论文清单（rank 1..N，批次依据）
    checks/paper_NN.json          阶段二逐篇核查 fragment（Agent 每篇即写）
    result.json                   合并后的契约结果，含 recent_papers 近五年全部
                                  论文及 S2 核查数据（执行器合并写入；
                                  跑完后补写 _run 执行元信息，见 [M5]）
    <task_id>_report.docx         Word 报告（export_word.py 生成，含逐篇详情+截图证据）
    wire.jsonl                    会话完整轨迹（各阶段 wire 按序拼接）
    trace.zip                     Phase 1 主会话的 Playwright 浏览器侧轨迹
    trace_batchNN.zip             各核查批会话的 trace（附加产物，不进契约）
    screenshots/<task_id>_profile.png  作者主页整页截图（执行器归档）
    screenshots/<task_id>_paper_NN.png 每篇论文一张 S2 详情页截图
                                  （not_found 篇目为搜索结果页留证；执行器从
                                  MCP 输出目录归档）
    data/mapping.jsonl            task_id <-> session_id <-> 框架 映射表（追加写）

    执行日志写在 logs/<task_id>.log（phase1 与所有核查批追加写入同一文件，
    批与批之间有执行器写的分隔头；项目根 logs/ 目录，不属于交付目录），
    供调试与解析 session_id 用（解析取日志中最后一个 session id，即最新会话）。

注意：
    - 默认全新重跑：执行前清理任务目录里的旧产出（result.json / phase1.json /
      checks/ / wire.jsonl / trace*.zip / screenshots/ / *_report.docx 等），
      避免失败任务"继承"上一次成功运行的旧结果。
    - 断点续跑：加 --resume 则反过来保留进度 —— phase1.json 已成功则跳过
      Phase 1，Phase 2 逐批只补缺失的 fragment（中途 Ctrl+C / 断网后重跑
      同一任务即可从断点继续）；--status <task_id> 可先查看完成状态。
      缺篇时 checks/ 不再自动清理，保留待续跑（全部就位才清）。
    - 同一时刻只允许一个执行器实例（data/.runner.lock），防止并发跑批把
      同一 IP 打到谷歌学术限流、以及 session 归属错乱。

模块导航（按功能分区，改哪块直接跳哪块）：
    [M1] 配置区 ................ 路径常量、框架/模型、模板与批次参数、CLI 路径检测
    [M2] 任务加载与 Prompt 渲染 . load_tasks / render_prompt（支持 extra 占位符）
    [M3] Session 定位 .......... snapshot_sessions / detect_new_session /
                                 parse_session_id_from_log / find_session_dir /
                                 locate_session
    [M4] 产物收集 .............. collect_wire_fragment / snapshot_mcp_output /
                                 _pack_trace_zip / collect_browser_artifacts
    [M5] 清理与状态判定 ........ clean_task_outputs / read_status /
                                 annotate_run_info（跑完补写 result.json 的 _run）
    [M6] 映射表记录 ............ build_record / append_mapping（mapping.jsonl schema）
    [M7] 并发锁 ................ acquire_lock / release_lock
    [M8] 三段式管线 ............ run_kimi_session / rank_papers / fragments_missing /
                                 merge_result / run_one_task（串联 M2-M7）
    [M9] 批量主循环与 CLI ...... main（参数解析、CAPTCHA 重试、反爬延迟）
"""
import argparse
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# 设置 UTF-8 输出（Windows 控制台编码问题）
if platform.system() == "Windows":
    sys.stdout.reconfigure(encoding='utf-8')

# =============================================================================
# [M1] 配置区：路径常量、框架/模型标识、模板与批次参数、CLI 路径检测
#   - 改框架或模型：改 FRAMEWORK / MODEL（写入 mapping 的标识字段）
#   - 改 MCP 输出目录：必须与 ~/.kimi-code/mcp.json 的 --output-dir 保持一致
#   - 三段式模板：PHASE1_TEMPLATE_PATH（谷歌学术+清单）/
#     PHASE2_TEMPLATE_PATH（S2 核查批）；--template 指定的旧模板进 TEMPLATE_PATH
#     走单会话 legacy 模式
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PHASE1_TEMPLATE_PATH = PROJECT_ROOT / "scripts" / "task_prompt_phase1_s2.md"
PHASE2_TEMPLATE_PATH = PROJECT_ROOT / "scripts" / "task_prompt_phase2_s2.md"
TEMPLATE_PATH = PROJECT_ROOT / "scripts" / "task_prompt_template_s2.md"  # legacy 单会话模式用
LEGACY_MODE = False  # --template 指定后置 True，run_one_task 走旧单会话链路
MAPPING_PATH = PROJECT_ROOT / "data" / "mapping.jsonl"
LOCK_PATH = PROJECT_ROOT / "data" / ".runner.lock"
LOG_DIR = PROJECT_ROOT / "logs"  # 执行日志（非交付物，仅调试/解析 session_id 用）
SESSIONS_ROOT = Path.home() / ".kimi-code" / "sessions"
# playwright-mcp 的输出目录（与 ~/.kimi-code/mcp.json 的 --output-dir 一致）
MCP_OUTPUT_DIR = PROJECT_ROOT / ".playwright-mcp"

# 三段式参数：核查批大小（每个子会话负责的论文数）与各阶段超时
BATCH_SIZE = 10
PHASE1_TIMEOUT = 2700   # 45 分钟（谷歌学术翻页采全量清单，高产学者页数多）
BATCH_TIMEOUT = 1800    # 30 分钟/批（10 篇 S2 核查，SPA 加载慢）

# 反检测配置：MCP 启动时应通过 --config / --init-script 加载这两个文件
# （见 docs/QA1.md「反检测配置」节；check_mcp_config 在跑批前自检是否就位）
MCP_CONFIG_PATH = Path.home() / ".kimi-code" / "mcp.json"
MCP_STEALTH_CONFIG = PROJECT_ROOT / "scripts" / "playwright_mcp_config.json"
STEALTH_INIT_SCRIPT = PROJECT_ROOT / "scripts" / "stealth_init.js"

# Kimi CLI 路径检测
KIMI_BIN = Path.home() / ".kimi-code" / "bin" / "kimi.exe"
if not KIMI_BIN.exists():
    # 尝试在 PATH 中查找
    KIMI_BIN = "kimi"

FRAMEWORK = "kimi-code"
MODEL = os.environ.get("AGENT_MODEL", "kimi-for-coding/k3")  # 可通过环境变量或 CLI 参数覆盖


# =============================================================================
# [M2] 任务加载与 Prompt 渲染
#   - 任务字段变更（如甲方清单加字段）：改 load_tasks / render_prompt
#   - 三段式模板占位符：{{TASK_ID}} {{PERSON_NAME}} {{AFFILIATION_HINT}}
#     {{PERSON_NAME_URLENCODED}}；核查批模板额外用 {{PAPERS_JSON}} {{BATCH_SIZE}}，
#     由 extra 参数注入
# =============================================================================

def load_tasks(path: Path):
    """读 tasks.jsonl，返回任务列表。"""
    tasks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def render_prompt(task: dict, template_path: Path = None, extra: dict = None) -> str:
    """把任务字段填进 prompt 模板的占位符；extra 注入批次等附加占位符。"""
    tpl = (template_path or PHASE1_TEMPLATE_PATH).read_text(encoding="utf-8")
    name = task["person_name"]
    out = (tpl
           .replace("{{TASK_ID}}", task["task_id"])
           .replace("{{PERSON_NAME}}", name)
           .replace("{{PERSON_NAME_URLENCODED}}", urllib.parse.quote_plus(name))
           .replace("{{AFFILIATION_HINT}}", task.get("affiliation_hint", "")))
    for k, v in (extra or {}).items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def year_rule(task: dict, top10: bool = False) -> str:
    """生成 {{YEAR_RULE}} 占位符文本：task 带 year_exact 时精确单年，否则默认 2021+。

    top10=True 用于 legacy 单会话模板（Top10 口径）；
    False 用于 phase1 模板（不设数量上限口径）。
    """
    if task.get("year_exact"):
        y = task["year_exact"]
        stop = (f"按年份降序翻页，当某一页最后一行的年份已小于 {y} 时即可停止"
                f"（该页中 {y} 年的条目仍要保留）。")
        scope = f"【只统计 {y} 年这一年发表的论文】，其他年份一律排除"
    else:
        stop = "某一页最后一行年份 < 2021 即可停止；否则 cstart 改为 100、200… 继续翻页。"
        scope = "【汇总所有 2021 年及以后的论文】"
    if top10:
        return f"{stop}{scope}，按被引数降序取前 10 篇（不足 10 篇有几篇取几篇）。"
    return f"{stop}{scope}，不设数量上限——高产学者可能有几十甚至上百篇，全部都要，一篇不能少。"


# =============================================================================
# [M3] Session 定位：确定"本次运行"对应哪个 Kimi 会话
#   - 主路径：parse_session_id_from_log（解析 CLI 自报的 session id，可靠）
#   - 兜底：  detect_new_session（sessions 目录差分，疑似并发时可能绑错）
#   - 换 Agent 框架（如 Claude Code）：本区整体重写
# =============================================================================

def snapshot_sessions() -> set:
    """返回当前所有 session 目录路径集合（跨所有 wd_* 工作目录分组）。"""
    found = set()
    if SESSIONS_ROOT.exists():
        for wd in SESSIONS_ROOT.iterdir():
            if wd.is_dir():
                for s in wd.iterdir():
                    if s.is_dir() and s.name.startswith("session_"):
                        found.add(str(s))
    return found


def detect_new_session(before: set):
    """通过会话目录差异定位本次运行新建的 session。返回完整 session 目录路径。"""
    after = snapshot_sessions()
    new = after - before
    if not new:
        return None
    # 取最新修改的，防止一次运行意外产生多个
    return max(new, key=lambda p: Path(p).stat().st_mtime)


def parse_session_id_from_log(log_path: Path):
    """从执行日志末尾的 'To resume this session: kimi -r session_xxx' 解析 session_id。

    这是比目录差分更可靠的绑定方式：CLI 自己报告本次会话 id。
    """
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(r"kimi -r (session_[0-9a-fA-F-]+)", text)
    return matches[-1] if matches else None


def find_session_dir(session_id: str):
    """按 session_id 在 sessions 根目录下定位完整目录路径。"""
    if not session_id or not SESSIONS_ROOT.exists():
        return None
    for wd in SESSIONS_ROOT.iterdir():
        if wd.is_dir():
            candidate = wd / session_id
            if candidate.is_dir():
                return str(candidate)
    return None


def locate_session(log_path: Path, sessions_before: set):
    """定位一次会话：优先解析日志自报 id，目录差分兜底。返回 (session_id, session_dir)。"""
    session_id = parse_session_id_from_log(log_path)
    session_dir = find_session_dir(session_id) if session_id else None
    if not session_dir:
        session_dir = detect_new_session(sessions_before)
        session_id = Path(session_dir).name if session_dir else None
    return session_id, session_dir


# =============================================================================
# [M4] 产物收集：wire.jsonl（Agent 轨迹）+ trace.zip / 截图（浏览器侧产物）
#   - trace 来源：MCP --save-trace 落盘裸文件，_pack_trace_zip 按 Playwright
#     标准布局打包成契约要求的 trace.zip
#   - 截图：Agent 截到 MCP 输出目录（或项目根），执行器归档进 screenshots/
#   - 三段式下每个阶段/批次各自收集：wire 片段最后按序拼接成 wire.jsonl，
#     trace 按阶段命名（trace.zip = phase1，trace_batchNN.zip = 各核查批）
# =============================================================================

def collect_wire_fragment(session_dir: str) -> str:
    """读一次会话的 wire.jsonl 内容（三段式下由调用方按序拼接落盘）。"""
    if not session_dir:
        return ""
    wire = Path(session_dir) / "agents" / "main" / "wire.jsonl"
    if not wire.exists():
        return ""
    text = wire.read_text(encoding="utf-8", errors="replace")
    return text if text.endswith("\n") or not text else text + "\n"


def snapshot_mcp_output() -> dict:
    """快照 MCP 输出目录（相对路径 -> mtime，递归），用于运行后差分定位新产物。"""
    snap = {}
    if MCP_OUTPUT_DIR.exists():
        for p in MCP_OUTPUT_DIR.rglob("*"):
            if p.is_file():
                snap[str(p.relative_to(MCP_OUTPUT_DIR))] = p.stat().st_mtime
    return snap


def _is_new(rel: str, mtime: float, before: dict) -> bool:
    """文件是否本次运行新产生/新修改。"""
    return rel not in before or mtime > before[rel]


def _pack_trace_zip(traces_dir: Path, before: dict, dest: Path) -> bool:
    """把本次运行新产生的 Playwright trace 原始文件打包成 trace.zip。

    @playwright/mcp@0.0.64 的 --save-trace 落盘的是裸文件
    （traces/trace-<时间戳>.trace / .network / .stacks + traces/resources/），
    不是 zip；契约要求 trace.zip，这里按 Playwright 标准布局打包，
    可用 `npx playwright show-trace trace.zip` 回放。
    """
    if not traces_dir.exists():
        return False
    groups = {}  # 时间戳 -> {后缀: 路径}
    for p in traces_dir.glob("trace-*.*"):
        rel = str(p.relative_to(MCP_OUTPUT_DIR))
        if not _is_new(rel, p.stat().st_mtime, before):
            continue
        ts = p.stem.split("-", 1)[-1]           # trace-1784643123324
        suffix = p.suffix.lstrip(".")           # trace / network / stacks
        groups.setdefault(ts, {})[suffix] = p
    if not groups:
        return False
    # 取最新的一组（时间戳为毫秒级数字）
    ts = max(groups, key=lambda k: int(k) if k.isdigit() else 0)
    group = groups[ts]
    if "trace" not in group:
        return False
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for suffix, arc in (("trace", "trace.trace"),
                            ("network", "trace.network"),
                            ("stacks", "trace.stacks")):
            if suffix in group:
                zf.write(group[suffix], arc)
        resources = traces_dir / "resources"
        if resources.exists():
            for r in resources.rglob("*"):
                if r.is_file():
                    rel = str(r.relative_to(MCP_OUTPUT_DIR))
                    if _is_new(rel, r.stat().st_mtime, before):
                        zf.write(r, str(Path("resources") / r.relative_to(resources)))
    return True


def collect_browser_artifacts(before: dict, task: dict, task_dir: Path,
                              trace_name: str = "trace.zip"):
    """从 MCP 输出目录收归浏览器侧产物：trace.zip 和论文详情页截图。

    截图契约：每篇 recent_papers 一张 <task_id>_paper_NN.png（NN = rank
    编号），matched 篇目为 S2 论文详情页，not_found 篇目为搜索结果页
    留证。返回 (has_trace, has_screenshot)，has_screenshot 为"至少有一张"。
    三段式下每个阶段各自调用一次，trace_name 区分阶段。
    """
    task_id = task["task_id"]

    # --- trace.zip：打包本次运行新产生的 trace 原始文件 ---
    has_trace = _pack_trace_zip(MCP_OUTPUT_DIR / "traces", before, task_dir / trace_name)

    # --- 截图：归档到 screenshots/ 子目录 ---
    screenshots_dir = task_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    # Agent 按要求命名 <task_id>_profile.png（作者主页整页）和
    # <task_id>_paper_NN.png（每篇论文一张），可能落在 MCP 输出目录或项目根目录
    for base in (MCP_OUTPUT_DIR, PROJECT_ROOT):
        if not base.exists():
            continue
        named = sorted(base.glob(f"{task_id}_profile.png")) + \
            sorted(base.glob(f"{task_id}_paper_*.png"))
        for p in named:
            shutil.move(str(p), str(screenshots_dir / p.name))
            moved += 1
    if moved == 0:
        # 兜底：本次运行新产生的任意 png，按产生时间顺序编号归档
        new_pngs = []
        for base in (MCP_OUTPUT_DIR, PROJECT_ROOT):
            if not base.exists():
                continue
            for p in base.glob("*.png"):
                mtime = p.stat().st_mtime
                key = str(p.relative_to(base))
                if key not in before or mtime > before.get(key, 0):
                    new_pngs.append((mtime, p))
        for i, (_, p) in enumerate(sorted(new_pngs), start=1):
            shutil.move(str(p), str(screenshots_dir / f"{task_id}_paper_{i:02d}.png"))
            moved += 1

    return has_trace, moved > 0


# =============================================================================
# [M5] 清理与状态判定
#   - clean_task_outputs：跑前清旧产出，杜绝失败任务"继承"上次成功结果
#   - read_status：以 result.json 的 status 字段为任务结果唯一判据
# =============================================================================

def clean_task_outputs(task: dict, task_dir: Path):
    """执行前清理旧产出，保证本次结果只反映本次运行。

    顺带清理历史遗留的 run.log（执行日志已改写到项目根 logs/ 目录，
    不属于 data/<task_id>/ 交付目录）。
    """
    task_id = task["task_id"]
    for name in ("result.json", "phase1.json", "ranked_papers.json", "wire.jsonl",
                 "trace.zip", "run.log", f"{task_id}_profile.png",
                 f"{task_id}_report.docx"):
        (task_dir / name).unlink(missing_ok=True)
    for p in task_dir.glob(f"{task_id}_paper_*.png"):
        p.unlink(missing_ok=True)
    for p in task_dir.glob("trace_batch*.zip"):
        p.unlink(missing_ok=True)
    shutil.rmtree(task_dir / "checks", ignore_errors=True)
    shutil.rmtree(task_dir / "screenshots", ignore_errors=True)


def _read_json(path: Path):
    """读 JSON 文件，不存在或损坏返回 None。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def read_status(task_dir: Path) -> str:
    """读 result.json 的 status 字段，判断任务结果。"""
    data = _read_json(task_dir / "result.json")
    if data is None:
        return "no_result" if not (task_dir / "result.json").exists() else "invalid_result"
    return data.get("status", "unknown")


def log_has_rate_limit(task_id: str) -> bool:
    """执行日志中是否出现 API 限流迹象（429 / RateLimit）。

    典型故障：小模型上下文打满触发 compaction，而 compaction 请求被
     provider 限流（429）时 Kimi Code 直接中止整个会话（returncode=1），
    日志末尾出现 'compaction.failed: APIProviderRateLimitError: 429'。
    """
    log_path = LOG_DIR / f"{task_id}.log"
    if not log_path.exists():
        return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "429" in text or "RateLimit" in text


def annotate_run_info(task_dir: Path, session_id, start_time, end_time,
                      batch_session_ids=None):
    """任务跑完后把执行元信息补写进 result.json 的 _run 字段。

    必须由执行器（而非 Agent）写入：Agent 不知道自己的 session_id，
    在 prompt 里要这个值只会得到编造值。steps 取 wire.jsonl 中
    step.begin 事件计数；result.json 缺失或损坏时静默跳过（状态由
    read_status 另行判定）。三段式下 session_id 记 phase1 主会话，
    各核查批的 session 记 batch_session_ids。
    """
    data = _read_json(task_dir / "result.json")
    if data is None:
        return
    steps = 0
    wire = task_dir / "wire.jsonl"
    if wire.exists():
        steps = sum(1 for line in wire.read_text(encoding="utf-8", errors="replace").splitlines()
                    if '"step.begin"' in line)
    data["_run"] = {
        "session_id": session_id,
        "framework": FRAMEWORK,
        "model": MODEL,
        "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ") if start_time else None,
        "end_time": end_time.strftime("%Y-%m-%dT%H:%M:%SZ") if end_time else None,
        "steps": steps,
    }
    if batch_session_ids is not None:
        data["_run"]["batch_session_ids"] = batch_session_ids
    (task_dir / "result.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def derive_failure_reason(task_dir: Path, status: str, returncode: int):
    """生成 mapping 用的单行失败原因；status 为 success 时返回 None。

    优先取 Agent 写在 result.json 里的 note（captcha/not_found 的场景说明），
    其次按 returncode / status 给出 runner 侧判断。
    """
    if status == "success":
        return None
    data = _read_json(task_dir / "result.json")
    if data and data.get("note"):
        return data["note"]
    if returncode == -1:
        return "任务超时"
    if returncode == -2:
        return "CLI 执行异常"
    if returncode != 0 and log_has_rate_limit(task_dir.name):
        return "API 限流（429）导致会话中止"
    if status == "no_result":
        return "Agent 未写入 result.json"
    if status == "invalid_result":
        return "result.json 不是合法 JSON"
    return status  # captcha / not_found / partial 等但 Agent 没写 note


# =============================================================================
# [M6] 映射表记录：mapping.jsonl 的统一 schema
#   - 下游（谭的转换管线、审查系统任务列表页）按此 schema 读取
#   - schema 有变动必须同步下游（24 小时规则），并更新 docs/FORMAT.md §5
# =============================================================================

def build_record(task: dict, session_id, start_time, end_time,
                 returncode, status, has_result, has_screenshot, has_trace,
                 trajectory_collected, failure_reason=None) -> dict:
    """构造统一 schema 的执行记录（写入 mapping.jsonl）。

    注意：不记录 session_path（含本机用户名等隐私路径），只留 session_id。
    failure_reason 仅 status 非 success 时有值，供下游单行读出失败原因。
    """
    return {
        "task_id": task["task_id"],
        "person_name": task.get("person_name"),
        "framework": FRAMEWORK,
        "model": MODEL,
        "session_id": session_id,
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "duration_seconds": round((end_time - start_time).total_seconds(), 2) if start_time and end_time else None,
        "returncode": returncode,
        "status": status,
        "failure_reason": failure_reason,
        "has_result": has_result,
        "has_screenshot": has_screenshot,
        "has_trace": has_trace,
        "trajectory_collected": trajectory_collected,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def append_mapping(record: dict):
    """追加写入 mapping.jsonl。"""
    MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MAPPING_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# =============================================================================
# [M7] 并发锁：同一时刻只允许一个执行器实例
#   - 防两个批次并发打谷歌学术（同 IP 限流）+ session 归属错乱
#   - 异常退出残留锁文件时，确认无在跑批次后手动删除 data/.runner.lock
# =============================================================================

def acquire_lock() -> bool:
    """创建执行器锁文件，防止并发跑批。成功返回 True。"""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}".encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock():
    LOCK_PATH.unlink(missing_ok=True)


# =============================================================================
# [M8] 三段式管线：run_kimi_session / rank_papers / fragments_missing /
#   merge_result / run_one_task
#   数据流向：任务定义 -> phase1 会话（清单落盘）-> 执行器排序编号 ->
#   核查批会话（fragment 落盘）-> 合并 result.json -> Word 报告 -> 写映射。
# =============================================================================

def run_kimi_session(prompt: str, log_path: Path, timeout: int, label: str = "",
                     append: bool = False):
    """起一次 kimi -p 会话，输出捕获到 log_path。返回 (returncode, start, end)。

    returncode 语义：0 正常；-1 超时；-2 执行异常；其余为 CLI 退出码。
    append=True 时追加写入（同一任务的多个会话共用一个日志），并先写一行
    分隔头标注是哪个阶段/批次，方便 tail -f 全程观察和事后定位。
    """
    cmd = [str(KIMI_BIN), "-p", prompt]
    start_time = datetime.now(timezone.utc)
    try:
        with open(log_path, "a" if append else "w", encoding="utf-8") as log_file:
            if append:
                log_file.write(f"\n{'='*60}\n[runner] {label} 开始 "
                               f"{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n{'='*60}\n")
                log_file.flush()
            result = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                shell=False
            )
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        print(f"[ERROR] {label}超时（{timeout // 60}分钟）")
        returncode = -1
    except Exception as e:
        print(f"[ERROR] {label}执行失败: {e}")
        returncode = -2
    return returncode, start_time, datetime.now(timezone.utc)


def rank_papers(papers: list) -> list:
    """执行器侧统一排序编号：GS 被引全局降序，rank 从 1 连续编号。

    不信任 Agent 排序；核查/截图/fragment 编号都以此为唯一依据。
    """
    def key(p):
        try:
            return -(int(p.get("gs_citations") or 0))
        except (TypeError, ValueError):
            return 0
    ranked = sorted(papers, key=key)
    for i, p in enumerate(ranked, start=1):
        p["rank"] = i
    return ranked


def fragments_missing(task_dir: Path, batch: list) -> list:
    """本批中还没有 fragment（checks/paper_NN.json）的论文。"""
    checks = task_dir / "checks"
    return [p for p in batch
            if not (checks / f"paper_{p['rank']:02d}.json").exists()]


def merge_result(task_dir: Path, cleanup: bool = False):
    """Phase 3 合并：phase1.json + checks/*.json -> 契约 result.json（纯 Python）。

    缺 fragment 的论文记 match_status=not_found + note 说明；全部到位
    status=success，有缺 status=partial。返回 (status, missing_count)；
    phase1.json 缺失/损坏返回 (None, -1)。
    cleanup=True 时合并成功后删除 checks/（fragment 内容已全部进入
    result.json，不重复保留）。只允许在"确认该任务无人在跑"的场景传 True
    （任务收尾、启动扫描、--merge-only）；运行中途的批末合并必须 False——
    后续若崩溃，崩溃恢复还靠这些 fragment。
    """
    phase1 = _read_json(task_dir / "phase1.json")
    if phase1 is None:
        return None, -1
    ranked = _read_json(task_dir / "ranked_papers.json")
    if ranked is None:
        ranked = rank_papers(phase1.get("recent_papers") or [])

    checks = task_dir / "checks"
    recent = []
    missing = 0
    for p in ranked:
        frag = _read_json(checks / f"paper_{p['rank']:02d}.json") or {}
        if not frag:
            missing += 1
        entry = {
            # 身份字段以执行器排序清单为准（fragment 可能写歪）
            "rank": p["rank"],
            "title": p["title"],
            "year": p.get("year"),
            "gs_citations": p.get("gs_citations"),
            # 核查结果以 fragment 为准（兼容 s2_* 和 openalex_* 两套字段）
            "match_status": frag.get("match_status", "not_found"),
            "s2_id": frag.get("s2_id"),
            "s2_url": frag.get("s2_url"),
            "s2_citations": frag.get("s2_citations"),
            "openalex_id": frag.get("openalex_id"),
            "openalex_url": frag.get("openalex_url"),
            "openalex_citations": frag.get("openalex_citations"),
            "doi": frag.get("doi"),
            "journal": frag.get("journal"),
            "abstract": frag.get("abstract"),
            "screenshot": frag.get("screenshot"),
            "note": frag.get("note"),
        }
        if not frag:
            entry["note"] = "核查会话未完成，按 not_found 记录"
        recent.append(entry)

    result = {k: phase1.get(k) for k in
              ("task_id", "person_name", "affiliation", "interests",
               "total_citations", "h_index", "i10_index", "top_papers", "profile_url")}
    result["recent_papers"] = recent
    if missing:
        result["status"] = "partial"
        result["note"] = f"{missing} 篇论文核查会话未完成，按 not_found 记录"
    else:
        result["status"] = "success"
        if not recent:
            result["note"] = "近五年（2021+）无论文"
    (task_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if cleanup:
        shutil.rmtree(task_dir / "checks", ignore_errors=True)
    return result["status"], missing


def result_needs_rebuild(task_dir: Path) -> bool:
    """统一判据（三层防线共用）：result.json 缺失或比最新 fragment 旧
    -> 需要用磁盘数据（phase1.json + checks/）重建。
    """
    task_dir = Path(task_dir)
    if not (task_dir / "phase1.json").exists():
        return False  # 非三段式任务（legacy/老数据），不掺和
    rj = task_dir / "result.json"
    checks = task_dir / "checks"
    frags = list(checks.glob("paper_*.json")) if checks.exists() else []
    if not frags:
        return False  # 无 fragment 可重建（含合并后已自动清理 checks/ 的任务），不掺和
    newest_frag = max(p.stat().st_mtime for p in frags)
    return (not rj.exists()) or rj.stat().st_mtime < newest_frag


def recover_interrupted():
    """L2 启动兜底扫描：对 data/ 下所有需要重建的任务自动重建 result.json
    （兜住进程被硬杀、连批末合并都没执行到的场景）。CLI 跑批启动时和
    Web 服务启动时各调用一次，无需人工介入。
    """
    data_dir = PROJECT_ROOT / "data"
    if not data_dir.exists():
        return
    for d in sorted(data_dir.iterdir()):
        if d.is_dir() and result_needs_rebuild(d):
            status, miss = merge_result(d)
            # 只有全部就位才清 checks/：缺篇时保留 fragment 供 --resume 续跑补齐
            if status and miss == 0:
                shutil.rmtree(d / "checks", ignore_errors=True)
            if status:
                print(f"[恢复] {d.name}: 检测到中断残留，已自动重建 result.json"
                      f"（status={status}，缺 {miss} 篇）")


def show_task_state(task_id: str):
    """打印任务在磁盘上的完成状态（--status 用）：phase1 / fragment 缺口 / result。"""
    task_dir = PROJECT_ROOT / "data" / task_id
    if not task_dir.is_dir():
        print(f"[状态] {task_id}：目录不存在（从未执行过）")
        return
    phase1 = _read_json(task_dir / "phase1.json")
    result = _read_json(task_dir / "result.json")

    print(f"[状态] {task_id}")
    if phase1 is None:
        print("  Phase 1 : 未完成（无 phase1.json 或损坏）—— 只能全新重跑")
    else:
        n_papers = len(phase1.get("recent_papers") or [])
        print(f"  Phase 1 : status={phase1.get('status')}（近五年论文 {n_papers} 篇）")

    ranked = _read_json(task_dir / "ranked_papers.json")
    if ranked is None and phase1 and phase1.get("status") == "success":
        ranked = rank_papers(phase1.get("recent_papers") or [])
    if ranked:
        missing = fragments_missing(task_dir, ranked)
        done_complete = result and result.get("status") == "success" and missing
        if done_complete:
            # 完整任务收尾后 checks/ 已自动清理，fragment 内容已全部进入 result.json
            print(f"  Phase 2 : 已全部合并清理（fragment 0/{len(ranked)} 属正常收尾状态）")
        else:
            print(f"  Phase 2 : fragment {len(ranked) - len(missing)}/{len(ranked)}"
                  + (f"，缺 rank {[p['rank'] for p in missing]}" if missing else "（已齐）"))

    if result:
        print(f"  合并结果: status={result.get('status')}"
              + (f"（{result['note']}）" if result.get("note") else ""))
    else:
        print("  合并结果: 无 result.json")

    if phase1 and phase1.get("status") == "success" and ranked:
        missing = fragments_missing(task_dir, ranked)
        if missing and (not result or result.get("status") != "success"):
            print(f"  建议    : python scripts/run_tasks.py --resume --start-from {task_id} --limit 1 --no-delay")
        elif not missing or (result and result.get("status") == "success"):
            print("  建议    : 已完成，无需续跑（要更新数据请直接全新重跑）")


def run_one_task(task: dict, dry_run: bool = False, resume: bool = False) -> dict:
    """执行单个任务（三段式管线；--template 指定旧模板时走 legacy 单会话），返回执行记录。

    resume=True（--resume）断点续跑：phase1.json 已成功则跳过 Phase 1、
    不清理旧产出，Phase 2 只补缺失的 fragment；phase1 不可用则自动
    退化为全新任务（先清理再跑）。
    """
    task_id = task["task_id"]
    task_dir = PROJECT_ROOT / "data" / task_id

    print(f"\n{'='*60}")
    print(f"[{task_id}] {task['person_name']} @ {task.get('affiliation_hint', 'N/A')}"
          + ("（续跑模式）" if resume else ""))
    print(f"{'='*60}")

    if dry_run:
        # dry-run 不落盘：不建目录、不写 task.json，只打印将执行的命令
        if LEGACY_MODE:
            print(f"[DRY-RUN] legacy 单会话: {KIMI_BIN} -p '<{TEMPLATE_PATH.name} 渲染后的 prompt>'")
        else:
            print(f"[DRY-RUN] phase1: {KIMI_BIN} -p '<{PHASE1_TEMPLATE_PATH.name} 渲染后的 prompt>'")
            print(f"[DRY-RUN] phase2: 按 phase1.json 清单每 {BATCH_SIZE} 篇一批起会话"
                  f"（{PHASE2_TEMPLATE_PATH.name}），最后合并 result.json + Word 报告")
        return {"task_id": task_id, "status": "dry_run", "session_id": None}

    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "screenshots").mkdir(parents=True, exist_ok=True)

    # 1. 保存任务定义副本
    (task_dir / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2. 清理旧产出 [M5]；--resume 且 phase1 已成功则保留进度跳过 Phase 1
    resume_active = False
    phase1_existing = None
    old_wire = ""
    if resume:
        phase1_existing = _read_json(task_dir / "phase1.json")
        if (phase1_existing or {}).get("status") == "success":
            resume_active = True
            old_wire = ((task_dir / "wire.jsonl").read_text(encoding="utf-8")
                        if (task_dir / "wire.jsonl").exists() else "")
            print(f"[续跑] phase1.json 已就绪（近五年论文 "
                  f"{len(phase1_existing.get('recent_papers') or [])} 篇），跳过 Phase 1")
        else:
            print("[续跑] phase1.json 缺失或未完成，退化为全新任务执行")
    if not resume_active:
        clean_task_outputs(task, task_dir)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    start_time = datetime.now(timezone.utc)

    # ---- legacy 单会话模式（--template 回退路径，top-10 旧契约） ----
    if LEGACY_MODE:
        return _run_one_task_legacy(task, task_dir, start_time)

    # ================= Phase 1：谷歌学术 + 论文清单（年份口径见 year_rule） =================
    if resume_active:
        # 续跑：phase1 直接取磁盘上的成果，不起会话；浏览器产物沿用已有文件
        phase1 = phase1_existing
        session_id, session_dir = None, None
        has_trace = (task_dir / "trace.zip").exists()
        has_screenshot = (task_dir / "screenshots" / f"{task_id}_profile.png").exists()
        t0 = t1 = start_time
        returncode = 0  # phase1 未起会话，视为成功（避免下方 build_record 引用未赋值变量）
        wire_frags = []
    else:
        prompt = render_prompt(task, PHASE1_TEMPLATE_PATH,
                               extra={"YEAR_RULE": year_rule(task)})
        log_path = LOG_DIR / f"{task_id}.log"
        sessions_before = snapshot_sessions()
        mcp_before = snapshot_mcp_output()
        wire_frags = []

        returncode, t0, t1 = run_kimi_session(prompt, log_path, PHASE1_TIMEOUT, label="phase1")
        session_id, session_dir = locate_session(log_path, sessions_before)
        wire_frags.append(collect_wire_fragment(session_dir))
        has_trace, has_screenshot = collect_browser_artifacts(mcp_before, task, task_dir)

        phase1 = _read_json(task_dir / "phase1.json")
        p1_status = (phase1 or {}).get("status") or (
            "no_result" if phase1 is None else "unknown")

        # phase1 失败（captcha/not_found/没写文件）：写最小 result.json 保持下游语义，
        # 不进 phase2。main 的 captcha/429 重试逻辑照旧作用于 record["status"]。
        if p1_status != "success":
            result = {k: (phase1 or {}).get(k) for k in
                      ("task_id", "person_name", "affiliation", "interests",
                       "total_citations", "h_index", "i10_index", "top_papers", "profile_url")}
            result["task_id"] = task_id
            result["status"] = p1_status
            result["note"] = (phase1 or {}).get("note") or (
                None if phase1 is not None else "Agent 未写入 phase1.json")
            (task_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            (task_dir / "wire.jsonl").write_text(old_wire + "".join(wire_frags), encoding="utf-8")
            annotate_run_info(task_dir, session_id, t0, t1, batch_session_ids=[])
            record = build_record(
                task=task, session_id=session_id, start_time=start_time, end_time=t1,
                returncode=returncode, status=read_status(task_dir),
                has_result=True, has_screenshot=has_screenshot, has_trace=has_trace,
                trajectory_collected=bool(wire_frags[0]),
                failure_reason=derive_failure_reason(task_dir, read_status(task_dir), returncode))
            append_mapping(record)
            print(f"[完成] phase1 状态: {p1_status} | 未进入核查阶段")
            return record

    # ================= Phase 2：S2 逐篇核查（每 BATCH_SIZE 篇一个新会话） =================
    papers = (phase1.get("recent_papers") or [])
    ranked = rank_papers(papers)
    (task_dir / "ranked_papers.json").write_text(
        json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")
    batches = [ranked[i:i + BATCH_SIZE] for i in range(0, len(ranked), BATCH_SIZE)]
    print(f"[phase1] 近五年论文 {len(ranked)} 篇，分 {len(batches)} 批核查（每批 ≤{BATCH_SIZE} 篇）")

    batch_session_ids = []
    returncode_2 = 0
    end_time = t1
    try:
        for bi, batch in enumerate(batches, start=1):
            print(f"[批次{bi}/{len(batches)}] rank {batch[0]['rank']}~{batch[-1]['rank']}")
            if resume_active and not fragments_missing(task_dir, batch):
                print(f"[批次{bi}] fragment 已全部就位，续跑跳过")
                continue
            for attempt in (1, 2):
                # 每次起会话前按当前磁盘状态补缺：首轮=全批，重试/续跑=只缺的部分
                batch_todo = fragments_missing(task_dir, batch)
                if not batch_todo:
                    break
                prompt_b = render_prompt(task, PHASE2_TEMPLATE_PATH, extra={
                    "PAPERS_JSON": json.dumps(batch_todo, ensure_ascii=False, indent=1),
                    "BATCH_SIZE": len(batch_todo),
                })
                log_b = LOG_DIR / f"{task_id}.log"  # 所有会话追加进同一日志，批间有分隔头
                sb = snapshot_sessions()
                mb = snapshot_mcp_output()
                rc_b, _, end_time = run_kimi_session(
                    prompt_b, log_b, BATCH_TIMEOUT,
                    label=f"批次{bi}/{len(batches)} rank {batch[0]['rank']}~{batch[-1]['rank']}",
                    append=True)
                returncode_2 = rc_b
                sid_b, sdir_b = locate_session(log_b, sb)
                batch_session_ids.append(sid_b)
                wire_frags.append(collect_wire_fragment(sdir_b))
                # 续跑起的新 trace 加后缀，避免覆盖上一轮已有的同名 trace
                trace_tag = f"_resume_r{attempt}" if resume_active else ""
                bt, bs = collect_browser_artifacts(
                    mb, task, task_dir, trace_name=f"trace_batch{bi:02d}{trace_tag}.zip")
                has_trace = has_trace or bt
                has_screenshot = has_screenshot or bs
                missing = fragments_missing(task_dir, batch)
                if not missing:
                    break
                if attempt == 1:
                    print(f"[批次{bi}] 缺 {len(missing)} 篇 fragment（returncode={rc_b}），重试一次...")
            missing = fragments_missing(task_dir, batch)
            if missing:
                print(f"[批次{bi}] 重试后仍缺 {len(missing)} 篇：rank "
                      f"{[p['rank'] for p in missing]}，合并时按'核查会话未完成'处理"
                      f"（--resume 可再次续跑补齐）")
            # 每批结束（无论成败）都把当前已有数据合并进 result.json；
            # 批内中途被硬杀的场景由 --merge-only 事后重建（fragment 每篇即落盘）
            st, ms = merge_result(task_dir)
            print(f"[批次{bi}] result.json 已刷新（状态 {st}，缺 {ms} 篇，含未开始的批次）")
    except BaseException as e:
        # 异常/键盘中断兜底：先把当前进度合并进 result.json 再向上抛
        merge_result(task_dir)
        print(f"[中断] {type(e).__name__}：已把当前进度写入 result.json")
        raise

    # ================= Phase 3：合并 + Word 报告 =================
    # 只有全部 fragment 就位才清理 checks/：缺篇时保留，--resume 可再次续跑补齐
    status, miss = merge_result(task_dir)
    if miss == 0:
        shutil.rmtree(task_dir / "checks", ignore_errors=True)
    else:
        print(f"[合并] 缺 {miss} 篇 fragment，checks/ 保留待续跑")
    print(f"[合并] result.json 状态: {status}（缺 fragment {miss} 篇）")

    (task_dir / "wire.jsonl").write_text(old_wire + "".join(wire_frags), encoding="utf-8")
    annotate_run_info(task_dir, session_id, t0, end_time,
                      batch_session_ids=batch_session_ids)

    if status in ("success", "partial"):
        try:
            import export_word
            out = export_word.generate_report(task_dir)
            print(f"[Word] {out}" if out else "[Word] 未生成（无 result.json）")
        except Exception as e:
            print(f"[警告] Word 报告生成失败（不阻断任务）: {e}")

    record = build_record(
        task=task, session_id=session_id, start_time=start_time, end_time=end_time,
        returncode=returncode if returncode != 0 else returncode_2,
        status=read_status(task_dir),
        has_result=(task_dir / "result.json").exists(),
        has_screenshot=has_screenshot, has_trace=has_trace,
        trajectory_collected=any(wire_frags),
        failure_reason=derive_failure_reason(task_dir, read_status(task_dir),
                                             returncode if returncode != 0 else returncode_2))
    append_mapping(record)

    trajectory_mark = "OK" if record["trajectory_collected"] else "FAIL"
    trace_mark = "OK" if has_trace else "MISS"
    shot_mark = "OK" if has_screenshot else "MISS"
    print(f"[完成] 状态: {record['status']} | 耗时: {record['duration_seconds']}s | "
          f"轨迹: {trajectory_mark} | trace: {trace_mark} | 截图: {shot_mark}")
    if session_id:
        print(f"[会话] phase1={session_id} | 批次会话 {len(batch_session_ids)} 个")
    return record


def _run_one_task_legacy(task: dict, task_dir: Path, start_time) -> dict:
    """旧单会话链路（--template 指定旧模板时使用）：一个会话完成检索+核查，top-10。"""
    task_id = task["task_id"]
    log_path = LOG_DIR / f"{task_id}.log"
    prompt = render_prompt(task, TEMPLATE_PATH,
                           extra={"YEAR_RULE": year_rule(task, top10=True)})
    sessions_before = snapshot_sessions()
    mcp_output_before = snapshot_mcp_output()

    returncode, t0, end_time = run_kimi_session(
        prompt, log_path, PHASE1_TIMEOUT, label="legacy")

    session_id, session_dir = locate_session(log_path, sessions_before)
    wire = collect_wire_fragment(session_dir)
    (task_dir / "wire.jsonl").write_text(wire, encoding="utf-8")
    has_trace, has_screenshot = collect_browser_artifacts(mcp_output_before, task, task_dir)

    status = read_status(task_dir)
    annotate_run_info(task_dir, session_id, t0, end_time)

    record = build_record(
        task=task, session_id=session_id, start_time=start_time, end_time=end_time,
        returncode=returncode, status=status,
        has_result=(task_dir / "result.json").exists(),
        has_screenshot=has_screenshot, has_trace=has_trace,
        trajectory_collected=bool(wire),
        failure_reason=derive_failure_reason(task_dir, status, returncode))
    append_mapping(record)

    trajectory_mark = "OK" if record["trajectory_collected"] else "FAIL"
    trace_mark = "OK" if has_trace else "MISS"
    shot_mark = "OK" if has_screenshot else "MISS"
    print(f"[完成] 状态: {status} | 耗时: {record['duration_seconds']}s | "
          f"轨迹: {trajectory_mark} | trace: {trace_mark} | 截图: {shot_mark}")
    if session_id:
        print(f"[会话] {session_id}")
    return record


# =============================================================================
# [M9] 批量主循环与 CLI
#   - 反爬参数（任务间延迟 30-90s、CAPTCHA 冷却 30-45 分钟、重试次数）在此调
#   - check_mcp_config：跑批前自检 MCP 反检测配置（--config/--init-script）
#   - 熔断：连续 --max-consecutive-captcha 个任务 CAPTCHA 未解除即终止批次，
#     防止 IP 被标记后继续硬跑（谷歌日均可遇上亿爬虫，硬冲没有胜算）
#   - 并发锁 [M7] 只罩真实执行，--dry-run 不需要锁
# =============================================================================

def check_mcp_config():
    """跑批前自检 ~/.kimi-code/mcp.json 的反检测配置是否就位。

    只警告不阻断：配置漂移是已被实测过的故障模式（缺 --save-trace 导致
    整批没有 trace.zip；task_0001 会话没加载到 MCP 工具），但测试性
    运行不应被自检卡死。
    """
    problems = []
    if not MCP_CONFIG_PATH.exists():
        problems.append(f"找不到 MCP 配置文件: {MCP_CONFIG_PATH}")
    else:
        try:
            args = (json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
                    .get("mcpServers", {}).get("playwright", {}).get("args", []))
        except json.JSONDecodeError as e:
            problems.append(f"MCP 配置文件不是合法 JSON: {e}")
            args = []
        joined = " ".join(str(a) for a in args)
        for flag in ("--save-trace", "--headless", "--config", "--init-script"):
            if flag not in joined:
                problems.append(f"mcp.json 缺少 {flag} 参数")
        if "--config" in joined and str(MCP_STEALTH_CONFIG).replace("\\", "/") not in joined.replace("\\", "/"):
            problems.append(f"--config 未指向 {MCP_STEALTH_CONFIG}")
        if "--init-script" in joined and str(STEALTH_INIT_SCRIPT).replace("\\", "/") not in joined.replace("\\", "/"):
            problems.append(f"--init-script 未指向 {STEALTH_INIT_SCRIPT}")
    for f in (MCP_STEALTH_CONFIG, STEALTH_INIT_SCRIPT):
        if not f.exists():
            problems.append(f"反检测文件缺失: {f}")
    if problems:
        print("[警告] MCP 反检测配置自检未通过，谷歌学术拦截率会显著升高：")
        for p in problems:
            print(f"       - {p}")
        print("       按 docs/QA1.md「反检测配置」节修正 mcp.json 并重启会话后再跑。")
    else:
        print("[自检] MCP 反检测配置就位（--config / --init-script / --save-trace）")


def main():
    parser = argparse.ArgumentParser(description="批量执行谷歌学术人物检索任务（三段式管线）")
    parser.add_argument("--limit", type=int, help="只跑前 N 条任务")
    parser.add_argument("--start-from", help="从指定 task_id 开始（含）")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    parser.add_argument("--no-delay", action="store_true", help="禁用反爬延迟（测试用）")
    parser.add_argument("--max-captcha-retry", type=int, default=2, help="CAPTCHA 最大重试次数")
    parser.add_argument("--max-429-retry", type=int, default=2,
                        help="API 限流（429）导致会话中止时的最大重试次数（默认 2，冷却 2-5 分钟）")
    parser.add_argument("--max-consecutive-captcha", type=int, default=2,
                        help="连续 N 个任务 CAPTCHA 未解除则熔断终止批次（默认 2）")
    parser.add_argument("--merge-only", metavar="TASK_ID",
                        help="不跑任务，只把该任务磁盘上已有的 phase1.json + checks/ "
                             "重新合并成 result.json（用于批内被硬杀、进程崩溃后重建产物）")
    parser.add_argument("--resume", action="store_true",
                        help="断点续跑：phase1.json 已成功则跳过 Phase 1、不清理旧产出，"
                             "Phase 2 只补缺失的 fragment（默认全新重跑并清理旧产出）")
    parser.add_argument("--status", metavar="TASK_ID",
                        help="只查看该任务在磁盘上的完成状态（phase1/fragment/result），不执行")
    parser.add_argument("--model", help="指定模型（覆盖环境变量 AGENT_MODEL 和默认值）")
    parser.add_argument("--template",
                        help="【legacy 单会话模式】旧版 prompt 模板文件名"
                             "（相对 scripts/ 目录或绝对路径，如 task_prompt_template_s2.md）；"
                             "指定后走旧单会话 top-10 链路，默认不启用")
    args = parser.parse_args()

    # --status：只打印任务磁盘状态，不执行（中断后先看完成到哪再决定 --resume）
    if args.status:
        show_task_state(args.status)
        return

    # --merge-only：只重建 result.json，不跑任务（硬杀/崩溃后的产物恢复）
    if args.merge_only:
        task_dir = PROJECT_ROOT / "data" / args.merge_only
        if not task_dir.is_dir():
            print(f"[ERROR] 任务目录不存在: {task_dir}")
            return
        status, miss = merge_result(task_dir)
        # 只有全部就位才清 checks/：缺篇时保留 fragment 供 --resume 续跑补齐
        if status and miss == 0:
            shutil.rmtree(task_dir / "checks", ignore_errors=True)
        if status is None:
            print(f"[ERROR] {args.merge_only} 缺少 phase1.json，无法合并")
            return
        print(f"[合并] {args.merge_only}: status={status}，缺 fragment {miss} 篇"
              f" -> {task_dir / 'result.json'}")
        return

    # 应用命令行参数中的模型设置
    global MODEL, TEMPLATE_PATH, LEGACY_MODE
    if args.model:
        MODEL = args.model

    # --template 进入 legacy 单会话模式（默认三段式不需要该参数）
    if args.template:
        t = Path(args.template)
        if not t.is_absolute():
            t = PROJECT_ROOT / "scripts" / t
        if not t.exists():
            print(f"[ERROR] 找不到 prompt 模板: {t}")
            return
        TEMPLATE_PATH = t
        LEGACY_MODE = True

    # 加载任务列表
    tasks_path = PROJECT_ROOT / "tasks" / "tasks.jsonl"
    all_tasks = load_tasks(tasks_path)

    # 过滤任务
    tasks_to_run = all_tasks
    if args.start_from:
        start_idx = next((i for i, t in enumerate(all_tasks) if t["task_id"] == args.start_from), None)
        if start_idx is None:
            print(f"[ERROR] 找不到起始任务: {args.start_from}")
            return
        tasks_to_run = all_tasks[start_idx:]

    if args.limit:
        tasks_to_run = tasks_to_run[:args.limit]

    print(f"[启动] 共 {len(tasks_to_run)} 条任务待执行")
    if LEGACY_MODE:
        print(f"[配置] 框架={FRAMEWORK} | 模型={MODEL} | 模式=legacy 单会话 | 模板={TEMPLATE_PATH.name} | 反爬延迟={'关闭' if args.no_delay else '开启'}")
    else:
        print(f"[配置] 框架={FRAMEWORK} | 模型={MODEL} | 模式=三段式 | 批大小={BATCH_SIZE} | 反爬延迟={'关闭' if args.no_delay else '开启'}")
    check_mcp_config()  # [M9] 跑批前自检反检测配置（只警告不阻断）

    if args.dry_run:
        for task in tasks_to_run:
            run_one_task(task, dry_run=True)
        return

    # 并发保护 [M7]：同一时刻只允许一个执行器
    if not acquire_lock():
        print(f"[ERROR] 检测到锁文件 {LOCK_PATH}，已有执行器在运行（或上次异常退出）。")
        print(f"        确认没有正在运行的批次后，删除该锁文件再重试。")
        return

    try:
        # L2 启动兜底：自动重建上次中断/崩溃残留的 result.json
        recover_interrupted()

        # 主循环
        success_count = 0
        captcha_count = 0
        failed_count = 0
        consecutive_captcha = 0  # 连续 CAPTCHA 未解除计数（熔断用）

        for i, task in enumerate(tasks_to_run):
            # 执行任务 [M8]
            record = run_one_task(task, resume=args.resume)

            # 429 限流重试：CLI 因限流中止（日志含 429 / RateLimit，
            # 常见于小模型 compaction 请求被限流）时，冷却 2-5 分钟后重跑。
            # 限流是临时性故障，重跑成本低，与 CAPTCHA 的长冷却分开处理。
            retry_429 = 0
            while (record["returncode"] != 0
                   and log_has_rate_limit(task["task_id"])
                   and retry_429 < args.max_429_retry):
                retry_429 += 1
                delay = random.uniform(120, 300)
                print(f"[429] 检测到 API 限流导致会话中止，冷却 {delay/60:.0f} 分钟后重试 "
                      f"({retry_429}/{args.max_429_retry})...")
                time.sleep(delay)
                record = run_one_task(task, resume=args.resume)

            # 统计结果
            if record["status"] == "success":
                success_count += 1
                consecutive_captcha = 0
            elif record["status"] == "captcha":
                captcha_count += 1
                # CAPTCHA 重试逻辑：冷却 30 分钟以上再试。
                # 触发后几分钟内硬冲只会加重封禁（task_0007/0008 三次连灭的教训）。
                retry = 0
                while retry < args.max_captcha_retry:
                    retry += 1
                    delay = random.uniform(1800, 2700)  # CAPTCHA 后冷却 30-45 分钟
                    print(f"[CAPTCHA] 检测到验证码，冷却 {delay/60:.0f} 分钟后重试 ({retry}/{args.max_captcha_retry})...")
                    time.sleep(delay)

                    record = run_one_task(task, resume=args.resume)
                    if record["status"] != "captcha":
                        break

                if record["status"] == "success":
                    success_count += 1
                    consecutive_captcha = 0
                else:
                    failed_count += 1
                    consecutive_captcha += 1
            else:
                failed_count += 1
                consecutive_captcha = 0

            # 熔断：连续 N 个任务重试后仍 CAPTCHA，说明 IP 已被谷歌标记，
            # 继续跑只会加重封禁且全是废数据，直接终止本批次。
            if consecutive_captcha >= args.max_consecutive_captcha:
                print(f"[熔断] 连续 {consecutive_captcha} 个任务 CAPTCHA 未解除，"
                      f"IP 可能已被谷歌标记，终止本批次（剩余 {len(tasks_to_run) - i - 1} 条）。")
                print(f"       建议冷却数小时后用 --start-from {task['task_id']} 续跑。")
                break

            # 反爬延迟：任务间随机等待
            if not args.no_delay and i < len(tasks_to_run) - 1:
                delay = random.uniform(30, 90)  # 30-90秒
                print(f"[延迟] {delay:.0f}秒后执行下一条任务...")
                time.sleep(delay)

        # 总结
        print(f"\n{'='*60}")
        print(f"[总结] 执行完毕")
        print(f"  成功: {success_count}")
        print(f"  验证码/失败: {captcha_count + failed_count}")
        print(f"  映射表: {MAPPING_PATH}")
        print(f"{'='*60}")
    finally:
        release_lock()


if __name__ == "__main__":
    main()

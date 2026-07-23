#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量执行器：读 tasks.jsonl，逐条拉起 Kimi Code 会话执行谷歌学术人物检索任务。

依赖：
    - Kimi Code CLI 已安装（通常在 ~/.kimi-code/bin/kimi.exe）
    - Playwright MCP 已配置（~/.kimi-code/mcp.json，需固定 @playwright/mcp@0.0.64
      并带 --headless --save-trace，0.0.65 起官方移除了 --save-trace）
    - Python 3.8+

用法：
    python scripts/run_tasks.py                          # 跑全部任务
    python scripts/run_tasks.py --limit 3                # 只跑前 3 条
    python scripts/run_tasks.py --start-from task_0003   # 从某条开始（含）
    python scripts/run_tasks.py --dry-run                # 只打印将执行的命令，不真跑

产出（每条任务，data/<task_id>/ 标准目录，严格 5 项）：
    task.json                     任务定义副本（执行器写入）
    result.json                   Agent 抽取的结果（Agent 写入；执行器跑完后
                                  补写 _run 执行元信息，见 [M5] annotate_run_info）
    wire.jsonl                    会话完整轨迹（从 Kimi 会话目录复制）
    trace.zip                     Playwright 浏览器侧轨迹（MCP --save-trace 产出）
    screenshots/<task_id>_profile.png  整页截图（执行器从 MCP 输出目录归档）
    data/mapping.jsonl            task_id <-> session_id <-> 框架 映射表（追加写）

    执行日志写在 logs/<task_id>.log（项目根 logs/ 目录，不属于交付目录），
    供调试与解析 session_id 用。

注意：
    - 执行前会清理任务目录里的旧产出（result.json / wire.jsonl / trace.zip /
      screenshots/），避免失败任务"继承"上一次成功运行的旧结果。
    - 同一时刻只允许一个执行器实例（data/.runner.lock），防止并发跑批把
      同一 IP 打到谷歌学术限流、以及 session 归属错乱。

模块导航（按功能分区，改哪块直接跳哪块）：
    [M1] 配置区 ................ 路径常量、框架/模型、CLI 路径检测
    [M2] 任务加载与 Prompt 渲染 . load_tasks / render_prompt
    [M3] Session 定位 .......... snapshot_sessions / detect_new_session /
                                 parse_session_id_from_log / find_session_dir
    [M4] 产物收集 .............. collect_trajectory（wire.jsonl）/
                                 snapshot_mcp_output / _pack_trace_zip /
                                 collect_browser_artifacts（trace.zip + 截图）
    [M5] 清理与状态判定 ........ clean_task_outputs / read_status /
                                 annotate_run_info（跑完补写 result.json 的 _run）
    [M6] 映射表记录 ............ build_record / append_mapping（mapping.jsonl schema）
    [M7] 并发锁 ................ acquire_lock / release_lock
    [M8] 单任务执行主流程 ...... run_one_task（串联 M2-M7）
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
# [M1] 配置区：路径常量、框架/模型标识、CLI 路径检测
#   - 改框架或模型：改 FRAMEWORK / MODEL（写入 mapping 的标识字段）
#   - 改 MCP 输出目录：必须与 ~/.kimi-code/mcp.json 的 --output-dir 保持一致
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "scripts" / "task_prompt_template.md"
MAPPING_PATH = PROJECT_ROOT / "data" / "mapping.jsonl"
LOCK_PATH = PROJECT_ROOT / "data" / ".runner.lock"
LOG_DIR = PROJECT_ROOT / "logs"  # 执行日志（非交付物，仅调试/解析 session_id 用）
SESSIONS_ROOT = Path.home() / ".kimi-code" / "sessions"
# playwright-mcp 的输出目录（与 ~/.kimi-code/mcp.json 的 --output-dir 一致）
MCP_OUTPUT_DIR = PROJECT_ROOT / ".playwright-mcp"

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
MODEL = "kimi-for-coding/k3"


# =============================================================================
# [M2] 任务加载与 Prompt 渲染
#   - 任务字段变更（如甲方清单加字段）：改 load_tasks / render_prompt
#   - 模板本身在 scripts/task_prompt_template.md，占位符 {{...}} 在此替换
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


def render_prompt(task: dict) -> str:
    """把任务字段填进 prompt 模板的占位符。"""
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    name = task["person_name"]
    return (tpl
            .replace("{{TASK_ID}}", task["task_id"])
            .replace("{{PERSON_NAME}}", name)
            .replace("{{PERSON_NAME_URLENCODED}}", urllib.parse.quote_plus(name))
            .replace("{{AFFILIATION_HINT}}", task.get("affiliation_hint", "")))


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


# =============================================================================
# [M4] 产物收集：wire.jsonl（Agent 轨迹）+ trace.zip / 截图（浏览器侧产物）
#   - trace 来源：MCP --save-trace 落盘裸文件，_pack_trace_zip 按 Playwright
#     标准布局打包成契约要求的 trace.zip
#   - 截图：Agent 截到 MCP 输出目录（或项目根），执行器归档进 screenshots/
# =============================================================================

def collect_trajectory(session_dir: str, task_dir: Path) -> bool:
    """把会话的 wire.jsonl 完整轨迹复制到任务目录。"""
    if not session_dir:
        return False
    wire = Path(session_dir) / "agents" / "main" / "wire.jsonl"
    if not wire.exists():
        return False
    shutil.copy(str(wire), str(task_dir / "wire.jsonl"))
    return True


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
                    zf.write(r, str(Path("resources") / r.relative_to(resources)))
    return True


def collect_browser_artifacts(before: dict, task: dict, task_dir: Path):
    """从 MCP 输出目录收归浏览器侧产物：trace.zip 和整页截图。

    返回 (has_trace, has_screenshot)。
    """
    task_id = task["task_id"]

    # --- trace.zip：打包本次运行新产生的 trace 原始文件 ---
    has_trace = _pack_trace_zip(MCP_OUTPUT_DIR / "traces", before, task_dir / "trace.zip")

    # --- 截图：归档到 screenshots/ 子目录 ---
    has_screenshot = False
    screenshots_dir = task_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    png_name = f"{task_id}_profile.png"
    # Agent 按要求命名截图，可能落在 MCP 输出目录或项目根目录
    for candidate in (MCP_OUTPUT_DIR / png_name, PROJECT_ROOT / png_name):
        if candidate.exists():
            shutil.move(str(candidate), str(screenshots_dir / png_name))
            has_screenshot = True
            break
    if not has_screenshot:
        # 兜底：本次运行新产生的任意 png
        new_pngs = []
        if MCP_OUTPUT_DIR.exists():
            for p in MCP_OUTPUT_DIR.glob("*.png"):
                mtime = p.stat().st_mtime
                if _is_new(str(p.relative_to(MCP_OUTPUT_DIR)), mtime, before):
                    new_pngs.append((mtime, p))
        if new_pngs:
            newest = max(new_pngs)[1]
            shutil.move(str(newest), str(screenshots_dir / png_name))
            has_screenshot = True

    return has_trace, has_screenshot


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
    for name in ("result.json", "wire.jsonl", "trace.zip", f"{task_id}_profile.png", "run.log"):
        (task_dir / name).unlink(missing_ok=True)
    shutil.rmtree(task_dir / "screenshots", ignore_errors=True)


def read_status(task_dir: Path) -> str:
    """读 result.json 的 status 字段，判断任务结果。"""
    rj = task_dir / "result.json"
    if not rj.exists():
        return "no_result"
    try:
        return json.loads(rj.read_text(encoding="utf-8")).get("status", "unknown")
    except json.JSONDecodeError:
        return "invalid_result"


def annotate_run_info(task_dir: Path, session_id, start_time, end_time):
    """任务跑完后把执行元信息补写进 result.json 的 _run 字段。

    必须由执行器（而非 Agent）写入：Agent 不知道自己的 session_id，
    在 prompt 里要这个值只会得到编造值。steps 取 wire.jsonl 中
    step.begin 事件计数；result.json 缺失或损坏时静默跳过（状态由
    read_status 另行判定）。
    """
    rj = task_dir / "result.json"
    if not rj.exists():
        return
    try:
        data = json.loads(rj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
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
    rj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def derive_failure_reason(task_dir: Path, status: str, returncode: int):
    """生成 mapping 用的单行失败原因；status 为 success 时返回 None。

    优先取 Agent 写在 result.json 里的 note（captcha/not_found 的场景说明），
    其次按 returncode / status 给出 runner 侧判断。
    """
    if status == "success":
        return None
    rj = task_dir / "result.json"
    if rj.exists():
        try:
            note = json.loads(rj.read_text(encoding="utf-8")).get("note")
            if note:
                return note
        except json.JSONDecodeError:
            pass
    if returncode == -1:
        return "任务超时（10 分钟）"
    if returncode == -2:
        return "CLI 执行异常"
    if status == "no_result":
        return "Agent 未写入 result.json"
    if status == "invalid_result":
        return "result.json 不是合法 JSON"
    return status  # captcha / not_found 等但 Agent 没写 note


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
# [M8] 单任务执行主流程：串联 M2-M7
#   步骤编号即数据流向：任务定义 -> prompt -> 起会话 -> 定位 session ->
#   收产物 -> 判状态 -> 写映射。改任一环节先看对应模块的区注释。
# =============================================================================

def run_one_task(task: dict, dry_run: bool = False) -> dict:
    """执行单个任务，返回执行记录。"""
    task_id = task["task_id"]
    task_dir = PROJECT_ROOT / "data" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # 1. 保存任务定义副本
    (task_dir / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2. 渲染 prompt [M2]
    prompt = render_prompt(task)

    # 3. 构造 Kimi Code 命令（直接使用可执行文件路径）
    cmd = [str(KIMI_BIN), "-p", prompt]

    print(f"\n{'='*60}")
    print(f"[{task_id}] {task['person_name']} @ {task.get('affiliation_hint', 'N/A')}")
    print(f"{'='*60}")

    if dry_run:
        print(f"[DRY-RUN] 将执行: {KIMI_BIN} -p '<prompt>'")
        return {"task_id": task_id, "status": "dry_run", "session_id": None}

    # 4. 清理旧产出 [M5]，快照执行前的 session 列表 [M3] 与 MCP 输出目录 [M4]
    clean_task_outputs(task, task_dir)
    sessions_before = snapshot_sessions()
    mcp_output_before = snapshot_mcp_output()

    # 5. 执行 Kimi Code，捕获输出到 logs/<task_id>.log（非交付物）
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{task_id}.log"
    start_time = datetime.now(timezone.utc)

    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            result = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=600,  # 10分钟超时
                shell=False
            )
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        print(f"[ERROR] 任务超时（10分钟）")
        returncode = -1
    except Exception as e:
        print(f"[ERROR] 执行失败: {e}")
        returncode = -2

    end_time = datetime.now(timezone.utc)

    # 6. 定位 session [M3]：优先解析执行日志中 CLI 自报的 session id，目录差分兜底
    session_id = parse_session_id_from_log(log_path)
    session_dir = find_session_dir(session_id) if session_id else None
    if not session_dir:
        session_dir = detect_new_session(sessions_before)
        session_id = Path(session_dir).name if session_dir else None

    # 7. 收集产物 [M4]：wire.jsonl + 浏览器侧产物（trace.zip + 截图）
    trajectory_collected = collect_trajectory(session_dir, task_dir) if session_dir else False
    has_trace, has_screenshot = collect_browser_artifacts(mcp_output_before, task, task_dir)

    # 8. 读取任务状态 [M5]
    status = read_status(task_dir)

    # 8.5 补写 _run 执行元信息 [M5]：执行器职责，不写进 prompt 让 Agent 填
    #     （Agent 不知道自己的 session_id，只会编造）
    annotate_run_info(task_dir, session_id, start_time, end_time)

    # 9. 构造执行记录 [M6]（统一 schema，含单行失败原因）
    record = build_record(
        task=task,
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        returncode=returncode,
        status=status,
        has_result=(task_dir / "result.json").exists(),
        has_screenshot=has_screenshot,
        has_trace=has_trace,
        trajectory_collected=trajectory_collected,
        failure_reason=derive_failure_reason(task_dir, status, returncode),
    )

    # 10. 追加写入 mapping.jsonl [M6]
    append_mapping(record)

    # 11. 打印结果摘要
    trajectory_mark = "OK" if trajectory_collected else "FAIL"
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
    parser = argparse.ArgumentParser(description="批量执行谷歌学术人物检索任务")
    parser.add_argument("--limit", type=int, help="只跑前 N 条任务")
    parser.add_argument("--start-from", help="从指定 task_id 开始（含）")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    parser.add_argument("--no-delay", action="store_true", help="禁用反爬延迟（测试用）")
    parser.add_argument("--max-captcha-retry", type=int, default=2, help="CAPTCHA 最大重试次数")
    parser.add_argument("--max-consecutive-captcha", type=int, default=2,
                        help="连续 N 个任务 CAPTCHA 未解除则熔断终止批次（默认 2）")
    args = parser.parse_args()

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
    print(f"[配置] 框架={FRAMEWORK} | 模型={MODEL} | 反爬延迟={'关闭' if args.no_delay else '开启'}")
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
        # 主循环
        success_count = 0
        captcha_count = 0
        failed_count = 0
        consecutive_captcha = 0  # 连续 CAPTCHA 未解除计数（熔断用）

        for i, task in enumerate(tasks_to_run):
            # 执行任务 [M8]
            record = run_one_task(task)

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

                    record = run_one_task(task)
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

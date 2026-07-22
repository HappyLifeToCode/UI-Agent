#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据完整性检查器：验证任务输出的完整性和质量

用法：
    python qa/validate_data.py                     # 检查所有任务
    python qa/validate_data.py --task-id task_0001 # 检查特定任务

检查项对应质量红线：产物缺项、轨迹断档（tool.call/tool.result、
step.begin/step.end 不配对）会以 issue 报出并使退出码为 1，不静默丢弃。
"""
import argparse
import json
import platform
import sys
from pathlib import Path

if platform.system() == "Windows":
    sys.stdout.reconfigure(encoding='utf-8')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def validate_task(task_dir: Path) -> dict:
    """验证单个任务目录的完整性"""
    task_id = task_dir.name
    issues = []
    warnings = []

    # 1. 检查必需文件（契约：data/<task_id>/ 严格 5 项）
    png_rel = f"screenshots/{task_id}_profile.png"
    required_files = {
        "task.json": False,
        "result.json": False,
        "wire.jsonl": False,
        "trace.zip": False,
        png_rel: False,
    }

    for filename in required_files:
        file_path = task_dir / filename
        if file_path.exists():
            required_files[filename] = True
        else:
            issues.append(f"缺失文件: {filename}")

    # 2. 验证 result.json
    result_json = task_dir / "result.json"
    result_data = None
    if result_json.exists():
        try:
            result_data = json.loads(result_json.read_text(encoding="utf-8"))

            # 检查必需字段
            required_fields = ["task_id", "person_name", "status"]
            for field in required_fields:
                if field not in result_data:
                    issues.append(f"result.json 缺少字段: {field}")

            # 检查状态
            status = result_data.get("status")

            # _run 执行元信息（由执行器跑完后补写，非 Agent 职责；早期数据可能没有）
            if "_run" not in result_data:
                warnings.append("缺少 _run 执行元信息（应由执行器跑完后补写）")

            if status == "success":
                # 成功状态下，检查数据完整性
                if "total_citations" not in result_data:
                    warnings.append("缺少 total_citations")
                elif not isinstance(result_data["total_citations"], int):
                    issues.append(f"total_citations 应为整数，实际为: {type(result_data['total_citations'])}")

                if "top_papers" not in result_data:
                    warnings.append("缺少 top_papers")
                elif len(result_data.get("top_papers", [])) < 3:
                    warnings.append(f"top_papers 少于3篇，实际: {len(result_data.get('top_papers', []))}")

        except json.JSONDecodeError as e:
            issues.append(f"result.json 格式错误: {e}")

    # 3. 验证 wire.jsonl（完整性 + 断档检测）
    wire_jsonl = task_dir / "wire.jsonl"
    if wire_jsonl.exists():
        try:
            lines = wire_jsonl.read_text(encoding="utf-8").splitlines()
            line_count = len([l for l in lines if l.strip()])

            if line_count < 50:
                warnings.append(f"wire.jsonl 行数过少: {line_count} < 50（可能轨迹不完整）")

            # --- 断档检测（质量红线：发现断档必须记录，不允许静默丢弃）---
            # 配对关系：llm.request ↔ step.end（一次模型请求一个步骤收尾）、
            # tool.call ↔ tool.result（工具调用必须有返回）。不配对即断档迹象。
            counts = {"llm.request": 0, "step.begin": 0, "step.end": 0,
                      "tool.call": 0, "tool.result": 0}
            for line in lines:
                for key in counts:
                    if f'"{key}"' in line:
                        counts[key] += 1

            if counts["tool.call"] < 5:
                warnings.append(f"tool_call 数量过少: {counts['tool.call']}（可能任务未正常执行）")

            if counts["tool.call"] != counts["tool.result"]:
                issues.append(
                    f"轨迹断档迹象: tool.call={counts['tool.call']} 与 "
                    f"tool.result={counts['tool.result']} 不配对")
            if counts["step.begin"] != counts["step.end"]:
                issues.append(
                    f"轨迹断档迹象: step.begin={counts['step.begin']} 与 "
                    f"step.end={counts['step.end']} 不配对（可能中途被杀/超时）")

        except Exception as e:
            issues.append(f"wire.jsonl 读取失败: {e}")

    # 4. 验证截图（screenshots/ 子目录）
    screenshot = task_dir / "screenshots" / f"{task_id}_profile.png"
    if screenshot.exists():
        size_kb = screenshot.stat().st_size / 1024
        if size_kb < 50:
            warnings.append(f"截图文件过小: {size_kb:.1f} KB（可能截图失败）")
        elif size_kb > 5000:
            warnings.append(f"截图文件过大: {size_kb:.1f} KB")
        # captcha 状态下的截图大概率是验证页而非作者主页，标人工复核
        if result_data and result_data.get("status") == "captcha":
            warnings.append("status=captcha 但存在截图（可能是验证页截图），需人工复核")

    # 5. 验证 trace.zip（完整性 + 关键条目）
    trace_zip = task_dir / "trace.zip"
    if trace_zip.exists():
        import zipfile
        try:
            with zipfile.ZipFile(trace_zip) as zf:
                if zf.testzip() is not None:
                    issues.append("trace.zip 损坏")
                elif "trace.trace" not in zf.namelist():
                    issues.append("trace.zip 缺少 trace.trace 主文件")
        except zipfile.BadZipFile:
            issues.append("trace.zip 不是有效的 zip 文件")

    return {
        "task_id": task_id,
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "files": required_files,
        "status": result_data.get("status") if result_data else "unknown"
    }


def print_report(results: list):
    """打印验证报告"""
    valid_count = sum(1 for r in results if r["valid"])
    total_count = len(results)

    print(f"\n{'='*60}")
    print(f"数据完整性检查报告")
    print(f"{'='*60}")
    print(f"总任务数: {total_count}")
    print(f"通过: {valid_count}")
    print(f"有问题: {total_count - valid_count}")
    print(f"{'='*60}\n")

    for result in results:
        task_id = result["task_id"]
        status = result["status"]
        symbol = "✓" if result["valid"] else "✗"

        print(f"{symbol} {task_id} (状态: {status})")

        # 显示文件检查结果
        missing_files = [f for f, exists in result["files"].items() if not exists]
        if missing_files:
            print(f"  缺失文件: {', '.join(missing_files)}")

        # 显示问题
        if result["issues"]:
            for issue in result["issues"]:
                print(f"  ❌ {issue}")

        # 显示警告
        if result["warnings"]:
            for warning in result["warnings"]:
                print(f"  ⚠️  {warning}")

        print()


def main():
    parser = argparse.ArgumentParser(description="验证任务数据完整性")
    parser.add_argument("--task-id", help="只检查特定任务")
    args = parser.parse_args()

    # 收集任务目录
    task_dirs = []
    if args.task_id:
        task_dir = DATA_DIR / args.task_id
        if task_dir.exists():
            task_dirs = [task_dir]
        else:
            print(f"[ERROR] 任务目录不存在: {task_dir}")
            return
    else:
        task_dirs = [d for d in DATA_DIR.iterdir() if d.is_dir() and d.name.startswith("task_")]

    if not task_dirs:
        print("[INFO] 没有找到任何任务数据")
        return

    # 验证每个任务
    results = []
    for task_dir in sorted(task_dirs):
        result = validate_task(task_dir)
        results.append(result)

    # 打印报告
    print_report(results)

    # 返回状态码
    if all(r["valid"] for r in results):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()

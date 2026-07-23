"""批量校验数据集:python qa/cli.py <样本.jsonl>"""
import json, sys
from validator import validate_sample
def main(path):
    total, passed, rejects = 0, 0, []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            sample = json.loads(line)
            total += 1
            issues = validate_sample(sample)
            if issues:
                rejects.append({
                    "line": lineno,
                    "task_id": sample.get("meta", {}).get("task_id"),
                    "issues": [vars(x) for x in issues],
                })
            else:
                passed += 1
    with open("reject_report.json", "w", encoding="utf-8") as f:
        json.dump(rejects, f, ensure_ascii=False, indent=2)
    print(f"共 {total} 条:通过 {passed},拒收 {len(rejects)}")
if __name__ == "__main__":
    main(sys.argv[1])
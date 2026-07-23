"""校验单个样本:python qa/check_one.py data/task_0001/sample.json"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qa.validator import validate_sample
sample = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
issues = validate_sample(sample)
if issues:
    for x in issues:
        print(f"[{x.level}] {x.rule} @ {x.location}: {x.message}")
else:
    print("PASS: 全部校验通过")
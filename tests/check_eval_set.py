"""
评测集格式检查：确保 eval_set.json 结构完整。

为什么需要？
    评测集是人工维护的 JSON，字段写错会让评测"悄悄失效"——
    脚本照常运行，但因为缺字段而永远判定通过，得出虚假的高分。
    CI 里跑这个检查，能在提交前拦住这类问题。
"""
import sys
import json
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parents[1]
EVAL_SET = ROOT / "tests" / "eval_set.json"

REQUIRED_CASE_KEYS = {"id", "topic", "expected"}
REQUIRED_EXPECTED_KEYS = {"doc_no_contains", "subsidy_values"}


def main() -> int:
    if not EVAL_SET.is_file():
        print(f"❌ 评测集不存在: {EVAL_SET}")
        return 1

    data = json.loads(EVAL_SET.read_text(encoding="utf-8"))

    cases = data.get("cases")
    if not cases:
        print("❌ 评测集没有 cases")
        return 1

    errors = []
    seen_ids = set()
    for i, c in enumerate(cases, 1):
        missing = REQUIRED_CASE_KEYS - set(c)
        if missing:
            errors.append(f"用例 {i} 缺字段: {missing}")
            continue
        if c["id"] in seen_ids:
            errors.append(f"用例 id 重复: {c['id']}")
        seen_ids.add(c["id"])
        if not isinstance(c["expected"], dict):
            errors.append(f"用例 {c['id']} 的 expected 不是对象")
            continue
        miss_exp = REQUIRED_EXPECTED_KEYS - set(c["expected"])
        if miss_exp:
            errors.append(f"用例 {c['id']} 的 expected 缺字段: {miss_exp}")

    if errors:
        for e in errors:
            print(f"❌ {e}")
        return 1

    print(f"✅ 评测集格式正确：{len(cases)} 个用例")
    return 0


if __name__ == "__main__":
    sys.exit(main())

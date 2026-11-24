"""
政策调研评测脚本：用标准答案量化质量。

为什么必须有评测？
    没有评测，就无法回答"这个组件到底靠不靠谱"。
    面试官/客户问"准确率多少"，答不出来 = 只是 demo。
    这是项目从演示走向可信的关键一步。

评测三个维度：
    1. 内容准确率 —— 期望的关键事实（补贴金额、文号、主体、范围）命中了多少
    2. 来源质量   —— S/A 级权威来源占比；D 级低质来源数量（应为 0）
    3. 结构化完整度 —— 政策卡片 6 个字段填了几个

用法：
    python tests/eval_policy.py            # 跑全部用例
    python tests/eval_policy.py --deep     # 用深度档评测
    python tests/eval_policy.py --out report.md   # 输出 Markdown 报告
"""
import sys
import io
import os
import json
import time
import argparse
from pathlib import Path

# Windows 控制台 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import get_settings
from core.llm import LLM
from core.tools import SearchTool
from core.authority import classify
from core.policy import build_card
from agents.researcher import ResearchEngine

EVAL_SET = ROOT / "tests" / "eval_set.json"


def evaluate_case(case: dict, llm, search, deep: bool) -> dict:
    """跑单个用例并打分。"""
    topic = case["topic"]
    dom = case.get("domain", "policy")
    exp = case.get("expected", {})

    t0 = time.time()
    engine = ResearchEngine(llm, search)
    pack = engine.research(topic, {}, domain=dom, deep=deep)
    elapsed = time.time() - t0

    # 把全部发现拼成一段文本，用于关键词命中检查
    corpus = "\n".join(f.claim for f in pack.findings)
    card = build_card(pack, llm=llm)
    card_text = json.dumps(card.to_dict(), ensure_ascii=False)
    combined = corpus + "\n" + card_text

    # ---- 1. 内容准确率 ----
    checks = []

    doc_ok = True
    if exp.get("doc_no_contains"):
        doc_ok = any(k in combined for k in exp["doc_no_contains"])
        checks.append(("文号识别", doc_ok))

    issuer_ok = True
    if exp.get("issuer_contains"):
        issuer_ok = any(k in combined for k in exp["issuer_contains"])
        checks.append(("发布主体", issuer_ok))

    subsidy_hits = [v for v in exp.get("subsidy_values", []) if v in combined]
    need = exp.get("subsidy_must_hit", 0)
    subsidy_ok = len(subsidy_hits) >= need
    if exp.get("subsidy_values"):
        checks.append((f"补贴数值(命中{len(subsidy_hits)}/{need})", subsidy_ok))

    scope_ok = True
    if exp.get("scope_keywords"):
        scope_ok = any(k in combined for k in exp["scope_keywords"])
        checks.append(("适用范围", scope_ok))

    # 反向检查：不应出现的错误内容
    wrong_hits = [k for k in exp.get("must_not_contain", []) if k in corpus]
    no_wrong = not wrong_hits
    if exp.get("must_not_contain"):
        checks.append(("无地域串味", no_wrong))

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks) or 1
    accuracy = passed / total

    # ---- 2. 来源质量 ----
    levels = [f.level for f in pack.findings if f.level]
    s_count = sum(1 for lv in levels if lv == "S")
    a_count = sum(1 for lv in levels if lv == "A")
    d_count = sum(1 for lv in levels if lv == "D")
    auth_ratio = (s_count + a_count) / len(levels) if levels else 0.0

    # ---- 3. 结构化完整度 ----
    card_filled = card.filled        # /6

    return {
        "id": case["id"],
        "topic": topic,
        "used_for": case.get("used_for", ""),
        "elapsed": round(elapsed, 1),
        "findings": len(pack.findings),
        "checks": checks,
        "accuracy": accuracy,
        "levels": {"S": s_count, "A": a_count, "D": d_count, "total": len(levels)},
        "auth_ratio": auth_ratio,
        "card_filled": card_filled,
        "card": card.to_dict(),
        "findings_detail": [{"claim": f.claim, "source": f.source, "level": f.level}
                            for f in pack.findings],
    }


def render_report(results: list, deep: bool) -> str:
    lines = []
    mode = "深度档（读全文+追问）" if deep else "快速档（读摘要）"
    lines.append(f"# 政策调研评测报告\n")
    lines.append(f"- 评测模式：{mode}")
    lines.append(f"- 用例数：{len(results)}")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 总览
    avg_acc = sum(r["accuracy"] for r in results) / len(results)
    avg_auth = sum(r["auth_ratio"] for r in results) / len(results)
    avg_card = sum(r["card_filled"] for r in results) / len(results)
    total_d = sum(r["levels"]["D"] for r in results)

    lines.append("## 总览\n")
    lines.append("| 指标 | 数值 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| 内容准确率 | **{avg_acc:.0%}** | 关键事实（文号/主体/金额/范围）命中率 |")
    lines.append(f"| 权威来源占比 | **{avg_auth:.0%}** | S/A 级（政府/权威机构）来源比例 |")
    lines.append(f"| 卡片字段完整度 | **{avg_card:.1f}/6** | 政策结构化抽取填了几个字段 |")
    lines.append(f"| 低质来源条数 | **{total_d}** | 应为 0 |")
    lines.append("")

    # 逐用例
    lines.append("## 逐用例结果\n")
    for r in results:
        lines.append(f"### {r['topic']}")
        lines.append(f"- 用途：{r['used_for']}")
        lines.append(f"- 耗时：{r['elapsed']}s ｜ 发现数：{r['findings']}")
        lines.append(f"- 内容准确率：**{r['accuracy']:.0%}**")
        lv = r["levels"]
        lines.append(f"- 来源分布：S {lv['S']} / A {lv['A']} / D {lv['D']}（共 {lv['total']}）")
        lines.append(f"- 卡片字段：{r['card_filled']}/6")
        lines.append("- 检查项：")
        for name, ok in r["checks"]:
            lines.append(f"  - {'✅' if ok else '❌'} {name}")
        lines.append("")

    # 卡片详情
    lines.append("## 政策卡片详情\n")
    for r in results:
        c = r["card"]
        lines.append(f"### {r['topic']}")
        for k, label in [("title", "名称"), ("doc_no", "文号"),
                         ("issuer", "发布主体"), ("effective", "有效期"),
                         ("scope", "适用对象")]:
            v = c.get(k) or "—"
            lines.append(f"- **{label}**：{v}")
        if c.get("subsidy"):
            lines.append(f"- **补贴标准**：{'；'.join(c['subsidy'])}")
        lines.append("")

    # 局限性（诚实说明）
    lines.append("## 已知局限\n")
    lines.append("- 评测集只有 3 个主题，样本量小，只能反映趋势不能作为严格统计")
    lines.append("- 标准答案基于政府公开文件人工核对，但政策有地区/时间差异")
    lines.append("- 关键词命中是简化判定，不理解语义等价表达")
    lines.append("- 未评估：结论的完整性（漏掉重要信息不扣分）")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="使用深度档评测")
    ap.add_argument("--out", default="", help="输出 Markdown 报告路径")
    ap.add_argument("--only", default="", help="只跑指定 id")
    args = ap.parse_args()

    s = get_settings()
    if not s.openai_api_key:
        print("❌ 缺少 OPENAI_API_KEY，无法评测")
        sys.exit(1)
    if not s.tavily_api_key:
        print("⚠️  未配置 TAVILY_API_KEY，搜索会降级到离线资料，评测结果无意义")
        sys.exit(1)

    llm = LLM(s.llm_model, s.openai_api_key, s.openai_base_url)
    search = SearchTool(api_key=s.tavily_api_key, fulltext=args.deep)

    cases = json.loads(EVAL_SET.read_text(encoding="utf-8"))["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]

    print(f"开始评测：{len(cases)} 个用例，模式={'深度' if args.deep else '快速'}\n")
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['topic']}")
        try:
            r = evaluate_case(case, llm, search, args.deep)
            results.append(r)
            print(f"    准确率 {r['accuracy']:.0%} | "
                  f"权威来源 {r['auth_ratio']:.0%} | "
                  f"卡片 {r['card_filled']}/6 | {r['elapsed']}s")
        except Exception as e:
            print(f"    ❌ 失败: {type(e).__name__}: {e}")

    if not results:
        print("无有效结果")
        sys.exit(1)

    report = render_report(results, args.deep)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"\n报告已写入: {args.out}")

    # 控制台摘要
    avg_acc = sum(r["accuracy"] for r in results) / len(results)
    avg_auth = sum(r["auth_ratio"] for r in results) / len(results)
    total_d = sum(r["levels"]["D"] for r in results)
    print("\n" + "=" * 50)
    print(f"内容准确率: {avg_acc:.0%}")
    print(f"权威来源占比: {avg_auth:.0%}")
    print(f"低质来源: {total_d} 条")
    print("=" * 50)


if __name__ == "__main__":
    main()

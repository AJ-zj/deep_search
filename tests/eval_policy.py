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
    # 与生产链路一致：图谱存在就注入（否则 GraphRAG 改进测不出来）
    graph_context = ""
    try:
        from graph.graph_query import context_for_question, GRAPH_FILE
        if GRAPH_FILE.is_file():
            graph_context = context_for_question(topic, restrict_topic=topic)
    except Exception:
        pass
    card = build_card(pack, llm=llm, graph_context=graph_context)
    card_text = json.dumps(card.to_dict(), ensure_ascii=False)
    combined = corpus + "\n" + card_text

    # ---- 1. 内容准确率 ----
    checks = []

    # 反向用例：主题本身是虚构的，正确行为是"识别出不存在的政策、不编造细节"
    is_negative = case.get("is_negative", False)
    if is_negative:
        # 判定：不应有具体文号/补贴金额；发现数量应很少或明确表示不存在
        fabrications = []
        if card.doc_no:
            fabrications.append(f"编造文号:{card.doc_no}")
        if card.subsidy:
            fabrications.append(f"编造补贴:{card.subsidy[:2]}")
        # 编造判定：只有"具体的文号/金额"才算编造；
        # 政策名称照抄用户主题是诚实的，不能算编造（"实施细则"是主题自带词）。
        honest = not fabrications
        checks.append(("虚构主题不编造", honest))
        # 反向用例不参与正向命中检查，直接算出准确率
        passed = sum(1 for _, ok in checks if ok)
        accuracy = passed / len(checks)
        return _assemble(case, elapsed, pack, checks, accuracy, card, corpus, graph_context)

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
    return _assemble(case, elapsed, pack, checks, accuracy, card, corpus, graph_context)


def _assemble(case, elapsed, pack, checks, accuracy, card, corpus, graph_context=""):
    """汇总评分结果（正向/反向用例共用）。"""
    levels = [f.level for f in pack.findings if f.level]
    s_count = sum(1 for lv in levels if lv == "S")
    a_count = sum(1 for lv in levels if lv == "A")
    d_count = sum(1 for lv in levels if lv == "D")
    auth_ratio = (s_count + a_count) / len(levels) if levels else 0.0

    return {
        "id": case["id"],
        "topic": case["topic"],
        "used_for": case.get("used_for", ""),
        "is_negative": case.get("is_negative", False),
        "used_graph": bool(graph_context),
        "elapsed": round(elapsed, 1),
        "findings": len(pack.findings),
        "checks": checks,
        "accuracy": accuracy,
        "levels": {"S": s_count, "A": a_count, "D": d_count, "total": len(levels)},
        "auth_ratio": auth_ratio,
        "card_filled": card.filled,
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

    # 反向用例（虚构主题）预期就没有发现，不能算"未取到数据"
    positive = [r for r in results if not r.get("is_negative")]
    empty = sum(1 for r in positive if r["findings"] == 0)

    lines.append("## 总览\n")
    lines.append("| 指标 | 数值 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| 内容准确率 | **{avg_acc:.0%}** | 关键事实（文号/主体/金额/范围）命中率 |")
    lines.append(f"| 权威来源占比 | **{avg_auth:.0%}** | S/A 级（政府/权威机构）来源比例 |")
    lines.append(f"| 卡片字段完整度 | **{avg_card:.1f}/6** | 政策结构化抽取填了几个字段 |")
    lines.append(f"| 低质来源条数 | **{total_d}** | 应为 0 |")
    lines.append(f"| 未取到数据 | **{empty}/{len(results)}** | 搜索/提炼失败，非答错 |")
    lines.append("")

    # 逐用例
    lines.append("## 逐用例结果\n")
    for r in results:
        lines.append(f"### {r['topic']}")
        lines.append(f"- 用途：{r['used_for']}")
        lines.append(f"- 耗时：{r['elapsed']}s ｜ 发现数：{r['findings']}")
        acc_line = f"- 内容准确率：**{r['accuracy']:.0%}**"
        if r.get("runs", 1) > 1:
            acc_line += (f"（{r['runs']} 次均值，区间 "
                         f"{r['accuracy_min']:.0%}~{r['accuracy_max']:.0%}）")
        lines.append(acc_line)
        if r.get("is_negative"):
            ok = all(v for _, v in r["checks"])
            lines.append("- ✅ 正确拒绝：虚构主题未编造文号/金额" if ok
                         else "- ❌ 失败：对虚构主题编造了具体细节")
        elif r["findings"] == 0:
            lines.append("- ⚠️ 本轮未取到任何发现（搜索/提炼失败，不计入能力评价）")
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
    lines.append("- 单次运行有波动（搜索结果每次不同）。建议用 `--repeat 3` 取均值再下结论")
    lines.append("- 标准答案基于政府公开文件人工核对，但政策有地区/时间差异")
    lines.append("- 关键词命中是简化判定，不理解语义等价表达")
    lines.append("- 未评估：结论的完整性（漏掉重要信息不扣分）")
    lines.append("- 「未取到数据」的用例通常是搜索/LLM 瞬时失败，不代表能力差，但也不该算通过")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="使用深度档评测")
    ap.add_argument("--out", default="", help="输出 Markdown 报告路径")
    ap.add_argument("--only", default="", help="只跑指定 id")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每个用例重复跑几次（用于观察稳定性，默认 1）")
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

    repeat = max(1, args.repeat)
    print(f"开始评测：{len(cases)} 个用例 × {repeat} 次，"
          f"模式={'深度' if args.deep else '快速'}\n")

    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['topic']}")
        runs = []
        for k in range(repeat):
            try:
                r = evaluate_case(case, llm, search, args.deep)
                runs.append(r)
                tag = f" (第{k+1}次)" if repeat > 1 else ""
                print(f"    准确率 {r['accuracy']:.0%} | "
                      f"权威来源 {r['auth_ratio']:.0%} | "
                      f"卡片 {r['card_filled']}/6 | 发现 {r['findings']} | "
                      f"{r['elapsed']}s{tag}")
            except Exception as e:
                print(f"    ❌ 失败: {type(e).__name__}: {e}")

        if not runs:
            continue
        # 多次运行时，聚合为均值 + 波动区间
        merged = runs[0]
        if len(runs) > 1:
            accs = [x["accuracy"] for x in runs]
            merged = dict(merged)
            merged["accuracy"] = sum(accs) / len(accs)
            merged["accuracy_min"] = min(accs)
            merged["accuracy_max"] = max(accs)
            merged["empty_runs"] = sum(1 for x in runs if x["findings"] == 0)
            merged["runs"] = len(runs)
            print(f"    → 均值 {merged['accuracy']:.0%} "
                  f"(区间 {min(accs):.0%}~{max(accs):.0%})")
        results.append(merged)

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
    positive = [r for r in results if not r.get("is_negative")]
    empty = sum(1 for r in positive if r["findings"] == 0)
    neg = [r for r in results if r.get("is_negative")]
    print("\n" + "=" * 50)
    print(f"内容准确率: {avg_acc:.0%}")
    print(f"权威来源占比: {avg_auth:.0%}")
    print(f"低质来源: {total_d} 条")
    print(f"未取到数据的用例: {empty}/{len(positive)}")
    if neg:
        passed = sum(1 for r in neg if all(v for _, v in r["checks"]))
        print(f"反向用例（虚构主题防编造）: {passed}/{len(neg)} 通过")
    print("=" * 50)


if __name__ == "__main__":
    main()

"""
离线单元测试：不联网、不需要任何 API Key。

为什么单独一组？
    CI 环境没有密钥，也不能依赖外部 API（不稳定、有成本）。
    所以把"纯逻辑"部分抽出来单独测：JSON 清洗、全文清洗、
    领域配置、证据契约、来源编号解析。这些是项目最容易出错的地方，
    也是最该被自动化守护的地方。

带网络的分步测试（test_core/test_research/test_planner）请本地运行。
"""
import sys
from pathlib import Path

# Windows 控制台/CI 默认是 cp1252 或 gbk，直接 print 中文会 UnicodeEncodeError。
# 这里强制把标准输出改成 UTF-8，保证本地和 CI 行为一致。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tools import clean_fulltext
from core.domain import get_domain, list_domains
from core.evidence import Finding, KnowledgePack, DayPlan, TravelPlan, Place
from agents.planner import PlanEngine
from agents.researcher import ResearchEngine

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)


def test_clean_fulltext():
    print("[clean_fulltext]")
    # 开头导航噪音应被跳过
    nav = "English (Global)\nEnglish (North America)\nEspañol (Latam)\n" \
          "English (Europe)\nEspañol (Europe)\nTiếng\n\n" + "正文内容。" * 100
    out = clean_fulltext(nav)
    check("跳过导航噪音", "English (Global)" not in out[:200], out[:80])
    check("保留正文", "正文内容" in out)

    # 超长应被截断
    long_text = "内容。" * 5000
    out2 = clean_fulltext(long_text, max_chars=1000)
    check("超长被截断", len(out2) <= 1200, f"len={len(out2)}")

    # 空输入
    check("空输入返回空", clean_fulltext("") == "")


def test_domain():
    print("[domain]")
    check("travel 能取到", get_domain("travel").key == "travel")
    check("policy 能取到", get_domain("policy").key == "policy")
    check("未知领域回退 general", get_domain("xxx").key == "general")
    check("空字符串回退 general", get_domain("").key == "general")
    keys = {d["key"] for d in list_domains()}
    check("四个领域齐全", keys == {"travel", "policy", "ecommerce", "general"}, str(keys))
    check("policy 不按地名限定", get_domain("policy").scope_by_place is False)
    check("travel 按地名限定", get_domain("travel").scope_by_place is True)


def test_evidence():
    print("[evidence]")
    pack = KnowledgePack(topic="测试")
    pack.add("有效结论", "https://a.com")
    pack.add("   ", "https://b.com")      # 空结论应被忽略
    check("空结论被忽略", len(pack.findings) == 1, str(len(pack.findings)))

    prompt = pack.to_prompt()
    check("prompt 带编号", "[1]" in prompt)
    check("prompt 带来源", "https://a.com" in prompt)
    check("prompt 带偏好段", "调研发现" in prompt)

    # 行程渲染
    plan = TravelPlan(topic="杭州", summary="概述")
    plan.days.append(DayPlan(day=1, references=[1], places=[
        Place(name="西湖", time="上午", desc="风景", ref=1,
              lng="120.1", lat="30.2", location="120.1,30.2", address="西湖区"),
    ]))
    md = plan.to_markdown(pack)
    check("渲染坐标", "120.1" in md)
    check("渲染地址", "西湖区" in md)
    check("渲染引用", "依据调研发现" in md)
    check("渲染参考来源", "参考来源" in md)
    check("all_places 统计", len(plan.all_places()) == 1)


def test_json_parse():
    print("[planner._parse 脏 JSON]")
    cases = [
        ('```json\n[{"day":1,"places":[]}]\n```', 1),
        ('好的：\n[{"day":1,"places":[]}]\n希望有帮助', 1),
        ('[{"day":1,"places":[],}]', 1),           # 尾随逗号
        ('[{{"day":1,"places":[]}}]', 1),          # 双大括号
        ('完全不是JSON', 0),                        # 兜底
        ('{}', 0),                                  # 非数组
    ]
    for raw, expected in cases:
        got = PlanEngine._parse(raw)
        check(f"解析 {raw[:28]!r}", len(got) == expected, f"得到 {len(got)}")

    # 引用编号越界保护
    check("越界引用归零", PlanEngine._safe_ref(99, 3) == 0)
    check("正常引用保留", PlanEngine._safe_ref(2, 3) == 2)
    check("非数字引用归零", PlanEngine._safe_ref("abc", 3) == 0)


def test_useless_filter():
    print("[researcher 无信息量过滤]")
    from agents.researcher import _USELESS_PATTERNS
    bad = [
        "当前网络搜索不可用，无法获取相关信息",
        "网络环境不支持查询该政策",
        "未能找到相关资料，建议联网重试",
        "（离线资料）当前网络搜索不可用",
    ]
    good = [
        "浙江省对分布式光伏省再补贴0.1元/千瓦时",
        "2019年起户用光伏补贴标准调整为0.18元",
    ]
    for s in bad:
        check(f"应丢弃 {s[:16]!r}", bool(_USELESS_PATTERNS.search(s)))
    for s in good:
        check(f"应保留 {s[:16]!r}", not _USELESS_PATTERNS.search(s))


def test_graph_query():
    print("[graph_query 图遍历]")
    from collections import defaultdict
    from graph.graph_query import split_terms, find_entities, neighborhood, render_context

    # 手工小图：模拟政策演化链
    triples = [
        {"h": "国能发〔2013〕390号", "r": "规定", "t": "补贴0.42元/千瓦时", "docs": ["001"]},
        {"h": "国能发〔2013〕390号", "r": "发布主体", "t": "国家能源局", "docs": ["001"]},
        {"h": "浙政发〔2013〕49号", "r": "依据", "t": "国能发〔2013〕390号", "docs": ["002"]},
        {"h": "浙政发〔2013〕49号", "r": "规定", "t": "省补0.1元/千瓦时", "docs": ["002"]},
        {"h": "发改能源〔2019〕761号", "r": "修订", "t": "国能发〔2013〕390号", "docs": ["003"]},
    ]
    adj = defaultdict(list)
    entities = set()
    for tr in triples:
        h, r, t = tr["h"], tr["r"], tr["t"]
        entities.add(h); entities.add(t)
        adj[h].append((r, t, tr))
        adj[t].append(("←" + r, h, tr))
    g = {"triples": triples, "adj": adj, "entities": entities}

    terms = split_terms("浙政发〔2013〕49号规定了什么")
    check("文号被切出", any("2013" in t or "〔" in t or len(t) >= 4 for t in terms))

    seeds = find_entities("浙政发〔2013〕49号规定了什么", g)
    check("种子实体正确", seeds and "浙政发〔2013〕49号" in seeds, str(seeds))

    trs = neighborhood(seeds, g, hops=2)
    rels = {(t["h"], t["r"], t["t"]) for t in trs}
    check("1跳命中直接规定", ("浙政发〔2013〕49号", "规定", "省补0.1元/千瓦时") in rels)
    check("2跳沿依据链走通", ("国能发〔2013〕390号", "规定", "补贴0.42元/千瓦时") in rels,
          str(rels))
    check("2跳发现修订关系", ("发改能源〔2019〕761号", "修订", "国能发〔2013〕390号") in rels)

    ctx = render_context(trs)
    check("上下文含图谱标注", "政策知识图谱" in ctx)
    check("上下文含关系箭头", "->" in ctx or "-[" in ctx)

    seeds2 = find_entities("火星基地的补贴是多少", g)
    check("无关问题不误命中", all("火星" not in s for s in seeds2), str(seeds2))

    # 跨主题污染防护（实测浙江主题曾被图注入国家761号文）
    from graph.graph_query import _filter_by_topic
    tmap = {"001": "浙江省分布式光伏发电补贴政策",
            "002": "国家分布式光伏发电补贴政策文件"}
    trs = [
        {"h": "浙政发〔2013〕49号", "r": "规定", "t": "省补0.1元/千瓦时", "docs": ["001"]},
        {"h": "发改价格〔2019〕761号", "r": "规定", "t": "补贴0.10元", "docs": ["002"]},
    ]
    kept = _filter_by_topic(trs, "浙江省分布式光伏发电补贴政策", tmap=tmap)
    check("跨主题三元组被过滤", len(kept) == 1 and kept[0]["h"].startswith("浙政发"),
          str(kept))
    kept2 = _filter_by_topic(trs, "国家分布式光伏发电补贴政策文件", tmap=tmap)
    check("本主题三元组保留", len(kept2) == 1 and "761" in kept2[0]["h"])


def test_topic_keywords():
    print("[researcher 关键词提取]")
    eng = ResearchEngine(llm=None, search=None)
    travel = get_domain("travel")
    kws = eng._topic_keywords("杭州 3 天人文美食游", travel)
    check("提取到杭州", "杭州" in kws, str(kws))
    check("过滤停用词'美食'", "美食" not in kws, str(kws))

    policy = get_domain("policy")
    kws2 = eng._topic_keywords("浙江省分布式光伏补贴政策", policy)
    check("政策领域保留主体", "浙江省" in kws2, str(kws2))


def main():
    test_clean_fulltext()
    test_domain()
    test_evidence()
    test_json_parse()
    test_useless_filter()
    test_topic_keywords()
    test_graph_query()

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("✅ 离线单元测试全部通过")


if __name__ == "__main__":
    main()

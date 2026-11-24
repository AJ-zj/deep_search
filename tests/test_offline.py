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

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("✅ 离线单元测试全部通过")


if __name__ == "__main__":
    main()

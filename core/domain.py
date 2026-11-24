"""
领域配置：让同一个调研引擎适配不同业务。

这就是"组件化"的关键——引擎逻辑只有一份，变的只是"怎么拆问题、怎么过滤"。
你以后做电力政策调研、电商文案调研，只要加一个 Domain 配置，不用改引擎。

用法：
    research(topic, domain="policy")       # 政策调研
    research(topic, domain="ecommerce")    # 电商/文案调研
    research(topic)                        # 默认 general
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class Domain:
    """一个调研领域的配置。"""
    key: str
    name: str
    # 拆解子问题时给 LLM 的维度提示（这是最关键的部分）
    dimensions_hint: str
    # 提炼结论时的额外要求（可以为空）
    extract_hint: str = ""
    # 过滤跑题结论时排除的泛词
    stop_words: List[str] = field(default_factory=list)
    # 是否需要把主题里的地名拼到搜索词前面（旅行需要，政策/电商不需要）
    scope_by_place: bool = False


# ---- 旅行调研（当前 TripResearch 用的）----
TRAVEL = Domain(
    key="travel",
    name="旅行规划",
    dimensions_hint="分别聚焦：①必去景点与口碑 ②特色美食与餐厅 ③行程节奏与避坑提醒",
    extract_hint="结论中要点明地点（如杭州）",
    stop_words=["旅游", "游", "天", "人文", "美食", "推荐", "攻略", "trip"],
    scope_by_place=True,
)

# ---- 政策调研（你实习在做：电力/地方政策）----
POLICY = Domain(
    key="policy",
    name="政策法规",
    dimensions_hint=(
        "分别聚焦：①政策文件与发布主体（文号、时间）"
        "②适用范围与申报条件 ③补贴/电价等关键数字 ④有效期与执行口径"
    ),
    extract_hint="结论要写明政策文号或出台主体，涉及数字原样保留",
    stop_words=["政策", "规定", "文件", "通知", "要求", "办法"],
    scope_by_place=False,
)

# ---- 电商内容调研（你实习在做：文案素材）----
ECOMMERCE = Domain(
    key="ecommerce",
    name="电商内容",
    dimensions_hint=(
        "分别聚焦：①目标人群与痛点 ②竞品卖点与话术 ③热销品类的真实用户评价"
        "④平台规则与合规红线"
    ),
    extract_hint="结论尽量带具体卖点、价格区间或用户原声",
    stop_words=["文案", "电商", "产品", "商品", "推荐", "营销"],
    scope_by_place=False,
)

# ---- 通用调研（兜底）----
GENERAL = Domain(
    key="general",
    name="通用调研",
    dimensions_hint="分别聚焦：①基本概念与背景 ②现状与关键事实 ③实际案例或数据 ④趋势与风险",
    extract_hint="",
    stop_words=["研究", "调研", "分析", "介绍", "了解"],
    scope_by_place=False,
)

_REGISTRY = {d.key: d for d in (TRAVEL, POLICY, ECOMMERCE, GENERAL)}


def get_domain(key: str = "") -> Domain:
    """按 key 取领域配置，未知则返回通用配置。"""
    return _REGISTRY.get((key or "").lower(), GENERAL)


def list_domains() -> List[dict]:
    """列出所有可用领域（供前端下拉选择）。"""
    return [{"key": d.key, "name": d.name} for d in _REGISTRY.values()]

"""
来源权威性分级：判断一条搜索结果的可信度。

为什么这是政策调研的核心？
    搜"浙江光伏补贴"，返回的可能混着：
      - 政府红头文件（.gov.cn）      ← 权威，可直接引用
      - 官方媒体/学术机构             ← 可信
      - 律所/行业媒体解读              ← 有参考价值，但可能过时
      - 内容农场/营销号/厂商推广页     ← 不可作为依据
    现在所有来源等权处理，等于把营销号和政策文件混为一谈。
    商用场景里引用错来源是灾难，所以必须先分级再排序。

分级依据（只用域名，不依赖外部服务，零成本）：
    S 级 —— 政府/立法机关：.gov.cn、人大、发改委、能源局等
    A 级 —— 权威机构：官方媒体、学术期刊、国际组织
    B 级 —— 专业媒体/行业站：律所、行业协会、专业资讯
    C 级 —— 一般网站
    D 级 —— 低质：内容农场、聚合站、营销号、问答站
"""
import re
from typing import Tuple
from urllib.parse import urlparse

# ---- 分级规则（按顺序匹配，先命中先返回）----
# 注意：.com.cn / .org.cn 这类二级后缀必须写进规则，
# 否则 "news.bjx.com.cn" 这类域名匹配不上（实测漏判过）。
_SUFFIX = r"\.(?:com|cn|net|org|com\.cn|org\.cn|net\.cn|gov\.cn|edu\.cn|ac\.cn)(?:$|/)"

_RULES = [
    # S 级：政府与立法
    ("S", re.compile(r"(^|\.)gov\.cn(?:$|/)|(^|\.)gov(?:$|/)|\.gov\.cn(?:$|/)", re.I)),

    # A 级：权威媒体 / 学术 / 国际组织
    ("A", re.compile(
        r"(^|\.)(xinhuanet|people|chinadaily|cctv|cri|cnr)\.(?:com|cn)(?:$|/)"
        r"|(^|\.)(cas|cssn|cae)\.cn(?:$|/)"
        r"|(^|\.)(nature|science|sciencedirect|springer|arxiv|cnki)\.(?:com|org|net)(?:$|/)"
        r"|(^|\.)(un|worldbank|imf|oecd|iea)\.org(?:$|/)"
        r"|(^|\.)(edu\.cn|ac\.cn|edu)(?:$|/)", re.I)),

    # B 级：专业/行业/律所
    ("B", re.compile(
        r"(^|\.)(lexology|pkulaw|lawinfochina|chinalaw|westlaw)\.(?:com|cn|com\.cn)(?:$|/)"
        r"|(^|\.)(bjx|solarbe|pvnews|chinasmartgrid|ne21|energytrend|solarzoom|ofweek)"
        r"\.(?:com|cn|com\.cn|net)(?:$|/)"
        r"|(^|\.)(caixin|yicai|21jingji|stcn|cnstock)\.(?:com|cn|com\.cn)(?:$|/)"
        r"|(^|\.)(wri|greentest)\.(?:org|org\.cn|com|cn)(?:$|/)", re.I)),

    # D 级：低质内容（排在 C 前面，避免被通用规则吃掉）
    ("D", re.compile(
        r"(^|\.)(zhidao|wenda|wenku|jingyan)\.baidu\.com(?:$|/)"
        r"|(^|\.)(sohu|163|sina|qq|ifeng|toutiao|baijiahao)\.(?:com|cn|com\.cn)(?:$|/)"
        r"|(^|\.)(zhihu|douban|weibo|xiaohongshu|bilibili)\.(?:com|cn|com\.cn)(?:$|/)"
        r"|(^|\.)(csdn|jianshu|cnblogs|51cto)\.(?:net|cn|com)(?:$|/)|(^|\.)juejin\.(?:cn|im)(?:$|/)"
        r"|(^|\.)(goodwe|growatt|trinasolar|jinko|longi)\.(?:com|cn|com\.cn)(?:$|/)", re.I)),
]

LEVEL_INFO = {
    "S": {"label": "政府/立法", "score": 100, "usable": True},
    "A": {"label": "权威机构", "score": 85, "usable": True},
    "B": {"label": "专业媒体", "score": 70, "usable": True},
    "C": {"label": "一般网站", "score": 50, "usable": True},
    "D": {"label": "低质来源", "score": 20, "usable": False},
}


def classify(url: str) -> str:
    """返回来源等级 S/A/B/C/D。"""
    if not url:
        return "D"
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return "D"
    if not host:
        # 可能是裸域名
        host = url.lower().split("/")[0]
    # 去掉端口和 www 前缀（保留原值再判断，规则里已兼容 www）
    host = host.split(":")[0]
    for level, pattern in _RULES:
        if pattern.search(host):
            return level
    return "C"


def score(url: str) -> int:
    """返回 0-100 的可信度分数。"""
    return LEVEL_INFO[classify(url)]["score"]


def label(url: str) -> str:
    return LEVEL_INFO[classify(url)]["label"]


def is_usable(url: str) -> bool:
    """是否可作为可信依据（D 级不用于支撑结论）。"""
    return LEVEL_INFO[classify(url)]["usable"]


def rank(results: list, keep_unusable: bool = False) -> list:
    """按权威性重排搜索结果，并在每条上标注 level。

    Args:
        results: [{title, url, snippet, fulltext}]
        keep_unusable: False 时剔除 D 级低质来源
    Returns:
        排序后的列表，每项多一个 level 字段（S/A/B/C/D）
    """
    scored = []
    for r in results:
        lv = classify(r.get("url", ""))
        if not keep_unusable and lv == "D":
            continue
        item = dict(r)
        item["level"] = lv
        scored.append((LEVEL_INFO[lv]["score"], item))
    # 分数高的在前；同分保持原顺序（稳定排序）
    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored]


def describe(levels: list) -> str:
    """生成一句来源质量描述，用于提示用户。"""
    from collections import Counter
    if not levels:
        return "无来源"
    cnt = Counter(levels)
    parts = [f"{LEVEL_INFO[k]['label']}{v}条" for k, v in sorted(cnt.items())]
    return "、".join(parts)

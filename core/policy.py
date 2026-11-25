"""
政策结构化抽取：把零散的调研发现整理成结构化政策卡片。

为什么需要？
    现在调研产出的是"一堆结论"，人还得自己从里面挑出文号、金额、期限。
    政策调研真正有用的形态是一张卡片：

        政策名称：……
        文号：浙政发〔2013〕49号
        发布主体：浙江省人民政府
        补贴标准：省再补贴 0.1 元/千瓦时
        适用对象：……
        有效期：……
        依据来源：https://……

设计原则（关键）：
    1. 只从**已读取的证据**里抽，不新增、不推断
    2. 每个字段都要能指回来源；抽不到的留空，绝不编造
    3. 用规则 + LLM 混合：文号、金额、日期这类强模式字段用正则兜底，
       语义字段（政策名称/适用对象）用 LLM 归纳
"""
import re
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional

from core.evidence import KnowledgePack

logger = logging.getLogger(__name__)


# ---- 强模式字段：正则抽取（比 LLM 更可靠、更省钱）----
# 中文公文文号标准形态：机关简称 + 〔年份〕 + 序号 + 号
#   例：浙政发〔2013〕49号 / 财建[2013]390号 / 发改价格〔2013〕1638号
#
# 为什么不用"机关简称白名单"？
#   实测机关简称变体极多（浙政发/财建/发改价格/国能发新能…），
#   白名单必然漏。改用「定位年份括号 → 往左回溯取机关简称」的策略，
#   回溯时遇到停用字（的/了/据/见/在/是…）即停，避免把整句吃进来。
_DOC_CORE = re.compile(
    r"(?P<l>(?:〔|\[|［|【))\s*(?P<year>\d{4})\s*(?:〕|\]|］|】)\s*(?P<num>\d{1,4})\s*号"
)
# 回溯终止字：这些字左边不再属于机关简称（虚词、动词、连词）
# 注意："发" 不能放进来——它是 "国发""浙政发" 的合法结尾；
#       只把 "布" 当终止（"发布" 的右字），这样 "…政府发布浙政发〔…〕" 能正确截到"浙政发"。
_STOP_CHARS = set(
    "的了布根据见在是按对将把为与及和或由从到按照关于通知如下特此据称表示"
)
_MAX_PREFIX = 8


def find_doc_no(text: str) -> str:
    """从文本中提取公文文号，如 "浙政发〔2013〕49号"。

    策略：定位「〔年份〕序号号」核心 → 往左回溯连续汉字作机关简称。
    遇到停用字立即停止；取 1-8 字。因为公文简称就是紧邻括号的那串汉字，
    不需要额外裁剪（裁剪反而会误伤 "发改价格" 这类结尾字不在白名单的情况）。
    """
    m = _DOC_CORE.search(text)
    if not m:
        return ""
    left = text[: m.start()]
    chars = []
    for ch in reversed(left):
        if "\u4e00" <= ch <= "\u9fa5" and ch not in _STOP_CHARS and len(chars) < _MAX_PREFIX:
            chars.append(ch)
        else:
            break
    prefix = "".join(reversed(chars))
    core = m.group(0)
    # 简称至少 2 字才算有效；否则只返回括号核心
    return (prefix + core) if len(prefix) >= 2 else core


# 金额：0.1元/千瓦时、0.42 元/度、5万元、每千瓦时 0.18 元
_MONEY = re.compile(
    r"(?:每)?\s*[\d.]+\s*(?:元|万元)\s*/\s*(?:千瓦时|度|瓦|千瓦|kWh|KWh)"
    r"|每千瓦时\s*[\d.]+\s*元"
    r"|[\d.]+\s*元\s*(?:/\s*瓦)?"
)
# 金额：0.1元/千瓦时、0.42 元/度、5万元、每千瓦时 0.18 元
_MONEY = re.compile(
    r"(?:每)?\s*[\d.]+\s*(?:元|万元)\s*/\s*(?:千瓦时|度|瓦|千瓦|kWh|KWh)"
    r"|每千瓦时\s*[\d.]+\s*元"
    r"|[\d.]+\s*元\s*(?:/\s*瓦)?"
)
# 日期：2018年12月31日 / 2019年
_DATE = re.compile(r"\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?")

# 发布主体线索
_ISSUER_HINT = re.compile(
    r"([\u4e00-\u9fa5]{2,15}(?:省|市|县|区)人民政府|[\u4e00-\u9fa5]{2,15}(?:省|市)能源局"
    r"|国家能源局|国家发展改革委|财政部|工业和信息化部|国务院)"
)


@dataclass
class PolicyCard:
    """结构化政策卡片。"""
    title: str = ""            # 政策名称
    doc_no: str = ""           # 文号
    issuer: str = ""           # 发布主体
    subsidy: List[str] = field(default_factory=list)     # 补贴标准（可多条）
    scope: str = ""            # 适用对象/范围
    effective: str = ""        # 生效/有效期
    key_points: List[str] = field(default_factory=list)  # 其他关键点
    sources: List[Dict[str, str]] = field(default_factory=list)  # [{url, level}]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def filled(self) -> int:
        """已填充的关键字段数量（用于评估抽取质量）。"""
        return sum([
            bool(self.title), bool(self.doc_no), bool(self.issuer),
            bool(self.subsidy), bool(self.scope), bool(self.effective),
        ])

    def to_markdown(self) -> str:
        """渲染成 Markdown 卡片。"""
        lines = [f"### {self.title or '（未识别政策名称）'}", ""]
        rows = [
            ("文号", self.doc_no),
            ("发布主体", self.issuer),
            ("有效期", self.effective),
            ("适用对象", self.scope),
        ]
        for k, v in rows:
            if v:
                lines.append(f"- **{k}**：{v}")
        if self.subsidy:
            lines.append(f"- **补贴标准**：")
            for s in self.subsidy:
                lines.append(f"  - {s}")
        if self.key_points:
            lines.append(f"- **其他要点**：")
            for p in self.key_points:
                lines.append(f"  - {p}")
        if self.sources:
            lines.append("")
            lines.append("**来源**：")
            for s in self.sources:
                lv = f"（{s['level']}级）" if s.get("level") else ""
                lines.append(f"- {s.get('url','')} {lv}")
        return "\n".join(lines)


def extract_regex(pack: KnowledgePack) -> PolicyCard:
    """用正则从发现里抽强模式字段（不调 LLM，零成本）。"""
    card = PolicyCard()

    for f in pack.findings:
        text = f.claim
        src = {"url": f.source, "level": f.level} if f.source else None

        # 文号
        if not card.doc_no:
            doc = find_doc_no(text)
            if doc:
                card.doc_no = doc

        # 金额类 → 补贴标准
        for m in _MONEY.finditer(text):
            val = m.group(0).strip()
            if val and val not in card.subsidy and len(val) >= 4:
                card.subsidy.append(val)

        # 发布主体
        if not card.issuer:
            m = _ISSUER_HINT.search(text)
            if m:
                card.issuer = m.group(1)

        # 生效期（取最后出现的年份做保守判断）
        if not card.effective:
            m = _DATE.search(text)
            if m and ("起" in text or "前" in text or "至" in text or "有效期" in text):
                card.effective = m.group(0).strip()

        # 收集来源
        if src and src["url"] and src not in card.sources:
            card.sources.append(src)

    # 补贴标准去重 + 去噪（太泛的不要）
    card.subsidy = [s for s in dict.fromkeys(card.subsidy)][:5]
    # 来源只保留权威度高的前 5 个
    order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    card.sources.sort(key=lambda s: order.get(s.get("level", ""), 5))
    card.sources = card.sources[:5]
    return card


def enrich_with_llm(card: PolicyCard, pack: KnowledgePack, llm,
                    max_findings: int = 12, graph_context: str = "") -> PolicyCard:
    """用 LLM 补齐语义字段（政策名称/适用对象），并修整格式。

    严格约束：只允许使用给定发现里出现的信息，抽不到就留空。

    graph_context（GraphRAG 接入点）：
        来自固定语料图谱的 2 跳关系链。政策演化/依据链这类【多跳事实】
        正是纯 RAG 的短板（评测里国家光伏、全国碳排用例波动的原因）。
        采信规则：图谱来自精读全文的结构化抽取，比搜索摘要片段更可信，
        字段冲突时【图谱优先】。
    """
    if llm is None or (not pack.findings and not graph_context):
        return card

    findings_text = "\n".join(
        f"[{i}] {f.claim}（{f.level}级来源）" for i, f in enumerate(pack.findings[:max_findings], 1)
    )
    graph_block = f"\n\n{graph_context}\n" if graph_context else ""
    graph_rule = (
        "\n4. 若提供了【政策知识图谱】，它与发现冲突时以图谱为准"
        "（图谱来自全文精读，比搜索片段可信）\n" if graph_context else ""
    )
    prompt = (
        f"以下是关于政策的研究发现：\n\n{findings_text}{graph_block}\n\n"
        "请把信息整理成结构化 JSON。严格要求：\n"
        "1. 只能使用上述发现或图谱中出现的信息，绝对不要补充常识或推断\n"
        "2. 抽不到的字段填空字符串或空数组，不要写“未提及”“暂无”\n"
        "3. 只输出 JSON，不要解释" + graph_rule + "\n"
        "格式：\n"
        '{"title":"政策名称","doc_no":"文号","issuer":"发布主体",'
        '"subsidy":["补贴标准，原文保留数字"],"scope":"适用对象/范围",'
        '"effective":"生效期或有效期","key_points":["其他关键点"]}'
    )
    try:
        resp = llm.invoke([
            {"role": "system", "content": "你是严谨的政策信息抽取员，只依据给定文本，绝不编造。"},
            {"role": "user", "content": prompt},
        ], max_tokens=800)
        data = _parse_json(resp.content)
        if not data:
            return card

        # 只填补空字段，不覆盖正则已经抽到的（正则更准）
        if not card.title:
            card.title = _clean(data.get("title"))
        if not card.doc_no:
            card.doc_no = _clean(data.get("doc_no"))
        if not card.issuer:
            card.issuer = _clean(data.get("issuer"))
        if not card.scope:
            card.scope = _clean(data.get("scope"))
        if not card.effective:
            card.effective = _clean(data.get("effective"))

        # 补贴标准：LLM 结果补充进来（与正则结果合并去重）
        for s in (data.get("subsidy") or []):
            s = _clean(s)
            if s and s not in card.subsidy and len(card.subsidy) < 5:
                card.subsidy.append(s)

        for p in (data.get("key_points") or []):
            p = _clean(p)
            if p and p not in card.key_points and len(card.key_points) < 5:
                card.key_points.append(p)
    except Exception as e:
        logger.warning(f"LLM 结构化抽取失败（不影响正则结果）: {type(e).__name__}: {e}")
    return card


def _clean(v) -> str:
    """清洗模型输出：去掉"未提及""无"这类占位。"""
    s = str(v or "").strip()
    if s in ("未提及", "暂无", "无", "N/A", "null", "None", "-", ""):
        return ""
    return s


def _parse_json(text: str) -> Optional[dict]:
    """脏 JSON 清洗（复用项目一贯策略）。

    实测模型经常把对象包在数组里返回：[{...}]，所以要先尝试整个解析，
    再尝试取数组首元素。只找 "{" "}" 会把数组尾部当成额外内容而报错。
    """
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)

    def _try(raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    # 1) 直接解析整段
    got = _try(s)
    if got is not None:
        return got

    # 2) 截取最外层 {...}（对象形态）
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        got = _try(re.sub(r",\s*([}\]])", r"\1", s[a:b + 1]))
        if got is not None:
            return got

    # 3) 截取最外层 [...]（数组形态，取首元素）
    a, b = s.find("["), s.rfind("]")
    if a != -1 and b > a:
        got = _try(re.sub(r",\s*([}\]])", r"\1", s[a:b + 1]))
        if got is not None:
            return got

    logger.warning(f"政策 JSON 解析失败 | 原文: {text[:150]!r}")
    return None


def build_card(pack: KnowledgePack, llm=None, graph_context: str = "") -> PolicyCard:
    """构建政策卡片：正则先抽（准），LLM 补（全），可选图谱上下文（多跳）。"""
    card = extract_regex(pack)
    card = enrich_with_llm(card, pack, llm, graph_context=graph_context)
    return card

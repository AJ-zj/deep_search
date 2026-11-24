"""
统一证据契约：调研产出、决策消费的交接物。

这是整个体系最关键的一个文件——"调研→决策"能连成体系，
就是因为它俩约定了同一种数据格式，而不是各说各话。

设计思想（借鉴王存章简历的精髓）：
  每条结论(finding)都必须带来源(source)，"提案只接受已读取证据"。
  这样决策引擎排的行程，每一天都能溯源到调研证据，不是凭空编排。
"""
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any


@dataclass
class Finding:
    """一条带来源的研究发现。"""
    claim: str          # 结论，如"灵隐寺秋季人流大，建议早到"
    source: str = ""    # 来源 URL 或出处
    confidence: float = 1.0  # 置信度
    level: str = ""     # 来源权威等级 S/A/B/C/D（见 core/authority.py）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class KnowledgePack:
    """目的地知识包：调研引擎的产出，决策引擎的输入。"""
    topic: str = ""
    findings: List[Finding] = field(default_factory=list)
    preferences: Dict[str, Any] = field(default_factory=dict)

    def add(self, claim: str, source: str = "", confidence: float = 1.0,
            level: str = ""):
        """新增一条发现，自动跳过空结论（脏数据防御）。"""
        claim = (claim or "").strip()
        if claim:
            self.findings.append(Finding(claim, source, confidence, level))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "findings": [f.to_dict() for f in self.findings],
            "preferences": self.preferences,
        }

    def to_prompt(self) -> str:
        """转成给决策引擎看的文本：每条结论带编号和来源，方便引用。"""
        lines = [f"调研主题：{self.topic}", "", "调研发现（每条都带来源）："]
        for i, f in enumerate(self.findings, 1):
            src = f" [来源: {f.source}]" if f.source else ""
            lv = f"（{f.level}级来源）" if f.level else ""
            lines.append(f"[{i}] {f.claim}{lv}{src}")
        if self.preferences:
            lines.append("")
            lines.append(f"用户偏好：{self.preferences}")
        return "\n".join(lines)

    @property
    def stats(self) -> Dict[str, Any]:
        """来源质量统计，供前端展示。"""
        from collections import Counter
        levels = [f.level for f in self.findings if f.level]
        return {
            "total": len(self.findings),
            "by_level": dict(Counter(levels)),
            "has_source": sum(1 for f in self.findings if f.source),
        }


@dataclass
class Place:
    """行程中的一个具体地点（带地图与图片信息）。"""
    name: str
    time: str = ""           # 上午/下午/晚上
    desc: str = ""           # 描述
    ref: int = 0             # 依据的调研发现编号

    # 以下由高德/Unsplash 补全
    lng: str = ""
    lat: str = ""
    location: str = ""       # "lng,lat"（高德格式）
    address: str = ""
    photo: str = ""          # 图片 URL
    photo_thumb: str = ""
    photo_credit: str = ""
    nearby: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def has_geo(self) -> bool:
        return bool(self.lng and self.lat)


@dataclass
class DayPlan:
    """一天的行程安排。"""
    day: int
    activities: List[str] = field(default_factory=list)
    references: List[int] = field(default_factory=list)  # 引用了哪些调研发现编号
    places: List[Place] = field(default_factory=list)    # 结构化地点（带地图/图片）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TravelPlan:
    """完整行程：决策引擎的产出。"""
    topic: str = ""
    days: List[DayPlan] = field(default_factory=list)
    summary: str = ""
    weather: List[Dict[str, str]] = field(default_factory=list)  # 城市天气预报

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "days": [d.to_dict() for d in self.days],
            "summary": self.summary,
            "weather": self.weather,
        }

    def all_places(self) -> List[Place]:
        out = []
        for d in self.days:
            out.extend(d.places)
        return out

    def to_markdown(self, pack: KnowledgePack = None) -> str:
        """渲染成 Markdown 报告，带来源引用、地址与地图链接。"""
        lines = [f"# {self.topic} · 行程方案", ""]
        if self.summary:
            lines += [self.summary, ""]
        if self.weather:
            lines.append("## 天气预报")
            for w in self.weather:
                lines.append(f"- {w.get('date','')} 周{w.get('week','')}："
                             f"{w.get('day_weather','')} {w.get('day_temp','')}℃ / "
                             f"{w.get('night_weather','')} {w.get('night_temp','')}℃")
            lines.append("")
        for d in self.days:
            lines.append(f"## 第 {d.day} 天")
            if d.places:
                for p in d.places:
                    head = f"- **{p.time}｜{p.name}**" if p.time else f"- **{p.name}**"
                    lines.append(head)
                    if p.desc:
                        lines.append(f"  {p.desc}")
                    if p.address:
                        lines.append(f"  地址：{p.address}")
                    if p.has_geo:
                        lines.append(f"  坐标：{p.lat},{p.lng} ｜ [在地图中查看](https://uri.amap.com/marker?position={p.location}&name={p.name})")
                    if p.nearby:
                        names = "、".join(n.get("name", "") for n in p.nearby[:3])
                        lines.append(f"  附近：{names}")
            else:
                for act in d.activities:
                    lines.append(f"- {act}")
            if d.references:
                refs = "、".join(f"[{r}]" for r in d.references)
                lines.append(f"\n  > 依据调研发现：{refs}")
            lines.append("")
        if pack and pack.findings:
            lines += ["## 参考来源", ""]
            for i, f in enumerate(pack.findings, 1):
                lines.append(f"[{i}] {f.claim} — {f.source}")
        return "\n".join(lines)

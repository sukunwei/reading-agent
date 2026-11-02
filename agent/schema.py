#!/usr/bin/env python3
"""认知层的数据模型（pydantic v2）。

结构化输出的 schema 直接复用这里的模型：
    client.messages.parse(..., output_format=ChapterSummary)
→ SDK 自动转 JSON Schema 并校验返回。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field

# --------------------------------------------------------------------- 章节

class ChapterSummary(BaseModel):
    """单章精读笔记（ChapterSummaryAgent 的输出结构）。"""

    core_thesis: str = Field(
        description="核心主旨：用 2-4 句话概括这一章到底在讲什么、作者的主张是什么")
    key_points: list[str] = Field(
        default_factory=list,
        description="关键要点：3-8 条，每条一句话，必须能对应到原文的具体论断")
    concepts: list[str] = Field(
        default_factory=list,
        description="重要概念/术语：原文出现过的专有名词与概念，每个附一句解释")
    cases: list[str] = Field(
        default_factory=list,
        description="案例论据：作者用来支撑论点的具体事例、数据或历史事件")
    data_points: list[str] = Field(
        default_factory=list,
        description="关键数据：原文给出的数字、时间、比例等可核对的硬信息")
    reflections: str = Field(
        default="",
        description="我的思考：读完这一章后最值得记住的判断或延伸联想")
    open_questions: list[str] = Field(
        default_factory=list,
        description="待验证疑问：原文没有回答、或值得进一步查证的问题")


# --------------------------------------------------------------------- 插图


class FigureDescription(BaseModel):
    """单张插图的视觉模型转述（M8 插图分流）。"""

    type: str = Field(
        default="", description="图表类型：柱状图/折线图/饼图/表格/地图/照片/示意图/流程图/其他")
    title: str = Field(default="", description="图表标题")
    axes: dict[str, str] = Field(
        default_factory=dict, description="坐标轴说明：x 轴、y 轴分别是什么、范围与单位")
    key_data: list[str] = Field(
        default_factory=list, description="图上能读到的关键数据点或趋势，每条一句话")
    insight: str = Field(default="", description="一句话说明这张图想说明什么")
    source: str = Field(default="", description="数据来源（图注里给了就照抄）")


# --------------------------------------------------------------------- 全书评测

class ScoreDetail(BaseModel):
    """单个维度的评分与依据。"""

    score: float = Field(description="1-10 分，允许一位小数")
    reason: str = Field(description="给这个分的依据，必须引用原书内容，不能泛泛而谈")


class BookReview(BaseModel):
    """全书五维评测（BookReviewAgent 的输出结构）。"""

    positioning: str = Field(
        description="书籍整体定位：这本书是什么书、解决什么问题、属于哪一类")
    score_logic: ScoreDetail = Field(description="逻辑结构性：全书框架、章节递进、论证闭环")
    score_density: ScoreDetail = Field(description="内容干货密度：有效知识占比、含水程度")
    score_innovation: ScoreDetail = Field(description="观点创新度：新认知、新模型、反常识价值")
    score_practical: ScoreDetail = Field(description="落地实用性：可执行、可复用、可指导工作生活")
    score_inspire: ScoreDetail = Field(description="阅读启发价值：长期认知升级、思维增益")
    strengths: list[str] = Field(default_factory=list, description="本书核心优势")
    weaknesses: list[str] = Field(default_factory=list, description="本书短板与局限性")
    suitable_for: list[str] = Field(default_factory=list, description="适合人群")
    not_suitable_for: list[str] = Field(default_factory=list, description="不适合人群")
    reading_advice: list[str] = Field(default_factory=list, description="高效阅读建议")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def avg_score(self) -> float:
        vals = [self.score_logic.score, self.score_density.score, self.score_innovation.score,
                self.score_practical.score, self.score_inspire.score]
        return round(sum(vals) / len(vals), 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def level(self) -> str:
        """书籍评级（设计文档 §4.3 的固定规则）。"""
        a = self.avg_score
        if a >= 9.0:
            return "神作"
        if a >= 7.5:
            return "优质好书"
        if a >= 6.0:
            return "值得一读"
        if a >= 4.5:
            return "中规中矩"
        return "价值较低"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scores(self) -> dict[str, float]:
        return {
            "逻辑结构性": self.score_logic.score,
            "内容干货密度": self.score_density.score,
            "观点创新度": self.score_innovation.score,
            "落地实用性": self.score_practical.score,
            "阅读启发价值": self.score_inspire.score,
        }

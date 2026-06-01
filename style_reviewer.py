"""
style_reviewer.py —— 风格 Reviewer：指纹对比器

把 Writer 的产出文章和作者的风格指纹做五维对比，
每个维度输出 0-1 的相似度分，并指出哪里偏了、偏了多少。

用法：
    from style_fingerprint import StyleFingerprint, load_reference_text
    from style_reviewer import StyleReviewer, StyleReview

    # 从参考文章提取指纹（可以复用已有的 fingerprint）
    reference = StyleFingerprint.from_texts(load_reference_text(["ref1.md", "ref2.md"]))

    # 对 Writer 产出打分
    reviewer = StyleReviewer(reference_fingerprint=reference)
    review = reviewer.review(writer_output)

    # 查看总分
    print(review.overall_score)      # 0.82

    # 查看五个维度的分和原因
    for dim in review.dimensions:
        print(dim.name, dim.score, dim.reason)

    # 注入到 Reviewer Agent 的系统提示词
    from style_reviewer import inject_reviewer_rules
    reviewer_instructions = inject_reviewer_rules(base_instructions, reviewer)

Harness 系列第 13 篇：风格 Reviewer
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from style_fingerprint import (
    StyleFingerprint,
    SentenceMetrics,
    ParagraphMetrics,
    VocabularyMetrics,
    ToneMetrics,
    StructureMetrics,
    _split_sentences,
    _analyze_sentences,
    _analyze_paragraphs,
    _analyze_vocabulary,
    _analyze_tone,
    _analyze_structure,
)


# ============ 数据结构 ============

@dataclass
class DimensionScore:
    """单维度评分结果。"""
    name: str           # 维度名称
    score: float        # 相似度，0~1，1 = 完全吻合
    delta: str          # 偏差描述，e.g. "平均句长偏高 +18字"
    reason: str         # 人话解释，e.g. "Writer 这篇句子比你平时长很多"
    suggestion: str     # 修改建议，e.g. "把超过 50 字的句子拆成两句"

    def is_pass(self, threshold: float = 0.65) -> bool:
        return self.score >= threshold

    def label(self) -> str:
        if self.score >= 0.85:
            return "✅ 吻合"
        elif self.score >= 0.65:
            return "🟡 偏差"
        else:
            return "🔴 失真"


@dataclass
class StyleReview:
    """风格审查完整结果。"""
    overall_score: float                          # 五维加权平均，0~1
    dimensions: list[DimensionScore]              # 五个维度的详细评分
    pass_count: int = 0                           # 通过的维度数（≥0.65）
    worst_dimension: Optional[DimensionScore] = None  # 最差维度

    def to_feedback(self) -> str:
        """生成给 Writer 的反馈文本，可直接追加到下轮对话。"""
        lines = []
        lines.append(f"## 风格审查结果（总分 {self.overall_score:.0%}）\n")

        pass_dims = [d for d in self.dimensions if d.is_pass()]
        fail_dims = [d for d in self.dimensions if not d.is_pass()]

        if fail_dims:
            lines.append("### 需要修改")
            for d in fail_dims:
                lines.append(f"**{d.name}** {d.label()}（{d.score:.0%}）")
                lines.append(f"- 偏差：{d.delta}")
                lines.append(f"- 原因：{d.reason}")
                lines.append(f"- 建议：{d.suggestion}")
                lines.append("")
        if pass_dims:
            lines.append("### 写得对味")
            for d in pass_dims:
                lines.append(f"**{d.name}** {d.label()}（{d.score:.0%}）：{d.reason}")
            lines.append("")

        if self.overall_score >= 0.85:
            lines.append("总评：**味道对了，可以发。**")
        elif self.overall_score >= 0.65:
            lines.append(f"总评：**整体不错，但 {fail_dims[0].name if fail_dims else ''} 还不太对味，修一下再发。**")
        else:
            lines.append("总评：**风格偏差太大，建议 Writer 重写，或者带着这份反馈重新生成。**")

        return "\n".join(lines)

    def should_regenerate(self, threshold: float = 0.5) -> bool:
        """总分低于阈值时，建议让 Writer 重新生成而非修改。"""
        return self.overall_score < threshold


# ============ 核心：Reviewer ============

class StyleReviewer:
    """风格 Reviewer 核心类。

    持有一份参考指纹，对任意文章做五维评分。
    Reviewer Agent 初始化时创建一个实例，后续每篇文章复用。

    Args:
        reference_fingerprint: 作者的风格指纹（由 StyleFingerprint.from_texts 生成）
        weights: 五个维度的权重，默认均等。
                 顺序：[句子, 段落, 词汇, 语气, 结构]
    """

    DIMENSION_NAMES = ["句子", "段落", "词汇", "语气", "结构"]
    DEFAULT_WEIGHTS = [0.25, 0.15, 0.20, 0.25, 0.15]

    def __init__(
        self,
        reference_fingerprint: StyleFingerprint,
        weights: Optional[list[float]] = None,
    ):
        self.ref = reference_fingerprint
        self.weights = weights or self.DEFAULT_WEIGHTS
        assert abs(sum(self.weights) - 1.0) < 1e-6, "权重之和必须等于 1"

    def review(self, article_text: str) -> StyleReview:
        """对一篇文章进行风格审查，返回五维评分。

        Args:
            article_text: Writer 产出的文章全文

        Returns:
            StyleReview，含总分、各维度分、修改建议。
        """
        # 提取待审文章的指纹
        sentences = _split_sentences(article_text)
        paragraphs = [p.strip() for p in article_text.split("\n\n") if p.strip()]

        candidate = StyleFingerprint(
            sentence=_analyze_sentences(sentences),
            paragraph=_analyze_paragraphs(paragraphs),
            vocabulary=_analyze_vocabulary(article_text),
            tone=_analyze_tone(article_text),
            structure=_analyze_structure(article_text),
            source_text_count=1,
        )

        # 五维对比
        dim_scores = [
            self._score_sentence(self.ref.sentence, candidate.sentence),
            self._score_paragraph(self.ref.paragraph, candidate.paragraph),
            self._score_vocabulary(self.ref.vocabulary, candidate.vocabulary),
            self._score_tone(self.ref.tone, candidate.tone),
            self._score_structure(self.ref.structure, candidate.structure),
        ]

        # 加权总分
        overall = sum(d.score * w for d, w in zip(dim_scores, self.weights))
        overall = round(min(1.0, max(0.0, overall)), 3)

        pass_count = sum(1 for d in dim_scores if d.is_pass())
        worst = min(dim_scores, key=lambda d: d.score)

        return StyleReview(
            overall_score=overall,
            dimensions=dim_scores,
            pass_count=pass_count,
            worst_dimension=worst,
        )

    # ================================================================
    # 各维度评分函数
    # ================================================================

    def _score_sentence(self, ref: SentenceMetrics, cand: SentenceMetrics) -> DimensionScore:
        """句子维度：对比平均句长 + 短句/长句比例。"""
        if ref.avg_length == 0:
            return DimensionScore("句子", 1.0, "无基准数据", "无法评分", "")

        # 平均句长偏差（允许 ±20% 浮动）
        length_diff = cand.avg_length - ref.avg_length
        length_score = max(0.0, 1.0 - abs(length_diff) / (ref.avg_length * 0.4))

        # 短句比例偏差（允许 ±15 个百分点）
        short_diff = cand.short_ratio - ref.short_ratio
        short_score = max(0.0, 1.0 - abs(short_diff) / 0.3)

        score = round((length_score * 0.6 + short_score * 0.4), 3)

        if abs(length_diff) <= 5:
            delta = f"平均句长相近（基准 {ref.avg_length:.0f}字，产出 {cand.avg_length:.0f}字）"
            reason = "句子长短和你平时差不多"
            suggestion = "无需调整"
        elif length_diff > 0:
            delta = f"平均句长偏高 +{length_diff:.0f}字（基准 {ref.avg_length:.0f}字）"
            reason = f"Writer 这篇句子比你平时长 {length_diff:.0f} 字，读起来偏「文章感」"
            suggestion = f"把超过 {int(ref.avg_length * 1.5)} 字的句子拆成两句"
        else:
            delta = f"平均句长偏低 {length_diff:.0f}字（基准 {ref.avg_length:.0f}字）"
            reason = f"句子比你平时短了 {-length_diff:.0f} 字，节奏偏碎"
            suggestion = "适当合并逻辑紧密的短句"

        return DimensionScore("句子", score, delta, reason, suggestion)

    def _score_paragraph(self, ref: ParagraphMetrics, cand: ParagraphMetrics) -> DimensionScore:
        """段落维度：对比每段句数。"""
        if ref.avg_sentences == 0:
            return DimensionScore("段落", 1.0, "无基准数据", "无法评分", "")

        diff = cand.avg_sentences - ref.avg_sentences
        score = round(max(0.0, 1.0 - abs(diff) / (ref.avg_sentences * 0.6)), 3)

        if abs(diff) < 0.8:
            delta = f"每段句数相近（基准 {ref.avg_sentences:.1f}句，产出 {cand.avg_sentences:.1f}句）"
            reason = "段落节奏和你平时吻合"
            suggestion = "无需调整"
        elif diff > 0:
            delta = f"每段偏长 +{diff:.1f}句（基准 {ref.avg_sentences:.1f}句/段）"
            reason = f"段落比你平时长，读者容易视觉疲劳"
            suggestion = f"把超过 {int(ref.avg_sentences) + 2} 句的段落拆开"
        else:
            delta = f"每段偏短 {diff:.1f}句（基准 {ref.avg_sentences:.1f}句/段）"
            reason = "段落太碎，节奏散"
            suggestion = "逻辑相关的短段合并，保持一个完整的思考单元一段"

        return DimensionScore("段落", score, delta, reason, suggestion)

    def _score_vocabulary(self, ref: VocabularyMetrics, cand: VocabularyMetrics) -> DimensionScore:
        """词汇维度：对比人称代词 + 高频词重叠 + 专业术语密度。"""
        scores = []
        reasons = []
        suggestions = []
        deltas = []

        # 1. 人称代词是否一致
        pronoun_match = 1.0 if ref.personal_pronoun == cand.personal_pronoun else 0.3
        scores.append(pronoun_match)
        if pronoun_match < 1.0:
            deltas.append(f"人称偏移：基准「{ref.personal_pronoun}」→ 产出「{cand.personal_pronoun}」")
            reasons.append(f"你平时用「{ref.personal_pronoun}」，这篇用了「{cand.personal_pronoun}」，读者感觉不是你在说话")
            suggestions.append(f"把文中「{cand.personal_pronoun}」统一换回「{ref.personal_pronoun}」")
        else:
            deltas.append(f"人称代词一致（均为「{ref.personal_pronoun}」）")
            reasons.append(f"人称代词「{ref.personal_pronoun}」用得一致")

        # 2. 高频词重叠率（前20）
        ref_words = {w for w, _ in ref.top_words[:20]}
        cand_words = {w for w, _ in cand.top_words[:20]}
        if ref_words and cand_words:
            overlap = len(ref_words & cand_words) / len(ref_words | cand_words)
            scores.append(overlap)
            deltas.append(f"高频词重叠率 {overlap:.0%}")
            if overlap < 0.3:
                reasons.append("词汇选择和你平时差异较大，「小词」不够像你")
                suggestions.append(f"参考你的常用词：{'、'.join(list(ref_words)[:5])}")
            else:
                reasons.append("常用词汇基本吻合")
        else:
            scores.append(0.8)

        # 3. 专业术语密度偏差（允许 ±50%）
        if ref.tech_term_ratio > 0:
            tech_diff = abs(cand.tech_term_ratio - ref.tech_term_ratio) / ref.tech_term_ratio
            tech_score = max(0.0, 1.0 - tech_diff * 0.5)
            scores.append(tech_score)
            if tech_diff > 0.5:
                deltas.append(f"术语密度偏差 {(cand.tech_term_ratio - ref.tech_term_ratio):+.1%}")
                if cand.tech_term_ratio > ref.tech_term_ratio:
                    reasons.append("这篇专业词汇堆砌，比你平时重")
                    suggestions.append("减少缩写和技术名词，多用日常语言")
                else:
                    reasons.append("术语密度比你平时低，可能丢失了技术感")
                    suggestions.append("适当保留专业术语，别把技术文章写成散文")
            else:
                deltas.append(f"术语密度相近（基准 {ref.tech_term_ratio:.1%}）")
        else:
            scores.append(0.9)

        score = round(sum(scores) / len(scores), 3)
        delta = "；".join(deltas)
        reason = "；".join(reasons) if reasons else "词汇选择吻合"
        suggestion = suggestions[0] if suggestions else "无需调整"

        return DimensionScore("词汇", score, delta, reason, suggestion)

    def _score_tone(self, ref: ToneMetrics, cand: ToneMetrics) -> DimensionScore:
        """语气维度：对比破折号/感叹号/省略号/反问句密度。"""
        scores = []
        deltas = []

        def _dim_score(ref_val: float, cand_val: float, label: str, tol: float = 0.5) -> float:
            """单指标评分：允许 tol 的绝对偏差。"""
            diff = abs(cand_val - ref_val)
            s = max(0.0, 1.0 - diff / (tol + 0.01))
            direction = "偏高" if cand_val > ref_val else "偏低"
            if diff > tol * 0.5:
                deltas.append(f"{label} {direction}（基准 {ref_val:.1f} → 产出 {cand_val:.1f}/千字）")
            return s

        s1 = _dim_score(ref.dash_density, cand.dash_density, "破折号密度", tol=0.4)
        s2 = _dim_score(ref.exclamation_density, cand.exclamation_density, "感叹号密度", tol=0.3)
        s3 = _dim_score(ref.ellipsis_density, cand.ellipsis_density, "省略号密度", tol=0.3)
        s4 = _dim_score(ref.rhetorical_density, cand.rhetorical_density, "反问句密度", tol=0.5)

        score = round((s1 * 0.35 + s2 * 0.25 + s3 * 0.15 + s4 * 0.25), 3)

        if not deltas:
            delta = "各语气指标均在基准范围内"
            reason = "语气温度和你平时吻合"
            suggestion = "无需调整"
        else:
            delta = "；".join(deltas)
            # 找偏差最大的给建议
            if s1 < min(s2, s3, s4):
                reason = f"破折号用得{'太多' if cand.dash_density > ref.dash_density else '太少'}，你平时用破折号做插入解释"
                suggestion = "保持每千字约 {:.1f} 次破折号".format(ref.dash_density)
            elif s2 < min(s1, s3, s4):
                reason = f"感叹号{'过多，太煽情' if cand.exclamation_density > ref.exclamation_density else '太少，语气偏冷'}"
                suggestion = "感叹号每千字约 {:.1f} 个".format(ref.exclamation_density)
            elif s4 < min(s1, s2, s3):
                reason = "反问句{}，你习惯用反问推动读者思考".format(
                    "用得太少，语气偏平" if cand.rhetorical_density < ref.rhetorical_density else "过多"
                )
                suggestion = "每篇加 1-2 个「你有没有发现…？」类型的反问"
            else:
                reason = "省略号用量和基准有偏差"
                suggestion = "省略号用于留白，不要滥用"

        return DimensionScore("语气", score, delta, reason, suggestion)

    def _score_structure(self, ref: StructureMetrics, cand: StructureMetrics) -> DimensionScore:
        """结构维度：对比标题格式 + 列表符号 + 结尾风格。"""
        scores = []
        deltas = []
        suggestions = []

        # 小标题格式
        heading_match = 1.0 if ref.heading_style == cand.heading_style else 0.3
        scores.append(heading_match)
        if heading_match < 1.0:
            deltas.append(f"小标题格式：基准 {ref.heading_style} → 产出 {cand.heading_style}")
            suggestions.append(f"把小标题改成 {ref.heading_style} 格式")
        else:
            deltas.append(f"小标题格式一致（{ref.heading_style}）")

        # 列表符号
        list_match = 1.0 if ref.list_marker == cand.list_marker else 0.4
        scores.append(list_match)
        if list_match < 1.0:
            deltas.append(f"列表符号：基准「{ref.list_marker}」→ 产出「{cand.list_marker}」")
            suggestions.append(f"把列表符号换成「{ref.list_marker}」")

        # 列表密度偏差（允许 ±50%）
        if ref.list_density > 0:
            density_diff = abs(cand.list_density - ref.list_density) / ref.list_density
            density_score = max(0.0, 1.0 - density_diff * 0.5)
            scores.append(density_score)
            if density_diff > 0.5:
                direction = "过多" if cand.list_density > ref.list_density else "过少"
                deltas.append(f"列表占比{direction}（基准 {ref.list_density:.0%} → 产出 {cand.list_density:.0%}）")
        else:
            scores.append(0.9)

        # 结尾风格
        ending_match = 1.0 if ref.ending_style == cand.ending_style else 0.5
        scores.append(ending_match)
        if ending_match < 1.0:
            deltas.append(f"结尾风格：基准「{ref.ending_style}」→ 产出「{cand.ending_style}」")
            suggestions.append(f"结尾改成{ref.ending_style}风格（你平时习惯这样收尾）")

        score = round(sum(scores) / len(scores), 3)
        delta = "；".join(deltas) if deltas else "结构格式吻合"
        reason = "结构格式和基准一致" if score >= 0.8 else "有几个格式细节和你的习惯不符"
        suggestion = suggestions[0] if suggestions else "无需调整"

        return DimensionScore("结构", score, delta, reason, suggestion)

    # ================================================================
    # 快捷方法
    # ================================================================

    def quick_check(self, article_text: str, threshold: float = 0.65) -> tuple[bool, str]:
        """快速检查：通过/不通过 + 简短原因。

        Returns:
            (passed: bool, reason: str)
        """
        review = self.review(article_text)
        passed = review.overall_score >= threshold
        if passed:
            reason = f"风格评分 {review.overall_score:.0%}，{review.pass_count}/5 个维度通过，可以发。"
        else:
            worst = review.worst_dimension
            reason = (
                f"风格评分 {review.overall_score:.0%}，"
                f"最差维度「{worst.name if worst else ''}」（{worst.score:.0%}）：{worst.reason if worst else ''}。"
                f"建议：{worst.suggestion if worst else ''}"
            )
        return passed, reason


# ============ run.py 集成 ============

def inject_reviewer_rules(
    base_instructions: str,
    reviewer: StyleReviewer,
    threshold: float = 0.65,
) -> str:
    """将风格 Reviewer 配置注入到 Reviewer Agent 的系统提示词。

    用法（在 run.py 的 reviewer_agent 中）：
        base = read_file("instructions/reviewer.md")
        reviewer_instructions = inject_reviewer_rules(base, reviewer)

    Args:
        base_instructions: 原有的 Reviewer 提示词
        reviewer: 已初始化的 StyleReviewer 实例
        threshold: 通过门槛（默认 0.65，低于此分要求修改）

    Returns:
        注入了风格审查规则的完整提示词
    """
    ref = reviewer.ref
    weights = reviewer.weights

    lines = [
        "\n\n## 风格审查规则（自动注入）",
        "",
        "你不只是内容 Reviewer，你还是风格 Reviewer。",
        "审查产出文章时，除了内容质量，还要对比以下风格基准：",
        "",
        "### 风格基准（来自参考文章）",
        f"- 句子：平均句长 {ref.sentence.avg_length:.0f}字，短句占比 {ref.sentence.short_ratio:.0%}",
        f"- 段落：平均每段 {ref.paragraph.avg_sentences:.0f}句",
        f"- 词汇：主要人称代词「{ref.vocabulary.personal_pronoun}」，专业术语密度 {ref.vocabulary.tech_term_ratio:.1%}",
        f"- 语气：破折号 {ref.tone.dash_density:.1f}/千字，感叹号 {ref.tone.exclamation_density:.1f}/千字",
        f"- 结构：小标题用 {ref.structure.heading_style}，列表用「{ref.structure.list_marker}」，结尾风格「{ref.structure.ending_style}」",
        "",
        f"### 评分规则",
        f"五个维度（权重：句子 {weights[0]:.0%} / 段落 {weights[1]:.0%} / 词汇 {weights[2]:.0%} / 语气 {weights[3]:.0%} / 结构 {weights[4]:.0%}）",
        f"每个维度 0~1 分，加权总分低于 {threshold:.0%} 则要求 Writer 修改。",
        "",
        "### 输出格式",
        "在审查报告末尾增加「风格审查」小节，列出：",
        "1. 总分（百分比）",
        "2. 各维度：通过 ✅ / 偏差 🟡 / 失真 🔴",
        "3. 失真维度给出具体修改建议",
        "4. 最终结论：可以发 / 修改后发 / 建议重写",
    ]

    return base_instructions + "\n".join(lines)


# ============ 自测 ============

if __name__ == "__main__":
    print("=" * 60)
    print("StyleReviewer 自测")
    print("=" * 60)

    # 参考文章（代表作者真实风格）
    reference_text = """
给 Writer 写了最详细的指令，文章读起来就是不像我写的。

八层治理全到位了。格式没错，论据没错，连标点符号都没错。但发到公众号，读者问「这篇文章是 AI 写的吧？」

不是指令不够细。我给了 Writer 最详细的写作规则。遵守了通用规则的文字，读起来就是通用文字。**它不知道「我的风格」长什么样。**

先说句子。你写文章，句长是固定的吗？不是。短句是刀刃，长句是铺垫。

我统计了自己的文章：平均句长约 30 字，但短句（≤20 字）占比接近 25%。AI 不知道这个数据，所有句子都差不多长——读者读到第三段就开始走神。

再说段落。你自己回看一下自己的文章，一段大概多少句？一般是 3-5 句。最长的一段不会超过 8 句——那是刻意为之，密集论证时才用。

词汇更明显。你有没有自己的高频词？「但是」、「你可能会问」、「问题在于」——读者读多了，会觉得「这确实是她在说话」。

风格不是玄学。一段话的风格，可以拆成五个维度：句子怎么切、段落怎么铺、用什么词、带什么语气、用什么结构。每个维度都是数字。数字就能传给 AI。
    """

    # 提取基准指纹
    from style_fingerprint import StyleFingerprint
    ref_fp = StyleFingerprint.from_texts([reference_text])
    reviewer = StyleReviewer(reference_fingerprint=ref_fp)

    print("\n基准指纹：")
    for line in ref_fp.summary():
        print(f"  {line}")

    # 测试1：风格吻合的文章（相似风格）
    print("\n" + "-" * 40)
    print("测试1：风格吻合的文章")
    print("-" * 40)
    similar_article = """
这次踩的坑不是代码问题。

我以为 Reviewer 只要检查内容对不对就够了。结果每次 Writer 输出，我还要自己读一遍才能判断——「这像我写的吗？」

答案经常是：不像。但我说不清哪里不对。

指纹系统打完之后，问题变得清晰了。不是内容的问题，是五个维度都没对上。句子比我平时长，段落比我平时碎，连结尾都不是我的风格。

所以 Reviewer 需要一把尺子。不是审内容的尺子，是审调调的尺子。

你有没有也遇到过这种情况——内容都对，但读起来就是感觉哪里不对劲？

下一篇讲怎么把这把尺子做成代码。
    """

    review1 = reviewer.review(similar_article)
    print(f"总分：{review1.overall_score:.0%}，通过维度：{review1.pass_count}/5")
    for d in review1.dimensions:
        print(f"  {d.name} {d.label()} ({d.score:.0%})：{d.reason}")
    print(f"\n反馈预览：\n{review1.to_feedback()}")

    # 测试2：风格偏离的文章（正式学术风）
    print("\n" + "-" * 40)
    print("测试2：风格偏离的文章（学术风）")
    print("-" * 40)
    divergent_article = """
在多智能体系统的工程实践中，输出一致性是衡量系统质量的重要维度之一。传统的内容质量评估方法侧重于事实准确性、逻辑连贯性等维度，然而在个性化内容生成场景下，风格一致性同样不可忽视。

本文提出一种基于量化指纹的风格审查方法。该方法将写作风格分解为句子、段落、词汇、语气、结构五个可量化维度，通过统计分析建立参考指纹，并以相似度评分评估候选文本与参考指纹的偏差程度。

实验结果表明，该方法能够有效识别风格偏离案例。在对照实验中，与人工评审相比，自动化风格审查在准确率方面达到了令人满意的水平。

综上所述，风格指纹结合量化评分的方法，为多智能体写作系统的质量保障提供了一种可行的技术路径。建议将该模块集成到现有的Reviewer Agent工作流中，作为内容质量评审的补充环节。
    """

    review2 = reviewer.review(divergent_article)
    print(f"总分：{review2.overall_score:.0%}，通过维度：{review2.pass_count}/5")
    for d in review2.dimensions:
        print(f"  {d.name} {d.label()} ({d.score:.0%})：{d.reason}")

    # 快速检查
    print("\n" + "-" * 40)
    print("quick_check 测试")
    print("-" * 40)
    passed1, reason1 = reviewer.quick_check(similar_article)
    passed2, reason2 = reviewer.quick_check(divergent_article)
    print(f"相似风格：{'通过' if passed1 else '不通过'} — {reason1}")
    print(f"学术风格：{'通过' if passed2 else '不通过'} — {reason2}")

    print("\n" + "=" * 60)
    print("自测完成")
    print("=" * 60)

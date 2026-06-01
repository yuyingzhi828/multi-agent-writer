"""
style_fingerprint.py —— 写作风格指纹提取与注入

把一个作者的写作风格拆成 5 个可量化维度：
1. 句子：句长分布、短句/长句比例
2. 段落：每段句数、开头模式
3. 词汇：高频词、人称代词、专业术语密度
4. 语气：反问句、感叹句、省略号/破折号
5. 结构：分节习惯、列表使用、衔接词

用法：
    from style_fingerprint import StyleFingerprint, inject_style_rules

    # 从已有文章提取指纹
    fingerprint = StyleFingerprint.from_texts(article_texts)

    # 注入到 Writer 指令
    style_rules = fingerprint.to_writer_rules()

    # 在 run.py 中：传给 Writer
    state.style_rules = style_rules

Harness 系列第 12 篇：风格指纹
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections import Counter
from typing import Optional


# ============ 数据结构 ============

@dataclass
class SentenceMetrics:
    """句子维度的量化特征。

    数据来源：按中文标点（。！？）分句后统计。
    """
    avg_length: float = 0.0          # 平均句长（字符数）
    length_std: float = 0.0          # 句长标准差
    short_ratio: float = 0.0         # 短句占比（≤20字），说话利落程度
    long_ratio: float = 0.0          # 长句占比（＞60字），信息密集度
    total_sentences: int = 0

    def summary(self) -> str:
        short_pct = round(self.short_ratio * 100)
        long_pct = round(self.long_ratio * 100)
        return (
            f"平均句长 {self.avg_length:.0f}字（标准差 {self.length_std:.0f}），"
            f"短句（≤20字）{short_pct}%，长句（＞60字）{long_pct}%，共 {self.total_sentences} 句"
        )


@dataclass
class ParagraphMetrics:
    """段落维度的量化特征。

    数据来源：按空行分段落，统计每段句数及开头模式。
    """
    avg_sentences: float = 0.0       # 平均每段句数
    max_sentences: int = 0           # 最多句数的段落
    openings: list[str] = field(default_factory=list)  # 各段开头前15字
    total_paragraphs: int = 0

    def summary(self) -> str:
        return (
            f"平均每段 {self.avg_sentences:.0f} 句，"
            f"最长一段 {self.max_sentences} 句，共 {self.total_paragraphs} 段"
        )


@dataclass
class VocabularyMetrics:
    """词汇维度的量化特征。

    数据来源：jieba 分词后统计词频（无 jieba 时退化为简单切分）。
    """
    top_words: list[tuple[str, int]] = field(default_factory=list)  # 前20高频词
    personal_pronoun: str = "你"      # 主要人称代词（你/您/我/我们）
    personal_density: float = 0.0    # 人称代词密度（每千字）
    tech_term_ratio: float = 0.0     # 专业术语占比（含英文/缩写词）
    total_words: int = 0

    def summary(self) -> str:
        top5 = [w for w, _ in self.top_words[:5]]
        return (
            f"高频词：{', '.join(top5)}，"
            f"人称代词以「{self.personal_pronoun}」为主（{self.personal_density:.1f}次/千字），"
            f"专业术语密度 {self.tech_term_ratio:.1%}，共 {self.total_words} 词"
        )


@dataclass
class ToneMetrics:
    """语气维度的量化特征。

    数据来源：统计标点符号和句式模式。
    """
    rhetorical_density: float = 0.0   # 反问句密度（每千字「？」前有「难道/怎么/是不是」等）
    exclamation_density: float = 0.0  # 感叹号密度（每千字）
    ellipsis_density: float = 0.0     # 省略号密度（每千字）
    dash_density: float = 0.0         # 破折号密度（每千字）
    colon_usage: str = "列表"         # 冒号后接什么：列表（-/*）还是解释（文字）
    total_chars: int = 0

    def summary(self) -> str:
        parts = []
        if self.rhetorical_density > 0.5:
            parts.append("惯用反问")
        if self.exclamation_density > 0.3:
            parts.append("情绪外露")
        if self.ellipsis_density > 0.5:
            parts.append("用省略号留白")
        if self.dash_density > 0.3:
            parts.append("用破折号解释")
        if not parts:
            parts.append("平实直接")
        return f"语气：{'、'.join(parts)}"


@dataclass
class StructureMetrics:
    """结构维度的量化特征。

    数据来源：分析 Markdown 标题层级、列表符号、衔接词模式。
    """
    heading_style: str = "##"         # 主要小标题格式（##/###/**加粗**）
    list_marker: str = "-"            # 列表符号（-/1./*）
    list_density: float = 0.0         # 列表行占比
    transition_patterns: list[str] = field(default_factory=list)  # 常见衔接词
    code_ratio: float = 0.0           # 代码块占比
    bold_ratio: float = 0.0           # 加粗文本占比
    ending_style: str = "提问"        # 结尾风格（提问/预告/总结/CTA）

    def summary(self) -> str:
        return (
            f"小标题用 {self.heading_style}，"
            f"列表用 {self.list_marker}，"
            f"衔接词：{'、'.join(self.transition_patterns[:4]) or '(未检测到)'}"
        )


# ============ 完整指纹 ============

@dataclass
class StyleFingerprint:
    """一个作者的完整风格指纹，5 个维度。

    用法：
        texts = ["文章1全文", "文章2全文"]
        fp = StyleFingerprint.from_texts(texts)
        rules = fp.to_writer_rules()
    """
    sentence: SentenceMetrics = field(default_factory=SentenceMetrics)
    paragraph: ParagraphMetrics = field(default_factory=ParagraphMetrics)
    vocabulary: VocabularyMetrics = field(default_factory=VocabularyMetrics)
    tone: ToneMetrics = field(default_factory=ToneMetrics)
    structure: StructureMetrics = field(default_factory=StructureMetrics)
    source_text_count: int = 0

    # ================================================================
    # 工厂方法：从文本列表提取指纹
    # ================================================================

    @classmethod
    def from_texts(cls, texts: list[str]) -> StyleFingerprint:
        """从一组参考文章中提取风格指纹。

        Args:
            texts: 作者的代表性文章全文列表，建议 3-10 篇。

        Returns:
            聚合后的 StyleFingerprint。
        """
        fp = cls(source_text_count=len(texts))
        if not texts:
            return fp

        combined = "\n\n".join(texts)

        # 分句
        sentences = _split_sentences(combined)

        # 分段落
        paragraphs = [p.strip() for p in combined.split("\n\n") if p.strip()]

        fp.sentence = _analyze_sentences(sentences)
        fp.paragraph = _analyze_paragraphs(paragraphs)
        fp.vocabulary = _analyze_vocabulary(combined)
        fp.tone = _analyze_tone(combined)
        fp.structure = _analyze_structure(combined)

        return fp

    # ================================================================
    # 转换为 Writer 指令
    # ================================================================

    def to_writer_rules(self) -> str:
        """将风格指纹转换为 Writer Agent 可读的指令文本。

        Returns:
            Writer 系统提示词的风格规则部分。
        """
        s = self.sentence
        p = self.paragraph
        v = self.vocabulary
        t = self.tone
        st = self.structure

        lines = []

        # 句子
        lines.append("## 句子风格")
        lines.append(f"- 平均句长约 {s.avg_length:.0f} 字，单句不要超过 {max(60, int(s.avg_length) * 2)} 字")
        if s.short_ratio > 0.2:
            lines.append(f"- 短句（≤20字）占 {s.short_ratio:.0%}，保持短句节奏，关键信息单句说清楚")
        if s.long_ratio > 0.1:
            lines.append(f"- 长句（＞60字）仅占 {s.long_ratio:.0%}，复杂信息拆成两个中句，不要用长句堆砌")
        lines.append("")

        # 段落
        lines.append("## 段落结构")
        lines.append(f"- 每段通常 {max(2, int(p.avg_sentences))}~{max(4, int(p.avg_sentences) + 2)} 句，段长步调一致")
        if p.max_sentences > 8:
            lines.append(f"- 一段最多 {p.max_sentences} 句时是刻意为之（密集论证），不要每段都这么长")
        lines.append("")

        # 词汇
        lines.append("## 词汇选择")
        if v.personal_pronoun == "你":
            lines.append("- 默认用「你」对话读者，不用「您」（太正式）或「大家」（太泛）")
        elif v.personal_pronoun == "我":
            lines.append("- 用「我」自称，建立作者存在感，不用「笔者」")
        elif v.personal_pronoun == "我们":
            lines.append("- 用「我们」建立统一战线感，和读者站在一起")
        if v.tech_term_ratio > 0.03:
            lines.append(f"- 专业术语密度约 {v.tech_term_ratio:.1%}，保留技术词汇但不堆砌")
        if v.tech_term_ratio < 0.01:
            lines.append("- 几乎不用技术术语，用日常语言讲道理")
        lines.append("")

        # 语气
        lines.append("## 语气控制")
        if t.rhetorical_density > 0.5:
            lines.append(f"- 反问句密度 {t.rhetorical_density:.1f}/千字，惯用反问推动读者思考")
        if t.exclamation_density > 0.3:
            lines.append("- 适度使用感叹号表达强烈观点，但一段最多一个")
        if t.ellipsis_density > 0.5:
            lines.append("- 省略号密度较高，用于留白和语气停顿")
        if t.dash_density > 0.3:
            lines.append("- 用破折号做插入解释，不用括号")
        lines.append("")

        # 结构
        lines.append("## 文章结构")
        lines.append(f"- 小标题用 {st.heading_style}")
        lines.append(f"- 列表用「{st.list_marker}」开头")
        if st.code_ratio > 0.05:
            lines.append(f"- 代码块占比约 {st.code_ratio:.1%}，技术文章保持代码示例")
        if st.code_ratio == 0:
            lines.append("- 不用代码块，纯文字表达")
        if st.bold_ratio > 0.01:
            lines.append("- 核心观点用加粗标注，帮助读者快速抓住重点")
        if st.ending_style == "提问":
            lines.append("- 结尾风格：抛一个具体问题给读者，不做总结")
        elif st.ending_style == "预告":
            lines.append("- 结尾风格：预告下一篇内容")

        return "\n".join(lines)

    def summary(self) -> list[str]:
        """各维度摘要，用于调试和文章引用。"""
        return [
            f"句子：{self.sentence.summary()}",
            f"段落：{self.paragraph.summary()}",
            f"词汇：{self.vocabulary.summary()}",
            f"语气：{self.tone.summary()}",
            f"结构：{self.structure.summary()}",
        ]


# ============ 分析函数（私有，每个维度独立） ============

def _split_sentences(text: str) -> list[str]:
    """按中文标点分句：。！？"""
    sentences = re.split(r'[。！？\n]', text)
    return [(s.strip() + "。") for s in sentences if len(s.strip()) > 5]


def _analyze_sentences(sentences: list[str]) -> SentenceMetrics:
    if not sentences:
        return SentenceMetrics()
    lengths = [len(s) for s in sentences]
    n = len(lengths)
    avg = sum(lengths) / n
    variance = sum((l - avg) ** 2 for l in lengths) / n
    short = sum(1 for l in lengths if l <= 20)
    long = sum(1 for l in lengths if l > 60)
    return SentenceMetrics(
        avg_length=avg,
        length_std=variance ** 0.5,
        short_ratio=short / n,
        long_ratio=long / n,
        total_sentences=n,
    )


def _analyze_paragraphs(paragraphs: list[str]) -> ParagraphMetrics:
    if not paragraphs:
        return ParagraphMetrics()
    sentence_counts = []
    openings = []
    for p in paragraphs:
        sents = _split_sentences(p)
        sentence_counts.append(len(sents))
        openings.append(p[:15] if len(p) > 15 else p)
    counts = [c or 1 for c in sentence_counts]  # 避免除0
    return ParagraphMetrics(
        avg_sentences=sum(counts) / len(counts),
        max_sentences=max(counts),
        openings=openings,
        total_paragraphs=len(paragraphs),
    )


def _analyze_vocabulary(text: str) -> VocabularyMetrics:
    """分词统计高频词、人称代词密度、专业术语密度。"""
    # 尝试 jieba 分词
    words = _tokenize(text)
    total = len(words)
    if total == 0:
        return VocabularyMetrics(total_words=0)

    # 过滤停用词（最小集）
    stopwords = {"的", "了", "是", "在", "和", "就", "都", "也", "有", "不",
                 "这", "那", "一个", "这个", "那个", "可以", "没有", "自己"}
    content_words = [w for w in words if w not in stopwords and len(w) > 1]

    # 前20高频词
    counter = Counter(content_words)
    top_words = counter.most_common(20)

    # 人称代词密度
    personal_terms = {"你": 0, "您": 0, "我": 0, "我们": 0, "大家": 0, "读者": 0}
    for w in content_words:
        if w in personal_terms:
            personal_terms[w] += 1
    total_personal = sum(personal_terms.values())

    # 确定主要人称代词
    max_term = max(personal_terms, key=personal_terms.get)  # type: ignore
    personal_density = total_personal / (len(text) / 1000) if len(text) > 0 else 0

    # 专业术语密度（含英文词、缩写、技术名词）
    tech_count = sum(1 for w in content_words if _is_tech_term(w))
    tech_ratio = tech_count / total

    return VocabularyMetrics(
        top_words=top_words,
        personal_pronoun=max_term,
        personal_density=round(personal_density, 1),
        tech_term_ratio=tech_ratio,
        total_words=total,
    )


def _analyze_tone(text: str) -> ToneMetrics:
    """统计语气特征：反问句、感叹、省略号、破折号、冒号用法。"""
    char_count = len(text)
    if char_count == 0:
        return ToneMetrics(total_chars=0)

    per_k = char_count / 1000

    # 反问句密度（？前有反问关键词）
    rhetorical_patterns = re.findall(
        r'(难道|是不是|怎么|哪有|谁说的|何必|不是吗|对吧|是不是这样)[^。！？]*\?',
        text,
    )
    rhetorical_density = len(rhetorical_patterns) / per_k if per_k > 0 else 0

    # 感叹号密度
    exclamation_count = text.count("！")
    exclamation_density = exclamation_count / per_k if per_k > 0 else 0

    # 省略号密度
    ellipsis_count = text.count("…") + text.count("...")
    ellipsis_density = ellipsis_count / per_k if per_k > 0 else 0

    # 破折号密度
    dash_count = text.count("——") + text.count("—")
    dash_density = dash_count / per_k if per_k > 0 else 0

    # 冒号后接什么
    colon_list = len(re.findall(r'：\s*[\n-]', text))  # 冒号+列表
    colon_text = len(re.findall(r'：[^：\n-]{10,}', text))  # 冒号+解释
    colon_usage = "列表" if colon_list > colon_text else "解释"

    return ToneMetrics(
        rhetorical_density=round(rhetorical_density, 1),
        exclamation_density=round(exclamation_density, 1),
        ellipsis_density=round(ellipsis_density, 1),
        dash_density=round(dash_density, 1),
        colon_usage=colon_usage,
        total_chars=char_count,
    )


def _analyze_structure(text: str) -> StructureMetrics:
    """分析 Markdown 结构特征。"""
    lines = text.split("\n")
    total_lines = len(lines)
    if total_lines == 0:
        return StructureMetrics()

    # 小标题格式
    h2_count = sum(1 for l in lines if l.startswith("## "))
    h3_count = sum(1 for l in lines if l.startswith("### "))
    bold_heading_count = sum(1 for l in lines if l.startswith("**") and l.endswith("**"))
    if h3_count > h2_count:
        heading_style = "###"
    elif bold_heading_count > h2_count:
        heading_style = "加粗文本"
    else:
        heading_style = "##"

    # 列表符号
    dash_count = sum(1 for l in lines if re.match(r'^- ', l.strip()))
    num_count = sum(1 for l in lines if re.match(r'^\d+\. ', l.strip()))
    star_count = sum(1 for l in lines if re.match(r'^\* ', l.strip()))
    list_marker = "-" if dash_count >= max(num_count, star_count) else ("1." if num_count >= star_count else "*")

    # 列表行占比
    list_lines = dash_count + num_count + star_count
    list_density = list_lines / total_lines if total_lines > 0 else 0

    # 常见衔接词
    transitions = {
        "因此": 0, "所以": 0, "但是": 0, "但": 0, "不过": 0,
        "更重要的是": 0, "回头看": 0, "换句话说": 0, "也就是说": 0,
        "你可能会问": 0, "你会发现": 0, "关键": 0, "问题在于": 0,
    }
    text_lower = text
    for kw in transitions:
        transitions[kw] = text_lower.count(kw)
    # 取出现次数最多的前3个衔接词
    top_transitions = [k for k, v in sorted(transitions.items(), key=lambda x: -x[1])[:4] if v > 0]

    # 代码块占比
    code_lines = sum(1 for l in lines if l.strip().startswith("```"))
    code_ratio = code_lines / (2 * total_lines) if total_lines > 0 else 0  # 一对 ``` 算两行

    # 加粗文本占比
    bold_lines = sum(1 for l in lines if "**" in l)
    bold_ratio = bold_lines / total_lines if total_lines > 0 else 0

    # 结尾风格（检测正文最后一个段落）
    content_paragraphs = [l for l in reversed(lines) if l.strip() and not l.startswith("```") and not l.startswith("---")]
    ending_style = "提问"
    if content_paragraphs:
        last = content_paragraphs[0]
        if last.endswith("？") or "？" in last:
            ending_style = "提问"
        elif "下一篇" in last:
            ending_style = "预告"
        elif "总结" in last or "综上" in last:
            ending_style = "总结"
        elif "点赞" in last or "转发" in last:
            ending_style = "CTA"

    return StructureMetrics(
        heading_style=heading_style,
        list_marker=list_marker,
        list_density=list_density,
        transition_patterns=top_transitions,
        code_ratio=code_ratio,
        bold_ratio=bold_ratio,
        ending_style=ending_style,
    )


# ============ 辅助函数 ============

def _tokenize(text: str) -> list[str]:
    """分词：优先 jieba，否则简单字符切分。"""
    try:
        import jieba
        return list(jieba.cut(text))
    except ImportError:
        # 简单回退：按标点+空格切分，取2字以上片段
        chunks = re.split(r'[，。！？、：；\n\s（）""''…—*#`-]', text)
        return [c.strip() for c in chunks if len(c.strip()) > 1]


def _is_tech_term(word: str) -> bool:
    """判断是否为技术术语（含英文、缩写、已知技术词）。"""
    tech_keywords = {
        "token", "api", "agent", "pipeline", "schema", "json", "api",
        "prompt", "llm", "gpt", "sd", "commit", "push", "repo",
        "函数", "模块", "接口", "参数", "配置", "架构", "部署",
    }
    if word.lower() in tech_keywords:
        return True
    # 含大写或数字的缩略词（如 API、SDD、AI）
    if re.search(r'[A-Z0-9]{2,}', word):
        return True
    # 含英文的混合词
    if re.search(r'[a-zA-Z]', word):
        return True
    return False


# ============ run.py 集成 ============

def inject_style_rules(base_instructions: str, fingerprint: StyleFingerprint) -> str:
    """将风格指纹注入到 Writer 的系统提示词中。

    用法（在 run.py 的 writer_agent 中）：
        base = read_file("instructions/writer.md")
        style_rules = fingerprint.to_writer_rules()
        writer_instructions = inject_style_rules(base, fingerprint)

    指纹规则追加在原有规则之后，以 `## 风格指纹` 标记。
    """
    style_section = fingerprint.to_writer_rules()
    return f"{base_instructions}\n\n## 风格指纹（从参考文章自动提取）\n{style_section}"


# ============ 文件操作辅助 ============

def load_reference_text(file_paths: list[str]) -> list[str]:
    """加载参考文章，返回文本列表。"""
    texts = []
    for path in file_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                texts.append(f.read())
        except FileNotFoundError:
            print(f"[StyleFingerprint] 文件未找到，跳过：{path}")
        except Exception as e:
            print(f"[StyleFingerprint] 读取失败 {path}: {e}")
    return texts


# ============ 自测 ============

if __name__ == "__main__":
    print("=" * 60)
    print("StyleFingerprint 自测")
    print("=" * 60)

    # 构造测试语料（模拟一个科技博主的写作风格）
    sample = """
给 Writer 写了最详细的指令，文章读起来就是不像我写的。

八层治理全到位了。格式没错，论据没错，连标点符号都没错。但发到公众号，我自己都读不出是我写的。问题出在哪？不是指令不够细。是指令里从来没告诉它「我的风格」。

风格不是玄学。一段话的风格，可以拆成五个维度：句子怎么切、段落怎么铺、用什么词、带什么语气、用什么结构。每个维度都是数字。数字就能传给 AI。

先说句子。你写文章，句长是固定的吗？不是。短句是刀刃，长句是铺垫。我的平均句长约 30 字，但短句占比接近 25%。这意味着每四句里，就有一句一刀切。AI 不知道这个数据，它就按默认的平均句长写，结果就是「文笔不错但不像你」。

再说段落。你自己回看自己的文章，一段大概多少句？一般 3-5 句。最长的一段不会超过 8 句——那是刻意为之，密集论证时才用。AI 没有这个约束，段落忽长忽短，读起来节奏就乱了。

词汇更明显。你有没有自己的高频词？「但是」、「你会发现」、「问题在于」——这些词你自己未必注意到，但它们就是你的指纹。AI 写的时候用的是通用词汇库，不会复现你的这些「小词」。

语气最难量化但也最决定风格。反问句密度、感叹号频率、省略号和破折号怎么用——你写文章的时候从不算这个，但你的读者能感觉到。AI 写出来要么太冷静要么太煽情，就是因为它不知道你的「语气温度」。

最后一层是结构。你怎么分节？用 ## 还是 ###？要不要列表？结尾是抛问题还是做总结？这些看起来是排版，实际上是你和读者的约法三章。AI 不知道你立了什么规矩，它就按自己的习惯来。

所以怎么办？不是调提示词。是先把你的风格拆成数字，再把数字变成 AI 能读懂的约束。

我们把前面这五个维度写成一个类：StyleFingerprint。给它喂几篇你自己的文章，它自动算出所有数据。然后一行代码，把指纹变成 Writer 的系统提示词。
"""

    # 从测试语料提取指纹
    fp = StyleFingerprint.from_texts([sample])
    print(f"\n提取自 {fp.source_text_count} 篇参考文章\n")

    for line in fp.summary():
        print(f"  {line}")

    # 生成 Writer 规则
    print("\n" + "-" * 40)
    print("生成的 Writer 风格规则:")
    print("-" * 40)
    rules = fp.to_writer_rules()
    print(rules)

    # 测试注入
    print("\n" + "-" * 40)
    print("注入效果（前500字）:")
    print("-" * 40)
    base = "# ROLE\n你是写作助手\n\n# RULES\n- 必须围绕核心观点展开"
    injected = inject_style_rules(base, fp)
    print(injected[:500])

    print("\n" + "=" * 60)
    print("自测完成")
    print("=" * 60)

"""Context Budgeting — Token Transformation Pipeline（五步）

Harness 标准组件。插在 Researcher 和 Planner 之间，
把 Researcher 的原始素材输出压缩到 token 预算以内。

五步：采集 → 排序 → 压缩 → 预算 → 拼装
"""

import math
import re
from state import State


# ——— 工具函数 ———


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文按字，英文按 ~4 字符/token）"""
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    non_chinese = len(text) - chinese_chars
    return chinese_chars + math.ceil(non_chinese / 4)


def _extract_title(content: str) -> str:
    """从 markdown 内容中提取标题"""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line.startswith("## "):
            return line[3:].strip()
    # fallback: 取第一行非空文本前 30 字
    for line in content.split("\n"):
        line = line.strip()
        if line and not line.startswith("```"):
            return line[:30]
    return "（无标题）"


def _keyword_density(content: str) -> float:
    """计算信息密度：去常见停用词后的独特内容比例"""
    stopwords = {
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
        "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
        "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "and", "but", "or", "not", "no", "this", "that", "it", "its",
    }
    words = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", content.lower())
    if not words:
        return 0.0
    unique = set(w for w in words if w not in stopwords and len(w) > 1)
    return len(unique) / len(words)


# ——— 五步主函数 ———


def context_budget(state: State, budget: int = 4000, keep_full: int = 5) -> State:
    """Token Transformation Pipeline：采集→排序→压缩→预算→拼装

    Args:
        state: 当前 State（researcher_agent 之后、planner_agent 之前）
        budget: Token 预算上限（默认 4000，给 Planner）
        keep_full: 保留全文的文件数（默认 5）

    Returns:
        State（research_notes 被替换为处理后的格式化文本）
    """
    raw = state.research_notes
    if not raw:
        state.research_notes = "（无素材）"
        return state

    # ——— 1. 采集：从 Researcher 输出中拆出素材块 ———
    blocks = [b.strip() for b in raw.split("\n### ") if b.strip()]
    if not blocks:
        blocks = [raw]  # 只有一个素材

    # 修复：第一个 block 可能没有 "### " 前缀
    if not blocks[0].startswith("###"):
        blocks[0] = "### " + blocks[0]

    class Candidate:
        def __init__(self, block: str):
            self.block = block
            self.content = block
            self.should_compress = False
            self.compressed = ""

    candidates = [Candidate(b) for b in blocks]

    # ——— 2. 排序：按相关度 × 信息密度 ———
    topic = state.progress
    scored = []
    for c in candidates:
        # 相关度：素材内容与主题的关键词重叠度
        topic_words = set(re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", topic.lower() if topic else ""))
        content_words = set(re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", c.content.lower()))
        relevance = len(topic_words & content_words) / max(len(topic_words), 1)

        density = _keyword_density(c.content)
        c.score = relevance * density
        scored.append(c)

    scored.sort(key=lambda x: x.score, reverse=True)

    # ——— 3. 压缩：前 keep_full 篇保留全文，其余只留标题 + 来源 ———
    for i, c in enumerate(scored):
        if i >= keep_full:
            c.should_compress = True
            title = _extract_title(c.content)
            # 取第一个来源标注行
            source_match = re.search(r"【来源】(.+?)[\n|]", c.content)
            source = source_match.group(1).strip() if source_match else ""
            c.compressed = f"### {title}\n来源：{source}（摘要，非全文）"

    # ——— 4. 预算：按 token 上限截断 ———
    used = 0
    kept = []
    for c in scored:
        text = c.compressed if c.should_compress else c.content
        tokens = _estimate_tokens(text)
        if used + tokens <= budget:
            kept.append(c)
            used += tokens
        else:
            break

    # ——— 5. 拼装：带序号和来源标注 ———
    parts = []
    for i, c in enumerate(kept):
        block = c.compressed if c.should_compress else c.content
        if c.should_compress:
            parts.append(f"【素材 {i + 1}/{len(kept)}】{block}")
        else:
            parts.append(f"【素材 {i + 1}/{len(kept)}】\n{block}")

    state.research_notes = (
        f"# 素材笔记（已压缩，token 预算 {budget}，实际 {used}）\n\n"
        + "\n\n".join(parts)
    )

    return state

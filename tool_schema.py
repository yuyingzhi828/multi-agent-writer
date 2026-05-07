"""
tool_schema.py —— 工具 Schema 层：给 Agent 开受控的信息口子

Agent 不直接调用外部 API。所有外部能力通过 Schema 定义的工具暴露。
Schema 定义了什么工具可用、参数约束、返回格式、调用次数上限。

设计原则：
1. 调用次数有上限（max_calls）——防止 Agent 死循环
2. 结果过滤（blocked_domains）——屏蔽低质量来源
3. 格式归一化（format_*）——Agent 不感知 API 差异
4. 错误隔离（try/except）——外部 API 故障不影响 Pipeline

用法：
    from tool_schema import call_tool, TOOL_SCHEMA, add_tool

    # 在 Researcher 里调用
    result = call_tool("search_web", {"query": "企业AI采用率 2025"}, call_count=1)
"""

import os
import json
import re
from typing import Optional


# ============================================================================
# Schema 定义
# ============================================================================

TOOL_SCHEMA = {
    "search_web": {
        "fn": "search_tavily",
        "description": "搜索互联网获取补充素材。仅在本地素材不足时使用，每次搜索消耗一次 API 额度。",
        "params": {
            "query": {
                "type": "str",
                "required": True,
                "max_len": 200,
                "description": "搜索关键词，优先使用中文+英文双语。例如：'企业 AI 采用率 enterprise AI adoption 2025'",
            },
            "max_results": {
                "type": "int",
                "required": False,
                "default": 5,
                "max": 10,
                "description": "返回结果条数，默认5条，最多10条",
            },
            "source": {
                "type": "str",
                "required": False,
                "default": "web",
                "enum": ["web", "news"],
                "description": "搜索来源：web=通用网页，news=新闻",
            },
        },
        "returns": "str: 格式化的搜索结果摘要（Markdown 格式，包含标题、摘要、来源URL）",
        "max_calls": 3,
        "blocked_domains": [
            "zhihu.com",
            "csdn.net",
        ],
    },
}


def add_tool(name: str, definition: dict):
    """注册新工具到 Schema"""
    required_keys = ["fn", "description", "params", "max_calls"]
    for k in required_keys:
        if k not in definition:
            raise ValueError(f"工具定义缺少必填字段: {k}")
    TOOL_SCHEMA[name] = definition
    print(f"[ToolSchema] 已注册工具: {name}")


# ============================================================================
# 实际 API 调用函数
# ============================================================================

def search_tavily(query: str, max_results: int = 5, source: str = "web") -> list[dict]:
    """调用 Tavily Search API"""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        # 尝试从 config.yaml 读取
        try:
            from pathlib import Path
            import yaml
            config_path = Path(__file__).parent / "config.yaml"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                api_key = cfg.get("tavily_api_key", "")
        except Exception:
            pass
    if not api_key:
        print("[ToolSchema] TAVILY_API_KEY 未设置，跳过搜索")
        return []

    try:
        import requests
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": False,
        }
        if source == "news":
            payload["topic"] = "news"

        resp = requests.post(
            "https://api.tavily.com/search",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as e:
        print(f"[ToolSchema] Tavily 搜索失败: {e}")
        return []


# ============================================================================
# 结果过滤与格式化
# ============================================================================

def filter_results(results: list[dict], schema_def: dict) -> list[dict]:
    """按 Schema 约束过滤结果：屏蔽低质量域名"""
    blocked = schema_def.get("blocked_domains", [])
    if not blocked:
        return results

    filtered = []
    for r in results:
        url = r.get("url", "")
        if any(domain in url for domain in blocked):
            print(f"[ToolSchema] 已过滤屏蔽域名: {url}")
            continue
        filtered.append(r)
    return filtered


def format_search_results(results: list[dict]) -> str:
    """将搜索结果格式化为 Researcher Agent 可读的素材摘要"""
    if not results:
        return "（搜索未返回结果）"

    lines = ["\n## 网络搜索补充素材\n"]
    for i, r in enumerate(results):
        title = r.get("title", "无标题")
        snippet = r.get("content") or r.get("snippet") or "无摘要"
        url = r.get("url", "无来源")

        # 截断过长的摘要（token 宝贵）
        if len(snippet) > 500:
            snippet = snippet[:500] + "..."

        lines.append(f"### 结果 {i + 1}")
        lines.append(f"- **标题**：{title}")
        lines.append(f"- **摘要**：{snippet}")
        lines.append(f"- **来源**：[{url}]({url})")
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# 统一调用入口
# ============================================================================

def call_tool(tool_name: str, params: dict, call_count: int) -> str:
    """工具调用的统一入口。

    Args:
        tool_name: 工具名称（必须在 TOOL_SCHEMA 中注册）
        params: 参数字典
        call_count: 当前已是第几次调用（用于检查次数上限）

    Returns:
        str: 格式化后的工具返回结果
    """
    schema_def = TOOL_SCHEMA.get(tool_name)
    if not schema_def:
        return f"（错误：未知工具 '{tool_name}'，可用工具：{list(TOOL_SCHEMA.keys())}）"

    # 检查调用次数
    if call_count > schema_def["max_calls"]:
        return (
            f"（已达到 '{tool_name}' 最大调用次数 {schema_def['max_calls']}，"
            f"不再发起请求。请基于已有素材继续。）"
        )

    fn_name = schema_def["fn"]

    if fn_name == "search_tavily":
        query = params.get("query", "")
        if not query:
            return "（错误：search_web 需要 query 参数）"
        max_results = min(params.get("max_results", 5), 10)
        source = params.get("source", "web")

        results = search_tavily(query, max_results, source)
        results = filter_results(results, schema_def)
        return format_search_results(results)

    return f"（错误：未知函数 '{fn_name}'）"


# ============================================================================
# Researcher 辅助：检测缺素材并提取搜索关键词
# ============================================================================

def extract_missing_keywords(research_notes: str) -> Optional[str]:
    """从 research_notes 中提取「缺素材：XXX」字段，返回搜索关键词"""
    match = re.search(r"缺素材[：:]\s*(.+?)(?:\n|$)", research_notes)
    if not match:
        return None

    keywords = match.group(1).strip()
    # 去掉括号里的注释（例如 "缺素材：企业AI案例（需要2024-2025年数据）"）
    keywords = re.sub(r"[（(].*?[）)]", "", keywords).strip()
    return keywords if keywords else None

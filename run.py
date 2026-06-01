import os
import re
import sys
import glob
from datetime import datetime
from pathlib import Path
from openai import OpenAI
try:
    import yaml
except ImportError:
    yaml = None
from state import State
from context_budget import context_budget
from traceability import Traceability
from recovery import recover, simple_retry, writer_recovery, reviewer_recovery
from tool_schema import call_tool, extract_missing_keywords

print("正在启动 9-Agent Harness（含 Recovery 恢复层 + ToolSchema 搜索层）...")

# ============ 加载 config.yaml ============
def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    defaults = {
        "materials_dir": str(Path(__file__).parent / "materials"),
        "outputs_dir": "outputs",
        "model": "deepseek-chat",
        "api_base": "https://api.deepseek.com",
    }
    if yaml and config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        defaults.update({k: v for k, v in loaded.items() if v is not None})
    return defaults

CONFIG = load_config()

# 命令行参数：python3 run.py "你的主题"
topic = sys.argv[1] if len(sys.argv) > 1 else None

api_key = os.getenv("DEEPSEEK_API_KEY") or CONFIG.get("deepseek_api_key", "")
if not api_key:
    raise ValueError("没有找到 DEEPSEEK_API_KEY，请在 config.yaml 或环境变量中设置")

client = OpenAI(api_key=api_key, base_url=CONFIG["api_base"])


# ============ 文件读写（仅 pipeline 首尾用）============
def read_file(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_file(path, content):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def parse_viewpoints(text: str) -> list[dict]:
    """把 state/viewpoints.md 的内容解析回 State.viewpoints"""
    if not text.strip():
        return []
    entries = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        # 格式: ## 写作记录 (2026-04-29 20:20)\n本次视角: ...\n切入点: ...\n核心论点: ...
        lines = block.split("\n")
        timestamp = ""
        log_parts = []
        for line in lines:
            if line.startswith("## 写作记录"):
                timestamp = line.replace("## 写作记录", "").strip(" ()")
            else:
                log_parts.append(line.strip())
        if timestamp and log_parts:
            entries.append({"timestamp": timestamp, "log": "；".join(log_parts)})
    return entries


def format_viewpoints(entries: list[dict]) -> str:
    """把 State.viewpoints 序列化回 state/viewpoints.md 格式"""
    if not entries:
        return ""
    blocks = []
    for vp in entries:
        blocks.append(f"## 写作记录 ({vp['timestamp']})\n{vp['log']}")
    return "\n\n".join(blocks) + "\n"


# ============ Agent 定义（签名统一：State -> State）============
base = read_file("instructions/base.md")
planner_rules = read_file("instructions/planner.md")
writer_rules = read_file("instructions/writer.md")
reviewer_rules = read_file("instructions/reviewer.md")
researcher_rules = read_file("instructions/researcher.md")


# ============ 素材库搜索 ============
MATERIALS_DIR = CONFIG["materials_dir"]


def fetch_url_text(url: str, timeout: int = 10) -> str:
    """抓取 URL 页面正文，返回纯文本。失败时返回空字符串。"""
    try:
        import urllib.request
        import html
        import re as _re
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        # 去掉 script/style 标签
        raw = _re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=_re.DOTALL | _re.IGNORECASE)
        raw = _re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=_re.DOTALL | _re.IGNORECASE)
        # 去掉所有 HTML 标签
        text = _re.sub(r'<[^>]+>', ' ', raw)
        text = html.unescape(text)
        # 合并多余空白
        text = _re.sub(r'\s+', ' ', text).strip()
        return text[:3000]  # 最多取前 3000 字
    except Exception as e:
        print(f"[fetch_url] 抓取失败 {url}: {e}")
        return ""


def search_materials(topic: str) -> str:
    """在本地素材库中搜索相关内容，同时抓取 config 里的 reference_urls。
    支持 .md / .txt 格式，由 config.materials_formats 控制。"""
    import glob
    results = []
    keywords = topic.lower().split()

    # 1. 本地素材库（支持多格式）
    formats = CONFIG.get("materials_formats", ["*.md"])
    all_files = []
    for fmt in formats:
        all_files.extend(glob.glob(f"{MATERIALS_DIR}/{fmt}", recursive=False))
        all_files.extend(glob.glob(f"{MATERIALS_DIR}/**/{fmt}", recursive=True))
    all_files = list(set(all_files))  # 去重

    for fpath in all_files:
        content = read_file(fpath)
        score = sum(1 for kw in keywords if kw in content.lower())
        if score > 0 or len(all_files) <= 3:
            preview = content[:500] + ("..." if len(content) > 500 else "")
            results.append(f"### [本地] {fpath}\n{preview}")

    if not all_files:
        results.append("（本地素材库为空）")

    # 2. reference_urls：从 config 抓取参考文章
    ref_urls = CONFIG.get("reference_urls") or []
    for url in ref_urls:
        print(f"[Materials] 抓取参考文章：{url}")
        text = fetch_url_text(url)
        if text:
            preview = text[:800] + ("..." if len(text) > 800 else "")
            results.append(f"### [URL] {url}\n{preview}")
        else:
            results.append(f"### [URL] {url}\n（抓取失败，跳过）")

    raw = "\n\n".join(results) if results else "（未找到任何素材）"
    return compact_materials(raw)


def compact_materials(text: str, limit: int = 3000, target_ratio: float = 0.8) -> str:
    """当原始素材超过 limit tokens 时，压缩到 limit * target_ratio。
    策略：按块切分，优先保留高分块，每块截取到最大允许长度。"""
    import math, re as _re

    def _tokens(s: str) -> int:
        cn = len(_re.findall(r'[\u4e00-\u9fff]', s))
        return cn + math.ceil((len(s) - cn) / 4)

    total = _tokens(text)
    if total <= limit:
        return text  # 不超过不处理

    target = int(limit * target_ratio) - 40  # 2400，预留尾部注释行开销
    blocks = [b.strip() for b in _re.split(r'\n(?=###)', text) if b.strip()]
    if not blocks:
        # 没有块结构，直接按字符截断
        return text[:target * 3] + "\n\n…（素材过长已截断）"

    # 每块平分 target tokens
    per_block = max(200, target // len(blocks))
    parts = []
    used = 0
    for block in blocks:
        if used >= target:
            break
        remaining = target - used  # 剩余可用 token
        bt = _tokens(block)
        allowance = min(per_block, remaining)  # 不超过剩余馉额
        if bt <= allowance:
            parts.append(block)
            used += bt
        else:
            # 截取到 allowance tokens 对应的字符数
            # 粗略估算：先按字符数二分查找，确保实际 tokens 不超过 allowance
            lo, hi = 0, len(block)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _tokens(block[:mid]) <= allowance:
                    lo = mid
                else:
                    hi = mid - 1
            parts.append(block[:lo] + "…（已截断）")
            used += _tokens(block[:lo])

    compact = "\n\n".join(parts)
    compact += f"\n\n（素材已压缩：原始 {total} tokens → 目标 {target} tokens，保留 {len(parts)}/{len(blocks)} 块）"
    return compact



def researcher_agent(state: State) -> State:
    """Researcher：搜索本地素材库，生成结构化素材笔记，写入 State.research_notes"""
    print("\n[Researcher] 正在搜索素材...\n")

    # 1. 本地素材库搜索
    local_materials = search_materials(state.progress)

    # 2. 让 LLM 整理素材
    system = f"{base}\n\n{researcher_rules}"
    user = f"""【当前主题】
{state.progress}

【本地素材库】
{local_materials}

请整理出一份素材笔记，每个素材标注：
- 【来源】文件名
- 【内容】关键摘录
- 【用途】可用于文章什么位置（开头/论据/案例/结尾）

如果素材不足，在笔记末尾标注"缺素材：XXX"。"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    state.research_notes = response.choices[0].message.content

    # 3. 本地素材不足 → 按 Tool Schema 调用搜索（最多 max_calls 次）
    search_calls = 0
    max_search_calls = 3
    while search_calls < max_search_calls:
        keywords = extract_missing_keywords(state.research_notes)
        if not keywords:
            break
        search_calls += 1
        print(f"\n[Researcher] 本地素材不足，调用搜索（第{search_calls}次）：{keywords}")
        search_result = call_tool(
            "search_web",
            {"query": keywords, "max_results": 5},
            call_count=search_calls,
        )
        state.research_notes += search_result
        # 再次让 LLM 基于扩充的素材重新整理
        user2 = f"""【当前主题】
{state.progress}

【全部素材（含本地+网络搜索）】
{state.research_notes}

素材已扩充。请重新整理一份完整的素材笔记。如果仍然不足，标注"缺素材：XXX"。"""
        response2 = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user2},
            ],
            temperature=0.3,
        )
        state.research_notes = response2.choices[0].message.content

    print(state.research_notes)
    return state


def planner_agent(state: State) -> State:
    """Planner：读 State.progress，输出策划结果，写入 State.planner_output"""
    print("\n[Planner] 正在生成策划...\n")

    system = f"{base}\n\n{planner_rules}"
    user = f"""【当前任务】
{state.progress}

【可用素材】
{state.research_notes}"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
    )
    state.planner_output = response.choices[0].message.content
    print(state.planner_output)
    return state


def writer_agent(state: State) -> State:
    """Writer：读 State.progress + State.planner_output + State.viewpoints，输出文章，更新 State"""
    print("\n[Writer] 正在写文章...\n")

    viewpoints_section = f"""
【已用视角（本次必须避开）】
{state.get_viewpoints_text()}
"""

    system = f"{base}\n\n{writer_rules}"
    user = f"""【当前任务】
{state.progress}

【Planner 给出的策划】
{state.planner_output}
{viewpoints_section}

写完文章后，必须在最后输出视角日志，格式严格如下：

[VIEWPOINT_LOG]
本次视角：（一句话描述这次用的视角）
切入点：（一句话描述从哪里切入）
核心论点：（一句话描述核心主张）
[/VIEWPOINT_LOG]
"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
    )
    state.writer_output = response.choices[0].message.content
    print(state.writer_output)

    # State: 从输出中提取 VIEWPOINT_LOG 并追加到 State
    match = re.search(
        r"\[VIEWPOINT_LOG\](.*?)\[/VIEWPOINT_LOG\]", state.writer_output, re.DOTALL
    )
    if match:
        log = match.group(1).strip()
        state.add_viewpoint(log)
        print(f"\n[State] 已追加视角记录（当前共 {len(state.viewpoints)} 条）")
    else:
        print(f"\n[State] ⚠️ 未捕获到 VIEWPOINT_LOG")

    return state


def reviewer_agent(state: State) -> State:
    """Reviewer：读 State.writer_output，对照 base.md 规则检查质量，写入 State.review_passed / State.review_notes"""
    print("\n[Reviewer] 正在审核...\n")

    # 干净上下文：不给 Reviewer 看 Planner 策划，只看文章成品 + 规则
    system = f"{base}\n\n{reviewer_rules}"
    user = f"""【待审核文章】
{state.writer_output}

【质量规则】
{base}

请逐条检查：
1. 核心观点是否在前三段出现？
2. 每段是否超过 150 字？
3. 有没有空洞总结？
4. 技术概念是否解释了？
5. 视角是否与已用视角重复？
   已用视角：
   {state.get_viewpoints_text()}

审核完成后，在最后输出：
[REVIEW_RESULT]
通过：是/否
问题：（如不通过，列出具体问题）
[/REVIEW_RESULT]
"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,  # 审核不需要创意，要一致性
    )
    state.review_notes = response.choices[0].message.content
    print(state.review_notes)

    # 解析审核结果
    match = re.search(r"通过：(是|否)", state.review_notes)
    state.review_passed = bool(match and match.group(1) == "是")

    if state.review_passed:
        print("\n[Reviewer] ✅ 审核通过")
    else:
        print("\n[Reviewer] ❌ 不通过，需要修改")

    return state


# ============ Pipeline ============
# 1. 从文件加载初始 State
state = State(
    progress=topic or read_file("instructions/state/progress.md"),
    viewpoints=parse_viewpoints(read_file("state/viewpoints.md")),
)

# 启用可追溯层

tracer = Traceability()
_traced = lambda agent, name: lambda s: tracer.wrap(agent, name, s)

# Recovery 包裹 Traceability 包裹 Agent
# 前四个 Agent 用简单指数退避重试
state = recover(simple_retry, _traced(researcher_agent, "Researcher"), "Researcher", state, base_delay=2)
state = recover(simple_retry, _traced(context_budget, "ContextBudgeting"), "ContextBudgeting", state, base_delay=2)
state = recover(simple_retry, _traced(planner_agent, "Planner"), "Planner", state, base_delay=2)

# Writer 用专用恢复：重试→降级→回退→升人
state = recover(writer_recovery, _traced(writer_agent, "Writer"), "Writer", state)

# Reviewer 用专用恢复：连续驳回 3 次触发回退
state = recover(reviewer_recovery, _traced(reviewer_agent, "Reviewer"), "Reviewer", state)

# 4. 不通过则退回重写（最多 2 次）
retry = 0
while not state.review_passed and retry < 2:
    retry += 1
    print(f"\n=== 退回重写（第 {retry} 次）===")
    state = recover(writer_recovery, _traced(writer_agent, "Writer"), "Writer", state)
    state = recover(reviewer_recovery, _traced(reviewer_agent, "Reviewer"), "Reviewer", state)

# 5. 持久化 State 到文件
save_file("state/viewpoints.md", format_viewpoints(state.viewpoints))

# 6. 成品落盘（版本号自动递增）
def save_output_versioned(content: str, topic: str) -> str:
    """成品落盘：outputs/{topic}_v1.md / v2.md ...，文章开头注入版本信息。"""
    outputs_dir = Path(CONFIG["outputs_dir"])
    outputs_dir.mkdir(parents=True, exist_ok=True)
    # 清理 topic 用于文件名
    safe_topic = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', topic)[:30].strip('_')
    # 找下一个版本号
    existing = sorted(outputs_dir.glob(f"{safe_topic}_v*.md"))
    next_v = len(existing) + 1
    filename = f"{safe_topic}_v{next_v}.md"
    filepath = outputs_dir / filename
    # 文章开头注入版本信息
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"<!-- 版本 v{next_v} | 生成时间 {now} | 审核：{'通过' if state.review_passed else '未通过'} -->\n\n"
    save_file(str(filepath), header + content)
    return str(filepath)

if state.writer_output:
    output_path = save_output_versioned(state.writer_output, topic or "article")
    print(f"[成品落盘] ✅ {output_path}")

# 7. 输出追溯报告
tracer.print_report()
print("\n=== 运行完成 ===")
print(f"[State] 审核结果: {'通过' if state.review_passed else '未通过（已达最大重试次数）'}")
print(f"[State] 已持久化 {len(state.viewpoints)} 条视角记录到 state/viewpoints.md")

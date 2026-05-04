import os
import re
from datetime import datetime
from openai import OpenAI
from state import State
from context_budget import context_budget

print("正在启动双Agent Harness...")

api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise ValueError("没有找到 DEEPSEEK_API_KEY,请先设置 API Key")

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


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
MATERIALS_DIR = "/Users/yuyingzhi/Documents/Knowledge Repository"


def search_materials(topic: str) -> str:
    """在本地素材库中搜索与主题相关的 markdown 文件。
    关键词匹配后返回文件摘要；素材库少时全量返回。"""
    import glob
    md_files = glob.glob(f"{MATERIALS_DIR}/**/*.md", recursive=True)
    if not md_files:
        return "（素材库为空，没有可用的本地素材）"

    results = []
    keywords = topic.lower().split()
    for fpath in md_files:
        content = read_file(fpath)
        score = sum(1 for kw in keywords if kw in content.lower())
        if score > 0 or len(md_files) <= 3:  # 文件少时全量返回
            preview = content[:500] + ("..." if len(content) > 500 else "")
            results.append(f"### {fpath}\n{preview}")

    return "\n\n".join(results) if results else "（未找到与主题匹配的素材）"



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
    progress=read_file("instructions/state/progress.md"),
    viewpoints=parse_viewpoints(read_file("state/viewpoints.md")),
)

# 2. Researcher 搜索素材
state = researcher_agent(state)

# 2.5. Context Budgeting：采集→排序→压缩→预算→拼装（Planner 只分 4000 token）
state = context_budget(state, budget=4000)

# 3. Planner → Writer（State 显式传递）
state = planner_agent(state)
state = writer_agent(state)

# 3. Reviewer 审核
state = reviewer_agent(state)

# 4. 不通过则退回重写（最多 2 次）
retry = 0
while not state.review_passed and retry < 2:
    retry += 1
    print(f"\n=== 退回重写（第 {retry} 次）===")
    state = writer_agent(state)
    state = reviewer_agent(state)

# 5. 持久化 State 到文件
save_file("state/viewpoints.md", format_viewpoints(state.viewpoints))
print("\n=== 运行完成 ===")
print(f"[State] 审核结果: {'通过' if state.review_passed else '未通过（已达最大重试次数）'}")
print(f"[State] 已持久化 {len(state.viewpoints)} 条视角记录到 state/viewpoints.md")

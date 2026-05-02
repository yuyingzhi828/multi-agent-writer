import os
import re
from datetime import datetime
from openai import OpenAI
from state import State

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


def planner_agent(state: State) -> State:
    """Planner：读 State.progress，输出策划结果，写入 State.planner_output"""
    print("\n[Planner] 正在生成策划...\n")

    system = f"{base}\n\n{planner_rules}"
    user = f"【当前任务】\n{state.progress}"

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


# ============ Pipeline ============
# 1. 从文件加载初始 State
state = State(
    progress=read_file("instructions/state/progress.md"),
    viewpoints=parse_viewpoints(read_file("state/viewpoints.md")),
)

# 2. Planner → Writer（State 显式传递）
state = planner_agent(state)
state = writer_agent(state)

# 3. 持久化 State 到文件
save_file("state/viewpoints.md", format_viewpoints(state.viewpoints))
print("\n=== 双Agent运行完成 ===")
print(f"[State] 已持久化 {len(state.viewpoints)} 条视角记录到 state/viewpoints.md")

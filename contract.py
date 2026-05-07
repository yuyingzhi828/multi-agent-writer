"""
contract.py —— Agent 输入输出契约显式化

每个 Agent 的 Contract 定义：
- 输入字段：必填、最小长度、关键词要求
- 输出字段：必填、长度范围、关键词要求
- validate_input() / validate_output() 运行时校验

集成到 run.py：
    from contract import with_contract, RESEARCHER_CONTRACT, PLANNER_CONTRACT, WRITER_CONTRACT, REVIEWER_CONTRACT

    # 在 Pipeline 调用时包裹 Agent：
    state = with_contract(researcher_agent, RESEARCHER_CONTRACT, state)
    state = with_contract(planner_agent,    PLANNER_CONTRACT,    state)
    state = with_contract(writer_agent,     WRITER_CONTRACT,     state)
    state = with_contract(reviewer_agent,   REVIEWER_CONTRACT,   state)

Harness 系列第 12 篇：Contract 显式化
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ============ 数据结构 ============

@dataclass
class FieldSpec:
    """单个字段的契约规格。

    Attributes:
        required:      字段是否必须有值（True = 空值直接报错）
        min_length:    字符串最小长度，0 表示不检查
        max_length:    字符串最大长度，0 表示不检查
        must_contain:  字段值必须包含的关键词列表
        description:   人类可读的字段说明（文档用）
    """
    required: bool = True
    min_length: int = 0
    max_length: int = 0
    must_contain: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class AgentContract:
    """一个 Agent 的完整契约：输入规格 + 输出规格。

    inputs:  Agent 运行前，State 里必须满足的字段约束
    outputs: Agent 运行后，State 里必须满足的字段约束

    契约用途：
    1. 文档：声明依赖关系，不靠读代码推断
    2. 运行时校验：出错立刻报，不靠 Agent 自我感觉良好
    3. 测试边界：只需构造满足 inputs 的 State，即可单独测某个 Agent
    """
    agent_name: str
    inputs: dict[str, FieldSpec] = field(default_factory=dict)
    outputs: dict[str, FieldSpec] = field(default_factory=dict)


# ============ 异常 ============

class ContractViolation(Exception):
    """契约违反：输入或输出不满足声明的约定。

    输入违反：上游 Agent 的产出不合格，不应该继续执行当前 Agent。
    输出违反：当前 Agent 的产出不合格，触发 Recovery 重试。
    """
    pass


# ============ 校验逻辑 ============

def _get_field_value(state: Any, field_name: str) -> Any:
    """从 State 对象读取字段值。字段不存在时返回 None。"""
    return getattr(state, field_name, None)


def _check_field(field_name: str, spec: FieldSpec, value: Any) -> list[str]:
    """对单个字段做所有校验，返回错误信息列表（空列表 = 通过）。"""
    errors = []

    # 1. 必填检查
    if spec.required and (value is None or value == ""):
        errors.append(f"  字段 '{field_name}' 必填但为空 | 说明：{spec.description}")
        return errors  # 空值不再做后续检查

    if value is None:
        return errors  # 非必填字段为空，跳过

    str_value = str(value)

    # 2. 最小长度
    if spec.min_length > 0 and len(str_value) < spec.min_length:
        errors.append(
            f"  字段 '{field_name}' 长度 {len(str_value)} 小于要求的 {spec.min_length}"
            f" | 说明：{spec.description}"
        )

    # 3. 最大长度
    if spec.max_length > 0 and len(str_value) > spec.max_length:
        errors.append(
            f"  字段 '{field_name}' 长度 {len(str_value)} 超过上限 {spec.max_length}"
            f" | 说明：{spec.description}"
        )

    # 4. 关键词检查
    for kw in spec.must_contain:
        if kw not in str_value:
            errors.append(
                f"  字段 '{field_name}' 缺少必要内容：'{kw}'"
                f" | 说明：{spec.description}"
            )

    return errors


def validate_input(contract: AgentContract, state: Any) -> None:
    """检查 State 是否满足 Agent 的输入契约。

    不满足则抛 ContractViolation。
    调用方应在执行 Agent 前调用此方法。

    输入违反 = 上游 Agent 产出不合格，当前 Agent 不应继续执行。
    Pipeline 应终止并报告是哪个上游 Agent 违反了输出契约。
    """
    errors = []
    for field_name, spec in contract.inputs.items():
        value = _get_field_value(state, field_name)
        errors.extend(_check_field(field_name, spec, value))

    if errors:
        raise ContractViolation(
            f"\n[Contract] ❌ {contract.agent_name} 输入契约违反"
            f"（检查上游 Agent 的输出是否合格）：\n"
            + "\n".join(errors)
        )

    print(f"[Contract] ✅ {contract.agent_name} 输入校验通过")


def validate_output(contract: AgentContract, state: Any) -> None:
    """检查 Agent 运行后 State 是否满足输出契约。

    不满足则抛 ContractViolation。
    调用方应在 Agent 执行完毕后调用此方法。

    输出违反 = 当前 Agent 产出不合格，应触发 Recovery 重试。
    """
    errors = []
    for field_name, spec in contract.outputs.items():
        value = _get_field_value(state, field_name)
        errors.extend(_check_field(field_name, spec, value))

    if errors:
        raise ContractViolation(
            f"\n[Contract] ❌ {contract.agent_name} 输出契约违反"
            f"（当前 Agent 产出不合格，触发 Recovery）：\n"
            + "\n".join(errors)
        )

    print(f"[Contract] ✅ {contract.agent_name} 输出校验通过")


# ============ 包装器：集成到 Pipeline ============

def with_contract(
    agent_fn: Callable,
    contract: AgentContract,
    state: Any,
) -> Any:
    """Contract 包装器：运行前校验输入，运行后校验输出。

    包裹顺序（由外到里）：
        recover → traced → with_contract → agent_fn

    输入违反：直接抛 ContractViolation，不触发 Recovery（上游的锅）。
    输出违反：把 ContractViolation 包成 RuntimeError 抛出，触发 Recovery 重试。

    用法示例：
        state = with_contract(researcher_agent, RESEARCHER_CONTRACT, state)

    与 recover / traced 组合使用：
        state = recover(
            simple_retry,
            _traced(
                lambda s: with_contract(writer_agent, WRITER_CONTRACT, s),
                "Writer"
            ),
            "Writer",
            state,
        )
    """
    # 前置校验（输入不合格 → ContractViolation，不走 Recovery）
    validate_input(contract, state)

    # 执行 Agent
    state = agent_fn(state)

    # 后置校验（输出不合格 → RuntimeError，触发 Recovery 重试）
    try:
        validate_output(contract, state)
    except ContractViolation as e:
        raise RuntimeError(
            f"[Contract] {contract.agent_name} 输出不合格，触发 Recovery 重试：{e}"
        ) from e

    return state


# ============ 各 Agent 的 Contract 定义 ============
# 对应 multi-agent-writer/run.py 里的 Agent 签名：State -> State

RESEARCHER_CONTRACT = AgentContract(
    agent_name="Researcher",
    inputs={
        "progress": FieldSpec(
            required=True,
            min_length=10,
            description="当前写作任务描述，至少10字，供 Researcher 检索素材方向",
        ),
    },
    outputs={
        "research_notes": FieldSpec(
            required=True,
            min_length=100,
            description=(
                "素材笔记，至少100字。"
                "本地素材库 + 网络搜索结果的整合，"
                "Planner 依赖此字段策划文章结构"
            ),
        ),
    },
)

PLANNER_CONTRACT = AgentContract(
    agent_name="Planner",
    inputs={
        "progress": FieldSpec(
            required=True,
            min_length=10,
            description="当前写作任务描述",
        ),
        "research_notes": FieldSpec(
            required=True,
            min_length=50,
            description=(
                "Researcher 整理好的素材笔记。"
                "Planner 依赖这份笔记做策划，不能为空或过短"
            ),
        ),
    },
    outputs={
        "planner_output": FieldSpec(
            required=True,
            min_length=100,
            must_contain=["视角", "切入点"],
            description=(
                "策划结果，至少100字。"
                "必须包含「视角」和「切入点」关键字——"
                "Writer 依赖这两个关键字定位策划意图"
            ),
        ),
    },
)

WRITER_CONTRACT = AgentContract(
    agent_name="Writer",
    inputs={
        "progress": FieldSpec(
            required=True,
            min_length=10,
            description="当前写作任务描述",
        ),
        "planner_output": FieldSpec(
            required=True,
            min_length=100,
            must_contain=["视角", "切入点"],
            description=(
                "Planner 的策划结果。"
                "Writer 基于此写作，策划不完整会导致文章结构散乱"
            ),
        ),
    },
    outputs={
        "writer_output": FieldSpec(
            required=True,
            min_length=500,
            description=(
                "文章正文，至少500字。"
                "Reviewer 依赖此字段做质量审核"
            ),
        ),
    },
)

REVIEWER_CONTRACT = AgentContract(
    agent_name="Reviewer",
    inputs={
        "writer_output": FieldSpec(
            required=True,
            min_length=500,
            description="Writer 产出的文章正文，至少500字才有审核价值",
        ),
    },
    outputs={
        "review_passed": FieldSpec(
            required=True,
            description="审核结果，True 表示通过，False 表示不通过",
        ),
        "review_notes": FieldSpec(
            required=True,
            min_length=20,
            description=(
                "审核意见，至少20字。"
                "通过时说明通过理由；不通过时列出具体问题，"
                "Writer 根据此意见修改"
            ),
        ),
    },
)


# ============ run.py 集成示例 ============
# 下面展示如何把 with_contract 与已有的 recover / traced 组合。
# 实际运行时，把这段代码放进 run.py 的 Pipeline 部分替换原有调用即可。

INTEGRATION_EXAMPLE = """
# === 集成到 run.py 的示例 ===

from contract import (
    with_contract, ContractViolation,
    RESEARCHER_CONTRACT, PLANNER_CONTRACT,
    WRITER_CONTRACT, REVIEWER_CONTRACT,
)

# 包裹顺序：recover → traced → with_contract → agent
# with_contract 放在 traced 内层：这样 Traceability 能记录完整耗时（含校验）

state = recover(
    simple_retry,
    _traced(
        lambda s: with_contract(researcher_agent, RESEARCHER_CONTRACT, s),
        "Researcher",
    ),
    "Researcher",
    state,
    base_delay=2,
)

state = recover(
    simple_retry,
    _traced(
        lambda s: with_contract(planner_agent, PLANNER_CONTRACT, s),
        "Planner",
    ),
    "Planner",
    state,
    base_delay=2,
)

state = recover(
    writer_recovery,
    _traced(
        lambda s: with_contract(writer_agent, WRITER_CONTRACT, s),
        "Writer",
    ),
    "Writer",
    state,
)

state = recover(
    reviewer_recovery,
    _traced(
        lambda s: with_contract(reviewer_agent, REVIEWER_CONTRACT, s),
        "Reviewer",
    ),
    "Reviewer",
    state,
)

# 输入契约违反（ContractViolation）不会被 recover 重试，会直接往上抛。
# 在最外层 try/except 捕获：
try:
    # ... Pipeline 调用 ...
    pass
except ContractViolation as e:
    print(f"[Pipeline] 契约违反，Pipeline 终止：{e}")
    raise
"""


# ============ 简单自测 ============

if __name__ == "__main__":
    """直接运行此文件可做简单冒烟测试，不依赖完整 Pipeline。"""

    # 构造一个最小的 State mock，不需要导入真实的 state.py
    class MockState:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    print("=" * 50)
    print("Contract 自测")
    print("=" * 50)

    # --- 测试 1：正常路径，全部通过 ---
    print("\n[测试1] 正常路径 - Planner 输入输出均合格")
    s = MockState(
        progress="写一篇关于 Contract 显式化的文章，探讨 Agent 之间的隐式耦合问题",
        research_notes="契约式编程（Design by Contract）由 Bertrand Meyer 在 Eiffel 语言中提出。"
                       "核心思想是：前置条件 + 后置条件 + 不变式。"
                       "在 Agent 系统中，Contract 定义了 Agent 之间的接口约定，"
                       "避免隐式耦合，出错时精准定位违反方。",
    )
    validate_input(PLANNER_CONTRACT, s)

    s.planner_output = (
        "视角：从排查成本切入，讲 Contract 的价值\n"
        "切入点：一次隐式耦合导致排查一整天的真实经历\n"
        "论点1：没有契约时问题怎么藏着\n"
        "论点2：Contract 三个维度\n"
        "论点3：集成到 run.py 的方式"
    )
    validate_output(PLANNER_CONTRACT, s)

    # --- 测试 2：输入违反（必填字段为空）---
    print("\n[测试2] 输入违反 - research_notes 为空")
    s2 = MockState(
        progress="写一篇关于多 Agent 系统的文章",
        research_notes="",  # 空！
    )
    try:
        validate_input(PLANNER_CONTRACT, s2)
        print("  ⚠️  预期应该报错，但没有")
    except ContractViolation as e:
        print(f"  ✅ 正确捕获契约违反：{e}")

    # --- 测试 3：输出违反（缺少关键词）---
    print("\n[测试3] 输出违反 - planner_output 缺少「切入点」")
    s3 = MockState(
        progress="写一篇关于 Contract 的文章",
        research_notes="x" * 100,
    )
    validate_input(PLANNER_CONTRACT, s3)

    s3.planner_output = (
        "视角：从系统设计角度讲 Contract 的价值\n"
        "论点1：接口显式化\n"
        "论点2：运行时校验\n"
        "这里没有「切入点」这个词"
    )
    try:
        validate_output(PLANNER_CONTRACT, s3)
        print("  ⚠️  预期应该报错，但没有")
    except ContractViolation as e:
        print(f"  ✅ 正确捕获契约违反：{e}")

    # --- 测试 4：with_contract 包装器 ---
    print("\n[测试4] with_contract 包装器 - 输出违反触发 RuntimeError")

    def mock_planner(state):
        state.planner_output = "太短了"  # 不满足 min_length=100
        return state

    s4 = MockState(
        progress="写一篇关于 Contract 的文章，探讨 Agent 之间的接口约定设计",
        research_notes="x" * 100,
    )
    try:
        with_contract(mock_planner, PLANNER_CONTRACT, s4)
        print("  ⚠️  预期应该报错，但没有")
    except RuntimeError as e:
        print(f"  ✅ 输出违反被包成 RuntimeError（触发 Recovery）：{str(e)[:80]}...")
    except ContractViolation as e:
        print(f"  ⚠️  输出违反直接抛了 ContractViolation，应包成 RuntimeError：{e}")

    print("\n" + "=" * 50)
    print("自测完成")
    print("=" * 50)

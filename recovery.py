#!/usr/bin/env python3
"""Recovery layer — Harness 六层治理最后一层：自动恢复与容错。

四种恢复策略：
1. 指数退避重试（API 超时、网络抖动）
2. 降级到备用 Agent（高配模型故障时切低配）
3. State 快照回退（Reviewer 连续驳回时回溯起点）
4. 人工升级（不可自动恢复的错误，通知人来处理）

用法：
    from recovery import recover, simple_retry, writer_recovery, reviewer_recovery

    state = recover(simple_retry, researcher_agent, "Researcher", state)
    state = recover(writer_recovery, writer_agent, "Writer", state)
    state = recover(reviewer_recovery, reviewer_agent, "Reviewer", state)
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# 日志目录
# ---------------------------------------------------------------------------
LOGS_DIR = Path(__file__).parent / "recovery_logs"
LOGS_DIR.mkdir(exist_ok=True)

RECOVERY_LOG = LOGS_DIR / "recovery.jsonl"


def _log_recovery(agent: str, attempt: int, strategy: str, error: str, outcome: str) -> None:
    """写一条恢复日志。"""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "agent": agent,
        "attempt": attempt,
        "strategy": strategy,
        "error": str(error)[:200],
        "outcome": outcome,
    }
    with open(RECOVERY_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 恢复策略
# ---------------------------------------------------------------------------

def simple_retry(
    agent_fn: Callable,
    agent_name: str,
    state: Any,
    max_retries: int = 4,
    base_delay: float = 2.0,
    **_,
) -> Any:
    """策略一：指数退避重试。

    适用场景：API 超时、网络抖动、临时限流。
    第 4 次失败后放弃，记录日志并抛出异常。
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = agent_fn(state)
            if attempt > 1:
                _log_recovery(agent_name, attempt, "exponential_backoff", "", "recovered")
            return result
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay ** attempt  # 2, 4, 8
                time.sleep(delay)
            else:
                _log_recovery(agent_name, attempt, "exponential_backoff", str(e), "exhausted")
                raise


def fallback_retry(
    agent_fn: Callable,
    agent_name: str,
    state: Any,
    *,
    fallback_fn: Callable,
    max_retries: int = 2,
    **__,
) -> Any:
    """策略二：重试失败后降级到备用 Agent。

    适用场景：Writer 高配模型故障，切到低配继续产出。
    降级产出打 [RECOVERY_MODE] 标记。
    """
    for attempt in range(1, max_retries + 1):
        try:
            result = agent_fn(state)
            if attempt > 1:
                _log_recovery(agent_name, attempt, "retry_before_fallback", "", "recovered")
            return result
        except Exception as e:
            if attempt == max_retries:
                _log_recovery(
                    agent_name, attempt, "fallback_to_backup", str(e), "fallback"
                )
                result = fallback_fn(state)
                if hasattr(result, "recovery_mode"):
                    result.recovery_mode = True
                return result
            time.sleep(2)


def state_rollback(
    agent_fn: Callable,
    agent_name: str,
    state: Any,
    snapshot_dir: Path = Path("traces"),
    rollback_tag: str = "state_v3",
    **__,
) -> Any:
    """策略三：State 快照回退。

    适用场景：Reviewer 连续驳回，回退到 Planner 之后的状态重新执行。
    要求 Traceability 层已启用（有 .json 快照）。
    """
    snapshot_path = snapshot_dir / f"{rollback_tag}.json"
    if not snapshot_path.exists():
        _log_recovery(agent_name, 1, "rollback_missing_snapshot", f"no {rollback_tag}", "skipped")
        return agent_fn(state)

    with open(snapshot_path) as f:
        rolled_back = json.load(f)

    _log_recovery(agent_name, 1, "state_rollback", f"restored {rollback_tag}", "rollback")
    return agent_fn(rolled_back)


def human_escalation(
    agent_name: str,
    error: Exception,
    state: Any,
    reason: str = "unknown",
) -> dict:
    """策略四：人工升级。

    适用场景：任意 Agent 重试 3 次全部失败、产出质量可疑、触及敏感内容。
    生成错误报告，暂停 Pipeline，等待人工介入。
    """
    escalation_report = {
        "timestamp": datetime.now().isoformat(),
        "agent": agent_name,
        "reason": reason,
        "error": str(error)[:500],
        "last_state_snapshot": str(state)[:1000],
        "action_required": "manual_review",
    }
    report_path = LOGS_DIR / f"escalation_{agent_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w") as f:
        json.dump(escalation_report, f, ensure_ascii=False, indent=2)

    _log_recovery(agent_name, 99, "human_escalation", str(error)[:200], "escalated")
    return escalation_report


# ---------------------------------------------------------------------------
# 复合恢复策略（面向特定 Agent 角色）
# ---------------------------------------------------------------------------

def writer_recovery(
    agent_fn: Callable,
    agent_name: str,
    state: Any,
    fallback_fn: Optional[Callable] = None,
    **kwargs,
) -> Any:
    """Writer 专用恢复：指数退避 → 降级备用 → State 回退 → 人工升级。"""
    # 第 1-3 次：指数退避
    for attempt in range(1, 4):
        try:
            result = agent_fn(state)
            return result
        except Exception as e:
            delay = 2 ** attempt
            if attempt < 3:
                time.sleep(delay)
            else:
                # 第 3 次还失败，尝试降级
                if fallback_fn:
                    _log_recovery(agent_name, attempt, "fallback_after_retry", str(e), "fallback")
                    try:
                        result = fallback_fn(state)
                        if hasattr(result, "recovery_mode"):
                            result.recovery_mode = True
                        return result
                    except Exception as fe:
                        return human_escalation(agent_name, fe, state, "fallback_also_failed")

                return human_escalation(agent_name, e, state, "max_retries_exhausted")


def reviewer_recovery(
    agent_fn: Callable,
    agent_name: str,
    state: Any,
    max_rejections: int = 3,
    snapshot_dir: Path = Path("traces"),
    **kwargs,
) -> Any:
    """Reviewer 专用恢复：连续驳回超过阈值 → State 回退。

    驳回计数由 run.py 维护在 state.review_reject_count。
    """
    result = agent_fn(state)

    if not getattr(result, "review_passed", True):
        result.review_reject_count = getattr(state, "review_reject_count", 0) + 1
        if result.review_reject_count >= max_rejections:
            _log_recovery(
                agent_name,
                result.review_reject_count,
                "reviewer_reject_threshold",
                f"rejected {result.review_reject_count} times",
                "rollback",
            )
            result._rollback = True

    return result


# ---------------------------------------------------------------------------
# 顶层包装
# ---------------------------------------------------------------------------

def recover(
    strategy_fn: Callable,
    agent_fn: Callable,
    agent_name: str,
    state: Any,
    **kwargs,
) -> Any:
    """通用 Recovery 包装器。

    参数：
        strategy_fn: 恢复策略函数（simple_retry / writer_recovery / reviewer_recovery / ...）
        agent_fn:    被包裹的 Agent 函数
        agent_name:  Agent 名称（日志用）
        state:       State 对象
        **kwargs:    传给恢复策略的额外参数（如 fallback_fn、max_rejections）
    """
    return strategy_fn(agent_fn, agent_name, state, **kwargs)


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    class FakeState:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    # 测试一：simpleretry 正常通过
    print("=== Test 1: simple_retry (no error) ===")
    def okay_agent(state):
        state.ok = True
        return state
    s = FakeState()
    s = recover(simple_retry, okay_agent, "TestAgent", s)
    assert s.ok
    print("  PASS")

    # 测试二：指数退避 + 最终抛出
    print("=== Test 2: simple_retry (always fails) ===")
    call_count = [0]
    def broken_agent(state):
        call_count[0] += 1
        raise RuntimeError("boom")
    s = FakeState()
    try:
        recover(simple_retry, broken_agent, "BrokenAgent", s)
    except RuntimeError:
        pass
    assert call_count[0] == 4  # 4 次重试
    print(f"  PASS (retried {call_count[0]} times)")

    # 测试三：降级恢复
    print("=== Test 3: fallback_retry ===")
    def doomed_agent(state):
        raise RuntimeError("down")
    def backup_agent(state):
        state.via_fallback = True
        return state
    s = FakeState()
    s = recover(fallback_retry, doomed_agent, "Doomed", s, fallback_fn=backup_agent, max_retries=2)
    assert s.via_fallback
    print("  PASS")

    # 测试四：Reviewer 连续驳回
    print("=== Test 4: reviewer_recovery (reject threshold) ===")
    def strict_reviewer(state):
        state.review_passed = False
        return state
    s = FakeState(review_reject_count=2)  # 已经被驳回过 2 次
    s = recover(reviewer_recovery, strict_reviewer, "Reviewer", s)
    assert s.review_reject_count == 3
    assert s._rollback is True
    print("  PASS")

    print(f"\n✅ All 4 tests passed. Recovery log: {RECOVERY_LOG}")

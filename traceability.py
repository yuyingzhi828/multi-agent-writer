"""
Traceability 层 — 包裹每个 Agent，记录输入/输出/决策 + State 快照。

用法：
    from traceability import Traceability

    tracer = Traceability()

    # 用 tracer.wrap 替代原来的 agent 调用
    state = tracer.wrap(researcher_agent, "Researcher", state)
    state = tracer.wrap(context_budget, "ContextBudgeting", state, budget=4000)
    state = tracer.wrap(planner_agent, "Planner", state)
    state = tracer.wrap(writer_agent, "Writer", state)
    state = tracer.wrap(reviewer_agent, "Reviewer", state)

    # 查看决策日志
    tracer.print_report()
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

TZ = timezone(timedelta(hours=8))


class Traceability:
    """Agent 决策 + State 演化三合一追溯系统。"""

    def __init__(self, output_dir: str = "traces"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.session_id = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        self.decisions: list[dict] = []
        self.state_snapshots: list[str] = []
        self.step = 0

    def wrap(
        self,
        agent_fn: Callable,
        name: str,
        state: Any,
        *args,
        **kwargs,
    ) -> Any:
        """包裹一个 Agent 调用，自动记录决策痕迹 + State 快照。"""
        self.step += 1

        # ── 保存输入快照 ──
        state_before = self._extract_state(state)
        self._save_snapshot(f"{self.step:02d}_{name}_before", state_before)

        # ── 执行 ──
        result = agent_fn(state, *args, **kwargs) if args or kwargs else agent_fn(state)

        # ── 保存输出快照 ──
        state_after = self._extract_state(result or state)
        self._save_snapshot(f"{self.step:02d}_{name}_after", state_after)

        # ── 记录决策 ──
        changed = {
            k: v
            for k, v in state_after.items()
            if k in state_before and state_before.get(k) != v
        }
        new_fields = [k for k in state_after if k not in state_before]

        decision = {
            "step": self.step,
            "agent": name,
            "timestamp": datetime.now(TZ).isoformat(),
            "input_summary": self._summarize(state_before),
            "output_summary": self._summarize(state_after),
            "changed_fields": list(changed.keys()),
            "new_fields": new_fields,
            "trace_file": f"{self.step:02d}_{name}",
        }
        self.decisions.append(decision)
        self._write_trace_log(decision)

        return result or state

    def print_report(self):
        """打印本次运行的完整追溯报告。"""
        print(f"\n{'='*60}")
        print(f"  Traceability Report — session {self.session_id}")
        print(f"  {len(self.decisions)} agent calls traced")
        print(f"  {len(self.state_snapshots)} state snapshots saved")
        print(f"{'='*60}")
        for d in self.decisions:
            print(f"\n  [{d['step']}] {d['agent']}")
            print(f"      ↳ changed: {d['changed_fields'] or '(none)'}")
            print(f"      ↳ new:     {d['new_fields'] or '(none)'}")
            print(f"      ↳ trace:   traces/{d['trace_file']}_*.json")
        print(f"\n  Full traces: {self.output_dir}/session_{self.session_id}/")
        print(f"{'='*60}\n")

    # ── 内部方法 ──────────────────────────────────────

    def _extract_state(self, state: Any) -> dict:
        """从 State 对象提取字典。"""
        if hasattr(state, "__dict__"):
            return {
                k: v
                for k, v in state.__dict__.items()
                if not k.startswith("_")
            }
        if isinstance(state, dict):
            return state
        return {"raw": str(state)[:200]}

    def _summarize(self, d: dict) -> dict:
        """生成字段摘要——字符串截断，避免日志爆炸。"""
        summary = {}
        for k, v in d.items():
            if isinstance(v, str):
                summary[k] = f"{v[:80]}..." if len(v) > 80 else v
            elif isinstance(v, (int, float, bool)):
                summary[k] = v
            elif isinstance(v, list):
                summary[k] = f"[{len(v)} items]"
            elif isinstance(v, dict):
                summary[k] = f"{{{len(v)} keys}}"
            else:
                summary[k] = str(type(v).__name__)
        return summary

    def _save_snapshot(self, label: str, data: dict):
        """保存 State 快照到文件。"""
        session_dir = self.output_dir / f"session_{self.session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        filepath = session_dir / f"{label}.json"
        filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str)
        )
        self.state_snapshots.append(str(filepath))

    def _write_trace_log(self, decision: dict):
        """追加决策日志到 session 的 traces.jsonl。"""
        session_dir = self.output_dir / f"session_{self.session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        log_path = session_dir / "decision_traces.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(decision, ensure_ascii=False) + "\n")


# ── 快速使用示例 ──────────────────────────────────────
if __name__ == "__main__":
    # 模拟一个最简 Pipeline
    from dataclasses import dataclass, field

    @dataclass
    class State:
        topic: str = ""
        research_notes: str = ""
        outline: str = ""
        draft: str = ""
        review_passed: bool = False

    def mock_researcher(state: State) -> State:
        state.research_notes = "素材1: Agent决策日志实践 | 素材2: 可观测性在LLM系统中的应用"
        return state

    def mock_planner(state: State) -> State:
        state.outcome = f"基于「{state.topic}」写一篇三段式文章"
        return state

    tracer = Traceability()
    s = State(topic="Agent可追溯性")
    s = tracer.wrap(mock_researcher, "Researcher", s)
    s = tracer.wrap(mock_planner, "Planner", s)
    tracer.print_report()

# multi-agent-writer

双 Agent 协作写作系统 —— Planner 策划 + Writer 写作，State 显式传递。

Harness 实战演示项目。

## 结构

```
Planner（策划）          Writer（写作）          Reviewer（审核）
     │                       │                       │
     │            State 对象显式传递                  │
     └───────────────────────────────────────────────┘
```

## 快速开始

```bash
export DEEPSEEK_API_KEY="你的key"
python3 run.py
```

## 做了什么

1. **Agent 分工**：Planner 策划 → Writer 写作 → Reviewer 审核（不通过则退回重写）
2. **视角去重**：Writer 自动记录已用视角，避免重复
3. **State 对象**：`state.py` 统一管理 progress / viewpoints / planner_output / writer_output / review_passed / review_notes，Agent 签名 `State → State`

## 文件

| 文件 | 作用 |
|---|---|
| `run.py` | Pipeline 主流程 |
| `state.py` | State 对象定义 |
| `instructions/` | Planner / Writer / Reviewer 各自的规则 |
| `state/` | 持久化的视角记录 |



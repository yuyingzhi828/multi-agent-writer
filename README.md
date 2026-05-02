# multi-agent-writer

双 Agent 协作写作系统 —— Planner 策划 + Writer 写作，State 显式传递。

Harness 实战演示项目。

## 结构

```
Planner（策划）          Writer（写作）
   GPT-5                  DeepSeek
     │                       │
     │   State 对象显式传递    │
     └───────────────────────┘
```

## 快速开始

```bash
export OPENAI_API_KEY="你的key"
export DEEPSEEK_API_KEY="你的key"
python3 run.py
```

## 做了什么

1. **Agent 分工**：Planner（GPT-5）把主题拆成结构，Writer（DeepSeek）把结构写成文章
2. **混合模型**：策划用强推理模型，写作用性价比模型——换模型只改 Agent 内部，State 接口不动
3. **视角去重**：Writer 自动记录已用视角，避免重复
4. **State 对象**：`state.py` 统一管理 progress / viewpoints / planner_output / writer_output，Agent 签名 `State → State`

## 文件

| 文件 | 作用 |
|---|---|
| `run.py` | Pipeline 主流程 |
| `state.py` | State 对象定义 |
| `instructions/` | Planner / Writer 各自的规则 |
| `state/` | 持久化的视角记录 |

## 相关文章

[Harness 实战：为什么要在多 Agent 系统里加入 State](https://github.com/yuyingzhi828/multi-agent-writer)

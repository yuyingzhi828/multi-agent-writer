"""多Agent写作系统的 State 对象。
遵循 Harness 原则：显式优于隐式。State 在 Agent 之间显式传递，不藏在文件里。
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class State:
    """一条 State 贯穿整个 Pipeline。
    
    每个 Agent 接收 State，处理后返回 State。
    Agent 的签名统一为：State -> State
    """
    progress: str = ""                          # 当前任务描述
    viewpoints: list[dict] = field(default_factory=list)   # 视角记录 [{timestamp, log}]
    planner_output: str = ""                    # Planner 策划结果
    writer_output: str = ""                     # Writer 文章结果

    def add_viewpoint(self, log: str) -> None:
        """追加一条视角记录。Writer 每写完一篇文章就追加。"""
        self.viewpoints.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "log": log,
        })

    def get_viewpoints_text(self) -> str:
        """格式化视角记录，拼成 Writer 能读的文本。"""
        if not self.viewpoints:
            return "（本主题第一次写，请自由选择视角）"
        return "\n".join(
            f"- [{vp['timestamp']}] {vp['log']}" for vp in self.viewpoints
        )

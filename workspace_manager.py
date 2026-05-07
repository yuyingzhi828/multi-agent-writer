"""
workspace_manager.py — 三库分离管理器

三库定义：
  materials/  素材库：只读输入，Pipeline 不允许写入
  workspace/  工地：临时中间产物，Pipeline 结束后自动清理
  outputs/    成品库：最终文章，带时间戳，不覆盖

集成方式：在 run.py 顶部初始化 WorkspaceManager，
所有文件读写操作替换为 wm.read_materials() / wm.write_workspace() / wm.save_output()
"""

import os
import glob
from datetime import datetime


class WorkspaceManager:
    """三库分离管理器：素材库（只读）、工地（临时）、成品库（最终输出）"""

    def __init__(self, base_dir: str = "."):
        self.base_dir = base_dir
        self.materials_dir = os.path.join(base_dir, "materials")   # 素材库：只读
        self.workspace_dir = os.path.join(base_dir, "workspace")   # 工地：临时
        self.outputs_dir   = os.path.join(base_dir, "outputs")     # 成品库：最终

        # 确保目录存在
        os.makedirs(self.materials_dir, exist_ok=True)
        os.makedirs(self.workspace_dir, exist_ok=True)
        os.makedirs(self.outputs_dir,   exist_ok=True)

    # ============ 素材库（只读）============

    def read_materials(self, filename: str = None) -> str:
        """读取素材库文件。
        
        filename=None 时全量扫描所有 .md 文件，返回拼接的摘要。
        filename 指定时读单个文件。
        素材库只读，Pipeline 不允许写入。
        """
        if filename:
            path = os.path.join(self.materials_dir, filename)
            if not os.path.exists(path):
                return ""
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        # 全量扫描
        results = []
        for fpath in glob.glob(f"{self.materials_dir}/**/*.md", recursive=True):
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            preview = content[:500] + ("..." if len(content) > 500 else "")
            results.append(f"### {os.path.basename(fpath)}\n{preview}")
        return "\n\n".join(results) if results else "（素材库为空）"

    def write_materials(self, *args, **kwargs):
        """写入素材库——Pipeline 内部禁止调用，直接抛异常。
        
        如需更新素材，请手动放入 materials/ 目录。
        """
        raise PermissionError(
            f"[WorkspaceManager] 素材库只读，Pipeline 不允许写入。"
            f"如需更新素材，请手动放入 {self.materials_dir}/"
        )

    def list_materials(self) -> list[str]:
        """列出素材库所有文件路径"""
        return sorted(glob.glob(f"{self.materials_dir}/**/*", recursive=True))

    # ============ 工地（读写 + 清理）============

    def read_workspace(self, filename: str) -> str:
        """读工地文件。文件不存在时返回空字符串。"""
        path = os.path.join(self.workspace_dir, filename)
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def write_workspace(self, filename: str, content: str):
        """写入工地文件（临时中间产物）。
        
        同名文件直接覆盖，这是预期行为——工地就是临时区。
        """
        path = os.path.join(self.workspace_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[WorkspaceManager] 工地写入: {filename}")

    def clean_workspace(self, keep_on_failure: bool = False):
        """清理工地。Pipeline 正常结束时调用。

        keep_on_failure=True：Pipeline 异常退出时保留工地用于调试。
        keep_on_failure=False（默认）：正常结束，全部清理。
        """
        if keep_on_failure:
            print(
                f"[WorkspaceManager] ⚠️  Pipeline 异常，工地文件保留用于调试: "
                f"{self.workspace_dir}/"
            )
            return

        files = glob.glob(f"{self.workspace_dir}/**/*", recursive=True)
        cleaned = 0
        for f in files:
            if os.path.isfile(f):
                os.remove(f)
                cleaned += 1
        print(f"[WorkspaceManager] 工地清理完成，删除 {cleaned} 个文件")

    def list_workspace(self) -> list[str]:
        """列出工地当前所有文件"""
        return sorted(glob.glob(f"{self.workspace_dir}/**/*", recursive=True))

    # ============ 成品库（只写）============

    def save_output(self, content: str, topic: str = "article") -> str:
        """写入成品库。文件名自动加时间戳，不覆盖旧文件。

        返回成品文件的完整路径。
        文件名格式：{时间戳}_{topic清洗后}.md
        例：20260506_120000_三库分离架构设计.md
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 清洗 topic 用作文件名（保留字母、数字、中文、横杠、下划线、空格）
        safe_topic = "".join(
            c if (c.isalnum() or '\u4e00' <= c <= '\u9fff' or c in "-_ ") else "_"
            for c in topic
        )
        safe_topic = safe_topic[:30].strip()
        filename = f"{timestamp}_{safe_topic}.md"
        path = os.path.join(self.outputs_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[WorkspaceManager] ✅ 成品写入: {filename}")
        return path

    def list_outputs(self) -> list[str]:
        """列出所有成品，按时间倒序（最新在最前）"""
        return sorted(glob.glob(f"{self.outputs_dir}/*.md"), reverse=True)

    # ============ 工具方法 ============

    def status(self):
        """打印三库当前状态（调试用）"""
        materials = self.list_materials()
        workspace = self.list_workspace()
        outputs   = self.list_outputs()
        print(f"\n[WorkspaceManager] 三库状态:")
        print(f"  素材库 ({self.materials_dir}/): {len(materials)} 个文件")
        print(f"  工地   ({self.workspace_dir}/): {len(workspace)} 个文件")
        print(f"  成品库 ({self.outputs_dir}/): {len(outputs)} 个文件")
        if workspace:
            print(f"  工地文件列表:")
            for f in workspace:
                print(f"    - {os.path.basename(f)}")
        if outputs:
            print(f"  最新成品: {os.path.basename(outputs[0])}")
        print()


# ============ run.py 集成示例 ============
#
# 在 run.py 顶部加：
#   from workspace_manager import WorkspaceManager
#   wm = WorkspaceManager(base_dir=".")
#
# Researcher 改为：
#   local_materials = wm.read_materials()          # 读素材库
#   wm.write_workspace("research_notes.md", ...)   # 写工地
#
# Planner / Writer / Reviewer 类似：
#   wm.write_workspace("planner_output.md", ...)
#   wm.write_workspace(f"draft_v{retry+1}.md", ...)
#   wm.write_workspace("review_notes.md", ...)
#
# Pipeline 结束时：
#   pipeline_success = True
#   try:
#       # ... Agent 调用 ...
#   except Exception as e:
#       pipeline_success = False
#       raise
#   finally:
#       if pipeline_success and state.review_passed:
#           output_path = wm.save_output(state.writer_output, topic=state.progress[:30])
#           print(f"[Pipeline] 成品已保存: {output_path}")
#       wm.clean_workspace(keep_on_failure=not pipeline_success)

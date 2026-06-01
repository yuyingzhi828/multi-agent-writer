"""
delivery.py —— Pipeline 自动交付层

Reviewer 通过之后，Pipeline 最后一步：
1. 把文章落到成品库（outputs/），文件名带版本号
2. 写入 outputs/index.json 成品索引（方便查询和统计）
3. 生成交付报告（版本、时间、各层耗时、风格评分摘要）

用法（在 run.py 最后）：
    from delivery import Delivery

    delivery = Delivery(config=CONFIG)
    result = delivery.deliver(state, topic=topic)
    print(result.summary())

Harness 系列第 14 篇：Pipeline 自动交付
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from state import State


# ============ 数据结构 ============

@dataclass
class DeliveryResult:
    """一次交付的完整记录。"""
    topic: str
    version: int
    file_path: str                        # 落盘路径，绝对路径
    timestamp: str                        # 交付时间 ISO 格式
    review_passed: bool
    review_notes: str = ""
    style_score: Optional[float] = None  # StyleReviewer 总分（可选）
    word_count: int = 0
    generation_time_s: float = 0.0       # Pipeline 总耗时（秒）
    agent_times: dict = field(default_factory=dict)  # 各 Agent 耗时

    def summary(self) -> str:
        lines = [
            f"✅ 交付完成 — v{self.version}",
            f"   文件：{self.file_path}",
            f"   主题：{self.topic}",
            f"   字数：{self.word_count} 字",
            f"   审核：{'通过' if self.review_passed else '⚠️ 未通过（已达最大重试）'}",
        ]
        if self.style_score is not None:
            label = "✅ 对味" if self.style_score >= 0.65 else "🟡 偏味"
            lines.append(f"   风格：{self.style_score:.0%} {label}")
        if self.generation_time_s > 0:
            lines.append(f"   耗时：{self.generation_time_s:.1f}s")
        return "\n".join(lines)

    def to_index_entry(self) -> dict:
        """转为 index.json 的一条记录。"""
        return {
            "topic": self.topic,
            "version": self.version,
            "file": self.file_path,
            "timestamp": self.timestamp,
            "review_passed": self.review_passed,
            "style_score": self.style_score,
            "word_count": self.word_count,
        }


# ============ 核心：Delivery ============

class Delivery:
    """Pipeline 自动交付层。

    职责：
    1. 成品落盘（版本号自增，文件名可读）
    2. 维护 outputs/index.json 成品索引
    3. 生成交付报告

    Args:
        config: run.py 的 CONFIG 字典，至少含 outputs_dir
        outputs_dir: 直接指定成品库路径（覆盖 config）
    """

    INDEX_FILE = "index.json"

    def __init__(
        self,
        config: Optional[dict] = None,
        outputs_dir: Optional[str] = None,
    ):
        cfg = config or {}
        self.outputs_dir = Path(outputs_dir or cfg.get("outputs_dir", "outputs"))
        self.outputs_dir.mkdir(parents=True, exist_ok=True)

    # ================================================================
    # 主入口
    # ================================================================

    def deliver(
        self,
        state: State,
        topic: str,
        generation_time_s: float = 0.0,
        agent_times: Optional[dict] = None,
        style_score: Optional[float] = None,
    ) -> DeliveryResult:
        """把 State 里的文章落到成品库，返回交付结果。

        Args:
            state: Pipeline 运行完的 State
            topic: 文章主题（用于文件命名）
            generation_time_s: Pipeline 总耗时（秒，可选）
            agent_times: 各 Agent 耗时字典（可选）
            style_score: StyleReviewer 总分（可选）

        Returns:
            DeliveryResult，含落盘路径、版本号、摘要。
        """
        content = state.writer_output or ""
        if not content:
            raise ValueError("state.writer_output 为空，没有可交付的内容")

        # 版本号
        version = self._next_version(topic)
        timestamp = datetime.now().isoformat(timespec="seconds")

        # 文件名：safe_topic_v1.md
        safe_topic = self._safe_name(topic)
        filename = f"{safe_topic}_v{version}.md"
        filepath = self.outputs_dir / filename

        # 注入文件头注释
        header = self._build_header(
            topic=topic,
            version=version,
            timestamp=timestamp,
            review_passed=state.review_passed,
            style_score=style_score,
        )
        filepath.write_text(header + content, encoding="utf-8")

        # 字数统计（去掉 Markdown 标记）
        word_count = self._count_words(content)

        result = DeliveryResult(
            topic=topic,
            version=version,
            file_path=str(filepath.resolve()),
            timestamp=timestamp,
            review_passed=state.review_passed,
            review_notes=state.review_notes or "",
            style_score=style_score,
            word_count=word_count,
            generation_time_s=generation_time_s,
            agent_times=agent_times or {},
        )

        # 更新索引
        self._update_index(result)

        return result

    # ================================================================
    # 成品索引
    # ================================================================

    def _update_index(self, result: DeliveryResult) -> None:
        """把这次交付追加到 outputs/index.json。"""
        index_path = self.outputs_dir / self.INDEX_FILE
        entries = []
        if index_path.exists():
            try:
                entries = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                entries = []

        entries.append(result.to_index_entry())
        index_path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_index(self) -> list[dict]:
        """读取成品索引，返回所有交付记录。"""
        index_path = self.outputs_dir / self.INDEX_FILE
        if not index_path.exists():
            return []
        try:
            return json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def list_deliveries(self, topic: Optional[str] = None) -> list[dict]:
        """列出成品库中的文章，可按主题过滤。"""
        entries = self.get_index()
        if topic:
            entries = [e for e in entries if topic in e.get("topic", "")]
        return entries

    def stats(self) -> dict:
        """成品库统计：总篇数、通过率、平均字数。"""
        entries = self.get_index()
        if not entries:
            return {"total": 0, "pass_rate": 0.0, "avg_words": 0}
        passed = sum(1 for e in entries if e.get("review_passed"))
        total_words = sum(e.get("word_count", 0) for e in entries)
        return {
            "total": len(entries),
            "pass_rate": passed / len(entries),
            "avg_words": total_words // len(entries),
        }

    # ================================================================
    # 辅助函数
    # ================================================================

    def _next_version(self, topic: str) -> int:
        """找下一个版本号：扫描已有文件，取最大版本号 + 1。"""
        safe = self._safe_name(topic)
        existing = list(self.outputs_dir.glob(f"{safe}_v*.md"))
        if not existing:
            return 1
        versions = []
        for f in existing:
            m = re.search(r"_v(\d+)\.md$", f.name)
            if m:
                versions.append(int(m.group(1)))
        return max(versions) + 1 if versions else 1

    @staticmethod
    def _safe_name(topic: str) -> str:
        """把主题转成合法文件名（最多 30 字符）。"""
        safe = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', topic)
        return safe[:30].strip('_') or "article"

    @staticmethod
    def _build_header(
        topic: str,
        version: int,
        timestamp: str,
        review_passed: bool,
        style_score: Optional[float],
    ) -> str:
        """生成文件头注释，嵌入元数据。"""
        lines = [
            f"<!-- 版本 v{version}",
            f"     主题 {topic}",
            f"     生成时间 {timestamp}",
            f"     审核 {'通过' if review_passed else '未通过'}",
        ]
        if style_score is not None:
            lines.append(f"     风格评分 {style_score:.0%}")
        lines.append("-->")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _count_words(text: str) -> int:
        """统计中文字数（去掉 Markdown 标记后计算）。"""
        # 去掉代码块
        text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        # 去掉 HTML 注释
        text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
        # 去掉 Markdown 标记
        text = re.sub(r'[#*`\[\]()>_~\-]', '', text)
        # 统计中文字符 + 英文单词
        chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
        english = len(re.findall(r'\b[a-zA-Z]+\b', text))
        return chinese + english


# ============ 自测 ============

if __name__ == "__main__":
    import tempfile
    import os

    print("=" * 60)
    print("Delivery 自测")
    print("=" * 60)

    # 临时目录模拟成品库
    with tempfile.TemporaryDirectory() as tmpdir:
        delivery = Delivery(outputs_dir=tmpdir)

        # 模拟 State
        state = State(
            progress="测试主题",
            writer_output="""# 测试文章

这是第一段内容。风格指纹系统解决了一个问题——AI 写的文章读起来不像你写的。

## 第二节

把风格拆成五个维度，每个维度都是数字。数字就能传给 AI。

你有没有发现，有些词是你自己的「小词」？那就是你的指纹。

下一篇讲风格 Reviewer。
""",
            review_passed=True,
            review_notes="[REVIEW_RESULT]\n通过：是\n问题：无\n[/REVIEW_RESULT]",
        )

        # 第一次交付
        result1 = delivery.deliver(
            state,
            topic="风格指纹测试",
            generation_time_s=12.5,
            style_score=0.78,
        )
        print(result1.summary())

        # 第二次交付（同一主题，版本号应该变成 v2）
        result2 = delivery.deliver(
            state,
            topic="风格指纹测试",
            generation_time_s=9.3,
            style_score=0.84,
        )
        print(f"\n第二次版本号：v{result2.version}（应为 2）")
        assert result2.version == 2, f"版本号错误：{result2.version}"

        # 验证 index.json
        entries = delivery.get_index()
        print(f"\nindex.json 共 {len(entries)} 条记录")
        assert len(entries) == 2

        # 统计
        stats = delivery.stats()
        print(f"统计：{stats}")
        assert stats["total"] == 2
        assert stats["pass_rate"] == 1.0

        # 列出成品
        print(f"\n成品库文件：")
        for e in delivery.list_deliveries():
            print(f"  v{e['version']} | {e['topic']} | {e['word_count']}字 | {e['timestamp']}")

        # 验证文件头
        content = Path(result1.file_path).read_text(encoding="utf-8")
        assert "版本 v1" in content
        assert "风格评分" in content
        print(f"\n文件头验证通过")

    print("\n" + "=" * 60)
    print("自测完成")
    print("=" * 60)

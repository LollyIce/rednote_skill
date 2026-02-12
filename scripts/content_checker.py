"""
内容质量检查器
检查文章内容是否符合小红书写作规范：去 AI 味、不捏造事实、情绪自然
"""

import json
import re
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# 加载写作指南
_GUIDELINES_PATH = Path(__file__).parent.parent / "resources" / "writing_guidelines.json"


def _load_guidelines() -> dict:
    with open(_GUIDELINES_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


class ContentCheckResult:
    """内容检查结果"""

    def __init__(self):
        self.warnings: list[dict] = []      # 警告（建议修改）
        self.errors: list[dict] = []        # 错误（必须修改）
        self.suggestions: list[str] = []    # 改进建议
        self.score: int = 100               # 质量评分 0-100

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def add_warning(self, rule: str, message: str, context: str = ""):
        self.warnings.append({"rule": rule, "message": message, "context": context})
        self.score = max(0, self.score - 5)

    def add_error(self, rule: str, message: str, context: str = ""):
        self.errors.append({"rule": rule, "message": message, "context": context})
        self.score = max(0, self.score - 15)

    def add_suggestion(self, suggestion: str):
        self.suggestions.append(suggestion)


def check_ai_patterns(content: str, title: str, guidelines: dict) -> list[dict]:
    """检查 AI 感词句"""
    issues = []
    forbidden = guidelines.get("forbidden_patterns", [])

    full_text = f"{title} {content}"

    for pattern in forbidden:
        # 处理带省略号的模式（如 "首先…其次…最后…"）
        if "…" in pattern:
            parts = [p.strip() for p in pattern.split("…") if p.strip()]
            if len(parts) >= 2:
                # 检查文本中是否同时包含这些关键词
                found_all = all(p in full_text for p in parts)
                if found_all:
                    issues.append({
                        "pattern": pattern,
                        "context": pattern,
                    })
        else:
            if pattern in full_text:
                # 找到上下文
                idx = full_text.find(pattern)
                start = max(0, idx - 10)
                end = min(len(full_text), idx + len(pattern) + 10)
                issues.append({
                    "pattern": pattern,
                    "context": f"...{full_text[start:end]}...",
                })

    return issues


def check_fabrication_risk(content: str, user_provided_facts: Optional[dict] = None) -> list[dict]:
    """
    检查可能的事实捏造风险
    识别文中的具体时间、价格、地点等信息，标记为潜在风险
    """
    issues = []

    # 检查具体时间（几点钟）
    time_patterns = re.findall(r'(早上|上午|中午|下午|晚上|凌晨)?\s*(\d{1,2})[:\：点](\d{0,2})', content)
    for match in time_patterns:
        full = "".join(match)
        if user_provided_facts and full in str(user_provided_facts.get("times", [])):
            continue
        issues.append({
            "type": "具体时间",
            "value": full,
            "message": f"检测到具体时间「{''.join(match)}」，请确认是否为用户提供的真实信息",
        })

    # 检查具体价格
    price_patterns = re.findall(r'(\d+\.?\d*)\s*[元块¥￥]|人均\s*(\d+)', content)
    for match in price_patterns:
        value = match[0] or match[1]
        if user_provided_facts and value in str(user_provided_facts.get("prices", [])):
            continue
        issues.append({
            "type": "具体价格",
            "value": f"{value}元",
            "message": f"检测到具体价格「{value}元」，请确认是否为用户提供的真实信息",
        })

    # 检查"朋友说"、"同事说"等转述
    hearsay_patterns = re.findall(r'(朋友|同事|闺蜜|老公|老婆|室友|同学)\s*(说|推荐|安利|告诉我)', content)
    for match in hearsay_patterns:
        issues.append({
            "type": "他人转述",
            "value": "".join(match),
            "message": f"检测到他人转述「{''.join(match)}」，请确认是否为真实经历",
        })

    return issues


def check_emotion_density(content: str) -> list[dict]:
    """检查情绪表达密度，避免过度密集"""
    issues = []
    sentences = re.split(r'[。！？!?\n]', content)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 2]

    consecutive_exclaim = 0
    for i, sent in enumerate(sentences):
        # 检查感叹句
        if sent.endswith("！") or sent.endswith("!") or "啊啊" in sent or "太" in sent and ("了" in sent or "！" in sent):
            consecutive_exclaim += 1
        else:
            consecutive_exclaim = 0

        if consecutive_exclaim >= 3:
            issues.append({
                "type": "情绪过密",
                "position": i,
                "message": f"连续 {consecutive_exclaim} 句情绪激动的句子，建议穿插一些平淡的叙述来降温",
                "context": sent[:30],
            })

    # 检查 emoji 密度
    emojis = re.findall(r'[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]', content)
    if len(emojis) > 10:
        issues.append({
            "type": "emoji过多",
            "message": f"检测到 {len(emojis)} 个 emoji，建议控制在 3-6 个，过多会显得刻意",
        })

    return issues


def check_content_length(content: str) -> Optional[dict]:
    """检查内容长度"""
    length = len(content)
    if length < 100:
        return {"type": "过短", "length": length, "message": "正文不足100字，信息量太少，建议补充细节和个人感受"}
    if length > 1000:
        return {"type": "过长", "length": length, "message": "正文超过1000字，手机端阅读压力大，建议精简到 300-800 字"}
    return None


def check_title_quality(title: str, guidelines: dict) -> list[dict]:
    """检查标题质量"""
    issues = []

    if len(title) > 20:
        issues.append({"type": "标题过长", "message": f"标题 {len(title)} 字，建议不超过 20 字"})

    if title.startswith("震惊"):
        issues.append({"type": "标题党", "message": "以'震惊'开头的标题已经被人反感了，换个方式"})

    # 检查是否堆砌 emoji
    title_emojis = re.findall(r'[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]', title)
    if len(title_emojis) > 3:
        issues.append({"type": "标题emoji过多", "message": f"标题中有 {len(title_emojis)} 个 emoji，建议最多 1-2 个"})

    return issues


def check_content(
    title: str,
    content: str,
    user_provided_facts: Optional[dict] = None,
) -> ContentCheckResult:
    """
    综合内容质量检查

    Args:
        title: 文章标题
        content: 文章正文
        user_provided_facts: 用户明确提供的事实信息（可选），格式如：
            {
                "times": ["早上9点"],
                "prices": ["68"],
                "places": ["xxx咖啡馆"],
            }
    """
    guidelines = _load_guidelines()
    result = ContentCheckResult()

    # 1. AI 感检查
    ai_issues = check_ai_patterns(content, title, guidelines)
    for issue in ai_issues:
        result.add_error(
            "ai_pattern",
            f"检测到 AI 感表达「{issue['pattern']}」",
            issue.get("context", ""),
        )

    # 2. 事实捏造风险检查
    fab_issues = check_fabrication_risk(content, user_provided_facts)
    for issue in fab_issues:
        result.add_warning(
            "fabrication_risk",
            issue["message"],
            issue.get("value", ""),
        )

    # 3. 情绪密度检查
    emo_issues = check_emotion_density(content)
    for issue in emo_issues:
        result.add_warning(
            "emotion_density",
            issue["message"],
            issue.get("context", ""),
        )

    # 4. 内容长度检查
    len_issue = check_content_length(content)
    if len_issue:
        result.add_warning("length", len_issue["message"])

    # 5. 标题质量检查
    title_issues = check_title_quality(title, guidelines)
    for issue in title_issues:
        result.add_warning("title", issue["message"])

    # 添加通用建议
    if not ai_issues:
        result.add_suggestion("✅ 未检测到 AI 感表达，语气自然")
    if not fab_issues:
        result.add_suggestion("✅ 未检测到可疑的事实捏造")
    if len(content) > 150 and not any(c in content for c in ["…", "——", "..."]):
        result.add_suggestion("💡 可以适当加入省略号或破折号，制造留白和停顿感")

    return result


def print_check_result(result: ContentCheckResult):
    """在终端中打印检查结果"""

    # 评分面板
    score_color = "green" if result.score >= 80 else "yellow" if result.score >= 60 else "red"
    console.print(Panel(
        f"📊 内容质量评分: [{score_color}]{result.score}[/{score_color}] / 100",
        style=score_color,
    ))

    # 错误列表
    if result.errors:
        table = Table(title="❌ 必须修改", show_lines=True, style="red")
        table.add_column("规则", style="red", max_width=15)
        table.add_column("问题", max_width=50)
        table.add_column("上下文", style="dim", max_width=30)
        for err in result.errors:
            table.add_row(err["rule"], err["message"], err.get("context", ""))
        console.print(table)

    # 警告列表
    if result.warnings:
        table = Table(title="⚠️ 建议修改", show_lines=True, style="yellow")
        table.add_column("规则", style="yellow", max_width=15)
        table.add_column("问题", max_width=50)
        table.add_column("上下文", style="dim", max_width=30)
        for warn in result.warnings:
            table.add_row(warn["rule"], warn["message"], warn.get("context", ""))
        console.print(table)

    # 建议
    if result.suggestions:
        console.print("\n[bold]💡 其他建议:[/bold]")
        for sug in result.suggestions:
            console.print(f"  {sug}")

    console.print()


# ============================================================
# CLI 入口 - 独立运行时检查指定文件
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="小红书内容质量检查器")
    parser.add_argument("--title", "-t", required=True, help="文章标题")
    parser.add_argument("--content", "-c", help="正文内容")
    parser.add_argument("--content-file", "-f", help="正文文件路径")

    args = parser.parse_args()

    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    elif args.content:
        content = args.content
    else:
        print("请提供 --content 或 --content-file")
        exit(1)

    result = check_content(args.title, content)
    print_check_result(result)

    if result.passed:
        console.print("[green]✅ 内容检查通过[/green]")
    else:
        console.print("[red]❌ 内容检查未通过，请修改后重试[/red]")

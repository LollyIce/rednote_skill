"""
热门话题排行榜脚本
抓取小红书近期热门话题/趋势，生成 Top 10/20 排行榜
"""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import config
from browser_helper import launch_browser, ensure_login, close_browser, navigate_to
from utils import (
    random_delay, extract_text, extract_attribute,
    parse_count, save_to_json, smooth_scroll, wait_for_any_selector,
)

console = Console()


def _load_selectors() -> dict:
    with open(config.SELECTORS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


async def scrape_explore_topics(page, selectors: dict, count: int) -> list[dict]:
    """
    从小红书发现/探索页面抓取热门话题
    通过分析首页推荐流中的高频话题来获取趋势
    """
    topics = []
    topic_sels = selectors.get("explore", {})

    console.print("  [cyan]正在抓取探索页热门内容...[/cyan]")

    await navigate_to(page, config.XIAOHONGSHU_EXPLORE)
    await random_delay(*config.PAGE_LOAD_WAIT)

    # 尝试从探索页的话题推荐区域抓取
    topic_card_sels = topic_sels.get("topic_card", ".topic-card, .channel-item, .category-item").split(", ")
    topic_name_sels = topic_sels.get("topic_name", ".topic-name, .channel-name, .title, span").split(", ")
    topic_count_sels = topic_sels.get("topic_view_count", ".view-count, .count, .desc").split(", ")

    # 先尝试直接获取话题卡片
    for sel in topic_card_sels:
        cards = await page.query_selector_all(sel)
        if cards:
            for card in cards:
                if len(topics) >= count:
                    break
                try:
                    name_el = None
                    for ns in topic_name_sels:
                        name_el = await card.query_selector(ns)
                        if name_el:
                            break
                    name = await extract_text(name_el, "") if name_el else ""
                    if not name:
                        continue

                    count_el = None
                    for cs in topic_count_sels:
                        count_el = await card.query_selector(cs)
                        if count_el:
                            break
                    view_text = await extract_text(count_el, "0") if count_el else "0"

                    link_el = await card.query_selector("a")
                    href = await extract_attribute(link_el, "href", "") if link_el else ""

                    topics.append({
                        "name": name.strip().lstrip("#"),
                        "view_count": parse_count(view_text),
                        "url": href,
                        "source": "explore_page",
                    })
                except Exception:
                    continue
            if topics:
                break

    return topics


async def scrape_trending_from_search(page, selectors: dict, count: int) -> list[dict]:
    """
    通过搜索页面的热搜词/推荐词来获取热门话题
    小红书搜索框点击后通常会展示热搜榜
    """
    topics = []
    search_sels = selectors.get("search", {})
    trending_sels = selectors.get("trending", {})

    console.print("  [cyan]正在获取搜索热词...[/cyan]")

    await navigate_to(page, config.XIAOHONGSHU_HOME)
    await random_delay(*config.PAGE_LOAD_WAIT)

    # 点击搜索框，触发热搜展示
    search_input_sels = search_sels.get("search_input", "#search-input").split(", ")
    search_input = await wait_for_any_selector(page, search_input_sels, timeout=8000)

    if search_input:
        await search_input.click()
        await random_delay(1, 2)

        # 抓取热搜列表
        hot_item_sels = trending_sels.get(
            "hot_search_item",
            ".trending-item, .hot-item, .search-trending-item, .hot-list-item, .hot-word"
        ).split(", ")
        hot_name_sels = trending_sels.get(
            "hot_search_name",
            ".title, .name, .word, span, a"
        ).split(", ")
        hot_rank_sels = trending_sels.get(
            "hot_search_rank",
            ".rank, .index, .num"
        ).split(", ")
        hot_heat_sels = trending_sels.get(
            "hot_search_heat",
            ".hot-score, .heat, .score, .count"
        ).split(", ")

        for sel in hot_item_sels:
            items = await page.query_selector_all(sel)
            if items:
                for item in items:
                    if len(topics) >= count:
                        break
                    try:
                        # 获取话题名
                        name_el = None
                        for ns in hot_name_sels:
                            name_el = await item.query_selector(ns)
                            if name_el:
                                break
                        name = await extract_text(name_el, "") if name_el else ""
                        if not name or len(name) < 2:
                            continue

                        # 获取排名
                        rank_el = None
                        for rs in hot_rank_sels:
                            rank_el = await item.query_selector(rs)
                            if rank_el:
                                break
                        rank_text = await extract_text(rank_el, "") if rank_el else ""

                        # 获取热度
                        heat_el = None
                        for hs in hot_heat_sels:
                            heat_el = await item.query_selector(hs)
                            if heat_el:
                                break
                        heat_text = await extract_text(heat_el, "0") if heat_el else "0"

                        topics.append({
                            "name": name.strip(),
                            "rank": rank_text,
                            "heat": parse_count(heat_text),
                            "source": "search_trending",
                        })
                    except Exception:
                        continue
                if topics:
                    break

    return topics


async def scrape_trending_from_feed(page, count: int) -> list[dict]:
    """
    兜底策略：从首页信息流中统计高频话题标签
    即使没有官方热搜入口，也能通过分析信息流得到趋势
    """
    console.print("  [cyan]正在分析首页信息流中的热门话题...[/cyan]")

    await navigate_to(page, config.XIAOHONGSHU_HOME)
    await random_delay(*config.PAGE_LOAD_WAIT)

    tag_counter: dict[str, int] = {}

    # 多次滚动采集
    for scroll_round in range(8):
        # 收集页面中所有 hashtag 链接
        tag_elements = await page.query_selector_all(
            "a[href*='/page/topics/'], .hashtag, .tag-item, a[href*='keyword=']"
        )

        for el in tag_elements:
            text = await extract_text(el, "")
            text = text.strip().lstrip("#")
            if text and len(text) >= 2 and len(text) <= 20:
                tag_counter[text] = tag_counter.get(text, 0) + 1

        # 也从笔记标题中提取话题标签 (#xxx)
        all_text_els = await page.query_selector_all(
            ".note-item .title, .note-item span, .note-item .desc"
        )
        for el in all_text_els:
            text = await extract_text(el, "")
            import re
            tags_in_text = re.findall(r'#([\u4e00-\u9fffA-Za-z0-9]{2,15})', text)
            for tag in tags_in_text:
                tag_counter[tag] = tag_counter.get(tag, 0) + 1

        await smooth_scroll(page, distance=600, times=2)
        await random_delay(*config.SCROLL_DELAY)

        console.print(f"    [dim]第 {scroll_round + 1}/8 轮扫描，已发现 {len(tag_counter)} 个话题[/dim]")

    # 按出现频次排序
    sorted_tags = sorted(tag_counter.items(), key=lambda x: x[1], reverse=True)[:count]

    topics = []
    for rank, (name, freq) in enumerate(sorted_tags, 1):
        topics.append({
            "name": name,
            "frequency": freq,
            "heat": freq * 100,  # 用频次估算热度
            "source": "feed_analysis",
        })

    return topics


def generate_trending_report(topics: list[dict], count: int) -> str:
    """生成热门话题排行榜 Markdown 报告"""
    lines = [
        f"# 🔥 小红书热门话题排行榜",
        f"",
        f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **话题数量**: {len(topics)}",
        f"",
        f"---",
        f"",
    ]

    # 排行表格
    lines.append("## 排行榜")
    lines.append("")
    lines.append("| 排名 | 话题 | 热度 | 数据来源 |")
    lines.append("|------|------|------|----------|")

    for i, topic in enumerate(topics[:count], 1):
        name = topic["name"]
        heat = topic.get("heat", topic.get("frequency", 0))
        source_map = {
            "search_trending": "🔍 搜索热搜",
            "explore_page": "🌟 探索推荐",
            "feed_analysis": "📊 信息流分析",
        }
        source = source_map.get(topic.get("source", ""), "未知")

        # 前三名加火焰 emoji
        rank_display = f"🥇" if i == 1 else f"🥈" if i == 2 else f"🥉" if i == 3 else f"{i}"

        lines.append(f"| {rank_display} | #{name} | {heat:,} | {source} |")

    lines.append("")

    # 创作建议
    lines.append("## 💡 蹭热点建议")
    lines.append("")
    if len(topics) >= 3:
        top3 = [t["name"] for t in topics[:3]]
        lines.append(f"当前最热话题是 **#{top3[0]}**、**#{top3[1]}**、**#{top3[2]}**。")
        lines.append("")
        lines.append("参考方向：")
        lines.append(f"- 围绕「{top3[0]}」分享你的真实体验或看法")
        lines.append(f"- 把「{top3[1]}」和你的领域做交叉，找到独特切入点")
        lines.append(f"- 「{top3[2]}」适合写观点类或故事类笔记")
    lines.append("")

    return "\n".join(lines)


async def get_trending(count: int = 20, output: str = None):
    """
    主流程：获取热门话题排行榜
    使用三级策略：搜索热搜 → 探索页推荐 → 信息流分析
    """
    config.ensure_dirs()
    selectors = _load_selectors()

    console.print(Panel(
        f"🔥 小红书热门话题排行榜\n"
        f"   目标数量: Top {count}",
        style="bold magenta",
    ))

    context, page = await launch_browser()

    try:
        # 检查登录
        is_logged_in = await check_login_status(page)
        if not is_logged_in:
            success = await wait_for_login(page)
            if not success:
                console.print("[red]未能登录，退出[/red]")
                return

        all_topics = []

        # 策略 1：搜索热搜
        console.print("\n[bold]📍 策略 1: 获取搜索热搜[/bold]")
        trending_topics = await scrape_trending_from_search(page, selectors, count)
        if trending_topics:
            console.print(f"  [green]✓ 从搜索热搜获取了 {len(trending_topics)} 个话题[/green]")
            all_topics.extend(trending_topics)
        else:
            console.print("  [yellow]未能从搜索热搜获取话题[/yellow]")

        # 策略 2：探索页推荐
        if len(all_topics) < count:
            console.print("\n[bold]📍 策略 2: 获取探索页推荐话题[/bold]")
            explore_topics = await scrape_explore_topics(page, selectors, count - len(all_topics))
            if explore_topics:
                console.print(f"  [green]✓ 从探索页获取了 {len(explore_topics)} 个话题[/green]")
                all_topics.extend(explore_topics)
            else:
                console.print("  [yellow]未能从探索页获取话题[/yellow]")

        # 策略 3：信息流分析（兜底）
        if len(all_topics) < count:
            console.print("\n[bold]📍 策略 3: 分析首页信息流热门标签[/bold]")
            feed_topics = await scrape_trending_from_feed(page, count - len(all_topics))
            if feed_topics:
                console.print(f"  [green]✓ 从信息流获取了 {len(feed_topics)} 个话题[/green]")
                all_topics.extend(feed_topics)

        if not all_topics:
            console.print("\n[red]❌ 未能获取到任何热门话题[/red]")
            return

        # 去重（按话题名）
        seen = set()
        unique_topics = []
        for t in all_topics:
            name = t["name"]
            if name not in seen:
                seen.add(name)
                unique_topics.append(t)
        all_topics = unique_topics[:count]

        # 保存原始数据
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = config.OUTPUT_DIR / f"hot_topics_{timestamp}.json"
        save_to_json(all_topics, json_path)

        # 生成报告
        report = generate_trending_report(all_topics, count)
        report_path = Path(output) if output else config.OUTPUT_DIR / f"hot_topics_{timestamp}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        console.print(f"\n[green]✓ 报告已保存: {report_path}[/green]")

        # 终端展示排行榜
        _print_ranking(all_topics, count)

    finally:
        await close_browser(context)


def _print_ranking(topics: list[dict], count: int):
    """在终端打印排行榜"""
    table = Table(title="🔥 小红书热门话题 Top " + str(min(count, len(topics))), show_lines=True)
    table.add_column("排名", justify="center", style="bold", width=6)
    table.add_column("话题", style="cyan", max_width=30)
    table.add_column("热度", justify="right", style="magenta")
    table.add_column("来源", style="dim", max_width=15)

    source_map = {
        "search_trending": "搜索热搜",
        "explore_page": "探索推荐",
        "feed_analysis": "信息流分析",
    }

    for i, topic in enumerate(topics[:count], 1):
        rank = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else str(i)
        heat = topic.get("heat", topic.get("frequency", 0))
        source = source_map.get(topic.get("source", ""), "未知")
        table.add_row(rank, f"#{topic['name']}", f"{heat:,}", source)

    console.print()
    console.print(table)


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="小红书热门话题排行榜")
    parser.add_argument("--count", "-c", type=int, default=20,
                        help="排行榜数量 (默认: 20, 可选 10/20)")
    parser.add_argument("--output", "-o", help="报告输出路径")

    args = parser.parse_args()
    count = min(max(args.count, 5), 50)  # 限制 5-50

    asyncio.run(get_trending(count=count, output=args.output))


if __name__ == "__main__":
    main()

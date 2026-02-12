"""
学习高流量文章脚本
通过浏览器自动化搜索小红书热门笔记，抓取内容与互动数据，生成分析报告
"""

import argparse
import asyncio
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

import config
from browser_helper import launch_browser, close_browser, navigate_to, ensure_login, ensure_login_on_page
from utils import (
    random_delay, safe_click, extract_text, extract_attribute,
    parse_count, save_to_json, smooth_scroll, wait_for_any_selector, truncate_text,
)

console = Console()


def _load_selectors() -> dict:
    """加载选择器配置"""
    with open(config.SELECTORS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


async def _apply_filters(page, sort: str = "hot"):
    """
    打开筛选面板并选择排序方式和发布时间
    排序依据: 综合 / 最新 / 最多点赞 / 最多评论 / 最多收藏
    发布时间: 不限 / 一天内 / 一周内 / 半年内
    """
    # 决定排序文本
    sort_map = {
        "hot": "最多点赞",
        "new": "最新",
        "comment": "最多评论",
        "collect": "最多收藏",
    }
    sort_text = sort_map.get(sort, "综合")
    date_text = "半年内"  # 默认筛选半年内的笔记

    # 点击筛选按钮（必须用 JS click，Playwright click 不触发面板）
    filter_exists = await page.query_selector("div.filter")
    if not filter_exists:
        console.print("  [yellow]未找到筛选按钮，使用默认排序[/yellow]")
        return

    await page.evaluate("document.querySelector('div.filter').click()")
    await random_delay(0.8, 1.2)

    # 等待 filter-panel 出现
    try:
        panel = await page.wait_for_selector("div.filter-panel", timeout=3000)
    except Exception:
        panel = None
    if not panel:
        console.print("  [yellow]筛选面板未打开，使用默认排序[/yellow]")
        return

    # 在 filter-panel 中点击排序选项和日期选项
    result = await page.evaluate("""
        ({sortText, dateText}) => {
            const panel = document.querySelector('div.filter-panel');
            if (!panel) return { sort: false, date: false };

            let sortClicked = false;
            let dateClicked = false;

            // 找到所有叶子节点（无子元素文本节点的包裹元素）
            const allEls = panel.querySelectorAll('div, span, button, a, li');
            for (const el of allEls) {
                const text = el.innerText?.trim();
                if (!text) continue;

                // 精确匹配排序选项
                if (text === sortText && !sortClicked) {
                    el.click();
                    sortClicked = true;
                }
                // 精确匹配日期选项
                if (text === dateText && !dateClicked) {
                    el.click();
                    dateClicked = true;
                }
            }

            return { sort: sortClicked, date: dateClicked };
        }
    """, {"sortText": sort_text, "dateText": date_text})

    if result.get("sort"):
        console.print(f"  [green]✓ 已选择排序: {sort_text}[/green]")
    else:
        console.print(f"  [yellow]未找到排序选项「{sort_text}」[/yellow]")

    if result.get("date"):
        console.print(f"  [green]✓ 已选择时间: {date_text}[/green]")
    else:
        console.print(f"  [yellow]未找到时间选项「{date_text}」[/yellow]")

    # 等待筛选生效（页面会重新加载结果）
    await random_delay(2, 3)


async def search_keyword(page, keyword: str, sort: str = "hot"):
    """
    在小红书搜索关键词并按指定方式排序
    排序和日期筛选都在「筛选」面板里（点击 div.filter → div.filter-panel）
    排序依据: 综合 / 最新 / 最多点赞 / 最多评论 / 最多收藏
    发布时间: 不限 / 一天内 / 一周内 / 半年内
    """
    selectors = _load_selectors()

    # 构建搜索 URL
    search_url = config.XIAOHONGSHU_SEARCH.format(keyword=keyword)
    console.print(f"  [cyan]搜索关键词: {keyword}[/cyan]")

    await navigate_to(page, search_url)
    await random_delay(*config.PAGE_LOAD_WAIT)

    # 如果搜索页弹出登录窗口，等待用户登录
    logged_in = await ensure_login_on_page(page)
    if not logged_in:
        console.print("[red]登录失败，无法继续搜索[/red]")
        return False

    await random_delay(1, 2)

    # 筛选：点击「筛选」按钮 → 打开 filter-panel → 选择排序和日期
    await _apply_filters(page, sort)


async def scrape_note_list(page, selectors: dict, count: int) -> list[dict]:
    """
    从搜索结果页抓取笔记列表的基础信息
    """
    notes = []
    note_selectors = selectors["search"]["note_item"].split(", ")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"抓取笔记中 (目标: {count} 篇)...", total=None)

        scroll_attempts = 0
        max_scroll_attempts = 20

        while len(notes) < count and scroll_attempts < max_scroll_attempts:
            # 查找页面上所有笔记元素
            note_elements = []
            for sel in note_selectors:
                try:
                    elements = await page.query_selector_all(sel)
                except Exception as e:
                    if "Target" in str(e) and "closed" in str(e):
                        console.print("  [red]浏览器页面已关闭，停止抓取[/red]")
                        return notes
                    raise
                if elements:
                    note_elements = elements
                    break

            # 提取每篇笔记的基础信息
            for element in note_elements:
                if len(notes) >= count:
                    break

                try:
                    # 获取笔记链接
                    link_el = await element.query_selector("a")
                    href = await extract_attribute(link_el, "href") if link_el else ""

                    # 跳过已抓取的
                    if any(n.get("url") == href for n in notes):
                        continue

                    # 获取标题
                    title_sels = selectors["search"].get("note_title", "a.title").split(", ")
                    title_el = None
                    for ts in title_sels:
                        title_el = await element.query_selector(ts)
                        if title_el:
                            break
                    if not title_el:
                        title_el = await element.query_selector("a, span")
                    title = await extract_text(title_el, "无标题")

                    # 获取互动数据（列表页可能只有点赞数）
                    like_sels = selectors["search"].get("note_like_count", ".like-wrapper .count").split(", ")
                    like_el = None
                    for ls in like_sels:
                        like_el = await element.query_selector(ls)
                        if like_el:
                            break
                    like_text = await extract_text(like_el, "0")

                    notes.append({
                        "title": title,
                        "url": href,
                        "like_count": parse_count(like_text),
                        "scraped_at": datetime.now().isoformat(),
                    })

                    progress.update(task, description=f"抓取笔记中 ({len(notes)}/{count})...")

                except Exception as e:
                    console.print(f"  [dim]跳过一条笔记: {e}[/dim]")
                    continue

            # 滚动加载更多
            if len(notes) < count:
                await smooth_scroll(page, distance=500, times=2)
                await random_delay(*config.SCRAPE_DELAY)
                scroll_attempts += 1

    console.print(f"  [green]✓ 共抓取到 {len(notes)} 篇笔记基础信息[/green]")
    return notes


async def scrape_note_detail_via_popup(page, note_element, selectors: dict) -> dict:
    """
    通过点击搜索结果中的笔记卡片弹出详情弹窗，抓取完整信息。
    小红书的搜索页是 SPA，点击笔记不会跳转页面，而是弹出 overlay 弹窗。

    Args:
        page: 当前搜索结果页面
        note_element: 笔记卡片的 DOM 元素句柄
        selectors: 选择器配置

    Returns:
        dict: 笔记详情数据
    """
    detail_selectors = selectors["note_detail"]
    note_data = {}

    ERROR_TEXTS = ["当前笔记暂时无法浏览", "笔记不存在", "内容已被删除", "页面不存在"]

    try:
        # 滚动到笔记卡片使其可见
        try:
            await note_element.scroll_into_view_if_needed(timeout=3000)
            await random_delay(0.3, 0.5)
        except Exception:
            try:
                await page.evaluate("(el) => el.scrollIntoView({block: 'center'})", note_element)
                await random_delay(0.3, 0.5)
            except Exception:
                pass

        # 点击笔记卡片 — 优先点击 a.cover（经实测最可靠）
        cover_el = await note_element.query_selector("a.cover")
        if not cover_el:
            cover_el = await note_element.query_selector("a, .cover")
        click_target = cover_el if cover_el else note_element

        try:
            await click_target.click(timeout=5000)
        except Exception:
            try:
                await page.evaluate("(el) => el.click()", click_target)
            except Exception as click_err:
                console.print(f"    [yellow]无法点击笔记: {click_err}[/yellow]")
                return note_data

        await random_delay(1.5, 2.5)

        # 等待弹窗/详情页出现 — 依次检查多种容器
        popup = None
        for sel_key in ["popup_mask", "popup_container", "note_scroller"]:
            sels = detail_selectors.get(sel_key, "").split(", ")
            sels = [s for s in sels if s]
            if sels:
                popup = await wait_for_any_selector(page, sels, timeout=3000)
            if popup:
                break

        if not popup:
            console.print(f"    [yellow]弹窗/详情页未打开[/yellow]")
            return note_data

        # 检测是否为错误页面
        try:
            page_text = await page.evaluate("() => document.body.innerText.substring(0, 500)")
            if any(err in page_text for err in ERROR_TEXTS):
                console.print(f"    [yellow]⚠ 该笔记无法浏览，跳过[/yellow]")
                note_data["detail_status"] = "web_restricted"
                return note_data
        except Exception:
            pass

        await random_delay(0.5, 1)

        # 捕获弹窗/详情页的 URL（是笔记的独立链接）
        detail_url = page.url
        if "/explore/" in detail_url:
            note_data["detail_url"] = detail_url

        # 抓取标题
        title_sels = detail_selectors["title"].split(", ")
        title_el = await wait_for_any_selector(page, title_sels, timeout=3000)
        if title_el:
            note_data["title"] = await extract_text(title_el, "")

        # 抓取正文
        content_sels = detail_selectors["content"].split(", ")
        content_el = await wait_for_any_selector(page, content_sels, timeout=3000)
        if content_el:
            note_data["content"] = await extract_text(content_el, "")

        # 抓取互动数据
        for field, sel_key in [
            ("like_count", "like_count"),
            ("collect_count", "collect_count"),
            ("comment_count", "comment_count"),
        ]:
            sels = detail_selectors[sel_key].split(", ")
            el = await wait_for_any_selector(page, sels, timeout=2000)
            if el:
                text = await extract_text(el, "0")
                note_data[field] = parse_count(text)

        # 抓取标签
        tag_sels = detail_selectors["tags"].split(", ")
        tags = []
        for sel in tag_sels:
            tag_elements = await page.query_selector_all(sel)
            for tag_el in tag_elements:
                tag_text = await extract_text(tag_el)
                if tag_text:
                    tag_text = tag_text.strip()
                    if not tag_text.startswith("#"):
                        tag_text = f"#{tag_text}"
                    tags.append(tag_text)
        note_data["tags"] = list(set(tags))

        # 抓取发布时间
        time_sels = detail_selectors["publish_time"].split(", ")
        time_el = await wait_for_any_selector(page, time_sels, timeout=2000)
        if time_el:
            note_data["publish_time"] = await extract_text(time_el, "")

        # 抓取作者
        author_sels = detail_selectors["author_name"].split(", ")
        author_el = await wait_for_any_selector(page, author_sels, timeout=2000)
        if author_el:
            note_data["author"] = await extract_text(author_el, "")

        note_data["detail_status"] = "ok"

    except Exception as e:
        console.print(f"    [yellow]抓取弹窗详情出错: {e}[/yellow]")
        note_data["detail_status"] = "error"

    return note_data


async def _close_detail_popup(page, detail_selectors: dict):
    """关闭笔记详情弹窗，回到搜索结果页"""
    # 方法1：点击 div.close-box（实测确认存在）
    close_sel = detail_selectors.get("close_button", "div.close-box")
    try:
        close_btn = await page.query_selector(close_sel)
        if close_btn and await close_btn.is_visible():
            await close_btn.click()
            await random_delay(0.5, 1)
            # 验证弹窗是否关闭
            mask = await page.query_selector(".note-detail-mask")
            if not mask:
                return
    except Exception:
        pass

    # 方法2：按 Escape（实测确认有效，且保留搜索结果 DOM）
    try:
        await page.keyboard.press("Escape")
        await random_delay(0.5, 1)
    except Exception:
        pass


def generate_analysis_report(notes: list[dict], keyword: str) -> str:
    """
    根据抓取的笔记数据，生成 Markdown 分析报告
    """
    report_lines = [
        f"# 小红书热门笔记分析报告",
        f"",
        f"- **搜索关键词**: {keyword}",
        f"- **分析笔记数**: {len(notes)} 篇",
        f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"---",
        f"",
    ]

    # ---- 1. 互动数据排行 ----
    report_lines.append("## 📊 互动数据 Top 10")
    report_lines.append("")

    sorted_by_like = sorted(notes, key=lambda x: x.get("like_count", 0), reverse=True)[:10]
    report_lines.append("| 排名 | 标题 | 👍 点赞 | ⭐ 收藏 | 💬 评论 |")
    report_lines.append("|------|------|---------|---------|---------|")
    for i, note in enumerate(sorted_by_like, 1):
        title = truncate_text(note.get("title", "无标题"), 30)
        like = note.get("like_count", 0)
        collect = note.get("collect_count", 0)
        comment = note.get("comment_count", 0)
        report_lines.append(f"| {i} | {title} | {like} | {collect} | {comment} |")
    report_lines.append("")

    # ---- 2. 高频关键词 ----
    report_lines.append("## 🔑 高频关键词 Top 20")
    report_lines.append("")

    all_text = " ".join(
        (note.get("title", "") + " " + note.get("content", ""))
        for note in notes
    )
    # 简单的中文分词（按标点和空格分割，过滤短词）
    words = re.findall(r'[\u4e00-\u9fff]{2,4}', all_text)
    # 过滤常见停用词
    stopwords = {"什么", "怎么", "这个", "那个", "一个", "可以", "就是", "真的",
                 "大家", "自己", "不是", "没有", "已经", "还是", "我们", "他们",
                 "知道", "觉得", "因为", "所以", "但是", "而且", "或者", "如果"}
    filtered = [w for w in words if w not in stopwords]
    word_freq = Counter(filtered).most_common(20)

    report_lines.append("| 排名 | 关键词 | 出现次数 |")
    report_lines.append("|------|--------|----------|")
    for i, (word, freq) in enumerate(word_freq, 1):
        report_lines.append(f"| {i} | {word} | {freq} |")
    report_lines.append("")

    # ---- 3. 标题模式分析 ----
    report_lines.append("## 📝 标题模式分析")
    report_lines.append("")

    titles = [note.get("title", "") for note in notes if note.get("title")]
    avg_title_len = sum(len(t) for t in titles) / len(titles) if titles else 0
    report_lines.append(f"- **平均标题长度**: {avg_title_len:.0f} 字")

    # 标题中常见句式
    question_titles = sum(1 for t in titles if "?" in t or "？" in t or "吗" in t)
    number_titles = sum(1 for t in titles if re.search(r'\d+', t))
    emoji_titles = sum(1 for t in titles if re.search(r'[^\w\s\u4e00-\u9fff]', t))
    report_lines.append(f"- **疑问句标题**: {question_titles} 篇 ({question_titles/len(titles)*100:.0f}%)")
    report_lines.append(f"- **含数字标题**: {number_titles} 篇 ({number_titles/len(titles)*100:.0f}%)")
    report_lines.append(f"- **含 Emoji 标题**: {emoji_titles} 篇 ({emoji_titles/len(titles)*100:.0f}%)")
    report_lines.append("")

    # ---- 4. 标签策略 ----
    report_lines.append("## 🏷️ 标签使用策略")
    report_lines.append("")

    all_tags = []
    for note in notes:
        all_tags.extend(note.get("tags", []))
    tag_freq = Counter(all_tags).most_common(15)

    if tag_freq:
        avg_tags = sum(len(note.get("tags", [])) for note in notes) / len(notes) if notes else 0
        report_lines.append(f"- **平均每篇标签数**: {avg_tags:.1f}")
        report_lines.append(f"- **最热门标签**:")
        report_lines.append("")
        report_lines.append("| 标签 | 使用次数 |")
        report_lines.append("|------|----------|")
        for tag, freq in tag_freq:
            report_lines.append(f"| {tag} | {freq} |")
    else:
        report_lines.append("- 未抓取到标签数据")
    report_lines.append("")

    # ---- 5. 内容长度分析 ----
    report_lines.append("## 📏 内容长度与互动率关系")
    report_lines.append("")

    notes_with_content = [n for n in notes if n.get("content")]
    if notes_with_content:
        short = [n for n in notes_with_content if len(n["content"]) < 200]
        medium = [n for n in notes_with_content if 200 <= len(n["content"]) < 500]
        long = [n for n in notes_with_content if len(n["content"]) >= 500]

        def avg_engagement(group):
            if not group:
                return 0
            return sum(n.get("like_count", 0) + n.get("collect_count", 0) + n.get("comment_count", 0) for n in group) / len(group)

        report_lines.append("| 内容长度 | 笔记数 | 平均互动量 |")
        report_lines.append("|----------|--------|------------|")
        report_lines.append(f"| 短 (<200字) | {len(short)} | {avg_engagement(short):.0f} |")
        report_lines.append(f"| 中 (200-500字) | {len(medium)} | {avg_engagement(medium):.0f} |")
        report_lines.append(f"| 长 (>500字) | {len(long)} | {avg_engagement(long):.0f} |")
    else:
        report_lines.append("- 未抓取到正文内容，无法分析")
    report_lines.append("")

    # ---- 6. 创作建议 ----
    report_lines.append("## 💡 创作建议")
    report_lines.append("")

    if word_freq:
        top_keywords = "、".join(w for w, _ in word_freq[:5])
        report_lines.append(f"1. **关键词热点**: 围绕「{top_keywords}」等高频词创作")
    report_lines.append(f"2. **标题长度**: 建议控制在 {max(10, int(avg_title_len - 5))}-{int(avg_title_len + 5)} 字")
    if number_titles > len(titles) * 0.3:
        report_lines.append("3. **数字标题**: 该领域含数字的标题效果好，建议使用具体数据")
    if emoji_titles > len(titles) * 0.3:
        report_lines.append("4. **Emoji 使用**: 该领域 Emoji 使用率高，建议适当添加")
    if tag_freq:
        top_tags = "、".join(t for t, _ in tag_freq[:5])
        report_lines.append(f"5. **推荐标签**: {top_tags}")
    report_lines.append("")

    return "\n".join(report_lines)


async def analyze(keyword: str, count: int = 20, sort: str = "hot", output: str = None):
    """
    主分析流程
    """
    config.ensure_dirs()
    selectors = _load_selectors()

    console.print(Panel(
        f"🔍 小红书热门笔记分析\n"
        f"   关键词: {keyword}\n"
        f"   数量: {count} 篇\n"
        f"   排序: {'最热' if sort == 'hot' else '最新'}",
        style="bold cyan",
    ))

    # 启动浏览器
    context, page = await launch_browser()

    try:
        # 登录（统一由 browser_helper 处理）
        logged_in = await ensure_login(page)
        if not logged_in:
            console.print("[red]未能登录，退出[/red]")
            return

        # 搜索
        search_result = await search_keyword(page, keyword, sort)
        if search_result is False:
            console.print("[red]搜索失败（可能未登录），退出[/red]")
            return

        # 抓取笔记列表
        try:
            notes = await scrape_note_list(page, selectors, count)
        except Exception as e:
            if "Target" in str(e) and "closed" in str(e):
                console.print("[red]浏览器意外关闭，请重新运行[/red]")
                return
            raise

        if not notes:
            console.print("[red]未抓取到任何笔记，请检查搜索关键词或网络[/red]")
            return

        # 逐篇点击笔记弹窗抓取详情
        console.print(f"\n[cyan]正在逐篇点击笔记抓取详情 ({len(notes)} 篇)...[/cyan]")
        search_url = config.XIAOHONGSHU_SEARCH.format(keyword=keyword)
        detail_selectors = selectors["note_detail"]
        note_item_sels = selectors["search"]["note_item"].split(", ")

        for i, note in enumerate(notes):
            note_url = note.get('url', '')
            # 构建完整笔记 URL 用于显示
            if note_url and not note_url.startswith('http'):
                full_note_url = f"https://www.xiaohongshu.com{note_url}"
            else:
                full_note_url = note_url
            console.print(f"  [{i + 1}/{len(notes)}] {truncate_text(note.get('title', ''), 40)}")
            if full_note_url:
                console.print(f"    [dim]URL: {full_note_url}[/dim]")

            try:
                # 确保在搜索页上（每次循环都回到搜索页）
                current_url = page.url
                if "search_result" not in current_url:
                    await navigate_to(page, search_url)
                    await random_delay(1, 2)

                # 重新查找笔记元素（每次都查，因为 DOM 可能重建了）
                note_elements = []
                for sel in note_item_sels:
                    note_elements = await page.query_selector_all(sel)
                    if note_elements:
                        break

                # 找到与当前 note 对应的元素
                target_el = None
                note_url = note.get("url", "")

                # 方法1：通过 URL 匹配
                if note_url:
                    for el in note_elements:
                        link_el = await el.query_selector("a")
                        if link_el:
                            href = await extract_attribute(link_el, "href")
                            if href and note_url in href:
                                target_el = el
                                break

                # 方法2：如果 URL 匹配失败，通过标题匹配
                if not target_el and note.get("title"):
                    for el in note_elements:
                        el_text = await extract_text(el, "")
                        if note.get("title", "NOMATCH") in el_text:
                            target_el = el
                            break

                # 方法3：按位置（最后手段）
                if not target_el and i < len(note_elements):
                    target_el = note_elements[i]

                if not target_el:
                    console.print(f"    [yellow]未找到对应元素，跳过[/yellow]")
                    continue

                # 点击并抓取详情
                detail_data = await scrape_note_detail_via_popup(page, target_el, selectors)

                # 合并弹窗数据到列表数据
                if detail_data:
                    for key, value in detail_data.items():
                        if value:
                            note[key] = value

                # 关闭弹窗 / 回到搜索结果页
                await _close_detail_popup(page, detail_selectors)

            except Exception as e:
                if "Target" in str(e) and "closed" in str(e):
                    console.print("  [red]浏览器已关闭，停止详情抓取[/red]")
                    break
                console.print(f"    [yellow]详情抓取出错: {e}[/yellow]")

            await random_delay(*config.SCRAPE_DELAY)

        # 保存原始数据
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = config.OUTPUT_DIR / f"notes_{keyword}_{timestamp}.json"
        save_to_json(notes, json_path)

        # 生成分析报告
        report = generate_analysis_report(notes, keyword)
        report_path = Path(output) if output else config.OUTPUT_DIR / f"report_{keyword}_{timestamp}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        console.print(f"\n[green]✓ 分析报告已保存: {report_path}[/green]")

        # 在终端展示摘要
        _print_summary(notes, keyword)

    finally:
        await close_browser(context)


def _print_summary(notes: list[dict], keyword: str):
    """在终端打印分析摘要"""
    table = Table(title=f"🔥 「{keyword}」热门笔记 Top 5", show_lines=True)
    table.add_column("标题", style="cyan", max_width=35)
    table.add_column("👍", justify="right", style="green")
    table.add_column("⭐", justify="right", style="yellow")
    table.add_column("💬", justify="right", style="blue")
    table.add_column("URL", style="dim", max_width=40)

    # 按 URL 去重
    seen_urls = set()
    unique_notes = []
    for note in notes:
        url = note.get("url", "")
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        unique_notes.append(note)

    sorted_notes = sorted(unique_notes, key=lambda x: x.get("like_count", 0), reverse=True)[:5]
    for note in sorted_notes:
        note_url = note.get("url", "")
        if note_url and not note_url.startswith("http"):
            note_url = f"https://www.xiaohongshu.com{note_url}"
        # 截断 URL 显示（去掉 query params）
        display_url = note_url.split("?")[0] if note_url else ""
        table.add_row(
            truncate_text(note.get("title", ""), 35),
            str(note.get("like_count", 0)),
            str(note.get("collect_count", 0)),
            str(note.get("comment_count", 0)),
            display_url,
        )

    console.print()
    console.print(table)


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="小红书热门笔记分析工具")
    parser.add_argument("--keyword", "-k", required=True, help="搜索关键词")
    parser.add_argument("--count", "-c", type=int, default=config.DEFAULT_ARTICLE_COUNT,
                        help=f"抓取笔记数量 (默认: {config.DEFAULT_ARTICLE_COUNT})")
    parser.add_argument("--sort", "-s", choices=["hot", "new"], default="hot",
                        help="排序方式: hot(最热), new(最新) (默认: hot)")
    parser.add_argument("--output", "-o", help="分析报告输出路径 (默认: output/report_<keyword>_<time>.md)")

    args = parser.parse_args()

    # 限制数量
    count = min(args.count, config.MAX_ARTICLE_COUNT)

    asyncio.run(analyze(
        keyword=args.keyword,
        count=count,
        sort=args.sort,
        output=args.output,
    ))


if __name__ == "__main__":
    main()

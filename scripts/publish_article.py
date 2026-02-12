"""
自动发布文章脚本
通过浏览器自动化在小红书创作者中心发布笔记
发布前自动执行内容质量检查（去 AI 味、事实核验、情绪密度）
"""

import argparse
import asyncio
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

import config
from browser_helper import launch_browser, ensure_login, close_browser, navigate_to
from content_checker import check_content, print_check_result
from utils import random_delay, human_type, safe_click, wait_for_any_selector

console = Console()


def _load_selectors() -> dict:
    """加载选择器配置"""
    with open(config.SELECTORS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def _read_content_file(filepath: str) -> str:
    """
    读取 Markdown 内容文件
    去掉 Markdown 语法标记，保留纯文本（小红书编辑器不支持 Markdown）
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {filepath}")

    content = path.read_text(encoding='utf-8')

    # 去掉 YAML frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()

    # 去掉 Markdown 标题标记
    import re
    content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)
    # 去掉加粗/斜体标记
    content = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', content)
    # 去掉链接，保留文本
    content = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', content)
    # 去掉图片标记
    content = re.sub(r'!\[([^\]]*)\]\([^\)]+\)', '', content)

    return content.strip()


async def fill_title(page, title: str, selectors: dict):
    """填写笔记标题"""
    console.print(f"  [cyan]填写标题: {title}[/cyan]")

    title_sels = selectors["publish"]["title_input"].split(", ")
    title_el = await wait_for_any_selector(page, title_sels, timeout=10000)

    if title_el:
        await title_el.click()
        await random_delay(0.3, 0.6)
        # 清空已有内容
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await random_delay(0.2, 0.4)
        # 逐字输入标题
        await human_type(page, title_sels[0], title)
    else:
        console.print("  [red]❌ 未找到标题输入框[/red]")
        raise Exception("未找到标题输入框")


async def fill_content(page, content: str, selectors: dict):
    """填写笔记正文"""
    console.print(f"  [cyan]填写正文 ({len(content)} 字)...[/cyan]")

    content_sels = selectors["publish"]["content_input"].split(", ")
    content_el = await wait_for_any_selector(page, content_sels, timeout=10000)

    if content_el:
        await content_el.click()
        await random_delay(0.3, 0.6)

        # 分段输入正文（避免一次性输入大量文本被检测）
        paragraphs = content.split("\n")
        for i, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                await page.keyboard.press("Enter")
                await random_delay(0.1, 0.3)
                continue

            # 逐字输入
            for char in para:
                await page.keyboard.type(char)
                await asyncio.sleep(0.02 + 0.08 * (hash(char) % 10) / 10)

            # 段落之间按回车
            if i < len(paragraphs) - 1:
                await page.keyboard.press("Enter")
                await random_delay(0.2, 0.5)

        console.print("  [green]✓ 正文已填写[/green]")
    else:
        console.print("  [red]❌ 未找到正文输入框[/red]")
        raise Exception("未找到正文输入框")


async def add_tags(page, tags: list[str], selectors: dict):
    """添加标签/话题"""
    if not tags:
        return

    console.print(f"  [cyan]添加标签: {', '.join(tags)}[/cyan]")

    tag_input_sels = selectors["publish"]["tag_input"].split(", ")

    for tag in tags:
        tag = tag.strip().lstrip("#")
        if not tag:
            continue

        try:
            # 在正文中输入 # 号触发话题选择
            content_sels = selectors["publish"]["content_input"].split(", ")
            content_el = await wait_for_any_selector(page, content_sels, timeout=5000)

            if content_el:
                await content_el.click()
                # 移动到末尾
                await page.keyboard.press("End")
                await random_delay(0.3, 0.5)

                # 输入空格 + #
                await page.keyboard.type(" #")
                await random_delay(0.5, 1.0)

                # 输入标签文字
                await page.keyboard.type(tag)
                await random_delay(1, 2)

                # 尝试点击话题建议
                suggestion_sels = selectors["publish"]["tag_suggestion"].split(", ")
                suggestion = await wait_for_any_selector(page, suggestion_sels, timeout=3000)
                if suggestion:
                    await suggestion.click()
                    await random_delay(0.5, 1.0)
                else:
                    # 没有建议，按空格确认
                    await page.keyboard.press("Space")
                    await random_delay(0.3, 0.5)

            console.print(f"    [green]✓ 标签: #{tag}[/green]")

        except Exception as e:
            console.print(f"    [yellow]⚠ 标签 #{tag} 添加失败: {e}[/yellow]")

        await random_delay(*config.ACTION_DELAY)


async def upload_cover(page, cover_path: str, selectors: dict):
    """上传封面图"""
    if not cover_path:
        return

    path = Path(cover_path)
    if not path.exists():
        console.print(f"  [yellow]⚠ 封面图不存在: {cover_path}[/yellow]")
        return

    console.print(f"  [cyan]上传封面图: {path.name}[/cyan]")

    upload_sels = selectors["publish"]["cover_upload"].split(", ")
    upload_el = await wait_for_any_selector(page, upload_sels, timeout=10000)

    if upload_el:
        await upload_el.set_input_files(str(path))
        await random_delay(3, 5)  # 等待上传完成
        console.print("  [green]✓ 封面图已上传[/green]")
    else:
        console.print("  [yellow]⚠ 未找到上传按钮[/yellow]")


async def click_publish_or_draft(page, selectors: dict, draft: bool = False):
    """点击发布或保存草稿"""
    if draft:
        console.print("  [cyan]保存为草稿...[/cyan]")
        btn_sels = selectors["publish"]["draft_button"].split(", ")
    else:
        console.print("  [cyan]准备发布...[/cyan]")
        btn_sels = selectors["publish"]["publish_button"].split(", ")

    btn = await wait_for_any_selector(page, btn_sels, timeout=10000)
    if btn:
        await random_delay(1, 2)
        await btn.click()
        await random_delay(2, 3)

        # 处理可能的确认弹窗
        confirm_sels = selectors["publish"]["confirm_dialog_ok"].split(", ")
        confirm = await wait_for_any_selector(page, confirm_sels, timeout=3000)
        if confirm:
            await confirm.click()
            await random_delay(1, 2)

        action = "草稿保存" if draft else "发布"
        console.print(f"  [green]✓ {action}成功！[/green]")
    else:
        action = "草稿" if draft else "发布"
        console.print(f"  [red]❌ 未找到{action}按钮[/red]")


async def publish(
    title: str,
    content: str,
    tags: list[str] = None,
    cover: str = None,
    draft: bool = False,
    skip_check: bool = False,
    user_facts: dict = None,
):
    """
    主发布流程
    发布前自动执行内容质量检查
    """
    config.ensure_dirs()
    selectors = _load_selectors()

    action = "保存草稿" if draft else "发布笔记"
    console.print(Panel(
        f"📝 小红书{action}\n"
        f"   标题: {title}\n"
        f"   正文: {len(content)} 字\n"
        f"   标签: {', '.join(tags) if tags else '无'}\n"
        f"   封面: {cover if cover else '无'}\n"
        f"   模式: {'草稿' if draft else '直接发布'}",
        style="bold cyan",
    ))

    # ========== 内容质量检查 ==========
    if not skip_check:
        console.print("\n[bold]🔍 正在检查内容质量...[/bold]")
        check_result = check_content(title, content, user_provided_facts=user_facts)
        print_check_result(check_result)

        if not check_result.passed:
            console.print(Panel(
                "❌ 内容质量检查未通过\n"
                "   检测到 AI 感表达或其他严重问题\n"
                "   请修改后重试，或使用 --skip-check 跳过检查",
                style="red",
            ))
            return

        if check_result.warnings:
            console.print("[yellow]⚠️ 存在一些警告，建议优化后再发布[/yellow]")
            proceed = Confirm.ask("是否继续发布？", default=True)
            if not proceed:
                console.print("[dim]已取消发布[/dim]")
                return

        console.print("[green]✅ 内容质量检查通过[/green]\n")

    # 启动浏览器
    context, page = await launch_browser()

    try:
        # 检查登录
        is_logged_in = await check_login_status(page)
        if not is_logged_in:
            success = await wait_for_login(page)
            if not success:
                console.print("[red]未能登录，退出[/red]")
                return

        # 导航到发布页面
        console.print("\n[cyan]正在打开发布页面...[/cyan]")
        await navigate_to(page, config.XIAOHONGSHU_PUBLISH)
        await random_delay(*config.PAGE_LOAD_WAIT)

        # 上传封面图（通常需要先上传图片才能继续编辑）
        await upload_cover(page, cover, selectors)

        # 填写标题
        await fill_title(page, title, selectors)
        await random_delay(*config.ACTION_DELAY)

        # 填写正文
        await fill_content(page, content, selectors)
        await random_delay(*config.ACTION_DELAY)

        # 添加标签
        if tags:
            await add_tags(page, tags, selectors)
            await random_delay(*config.ACTION_DELAY)

        # 发布前的最终停顿（给用户一个检查窗口）
        console.print(Panel(
            "⏳ 3 秒后将执行" + ("保存草稿" if draft else "发布") + "操作...\n"
            "   请在浏览器中检查内容是否正确",
            style="yellow",
        ))
        await asyncio.sleep(3)

        # 发布或保存草稿
        await click_publish_or_draft(page, selectors, draft)

        console.print(Panel(
            f"✅ {action}完成！\n"
            f"   请在小红书 App 或 Web 端确认",
            style="green",
        ))

    except Exception as e:
        console.print(f"\n[red]❌ {action}过程中出错: {e}[/red]")
        console.print("[yellow]提示: 请检查浏览器中的页面状态[/yellow]")

    finally:
        # 稍等一下，让用户看到结果
        await asyncio.sleep(2)
        await close_browser(context)


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="小红书自动发布笔记工具")

    parser.add_argument("--title", "-t", required=True, help="笔记标题")

    content_group = parser.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content", "-c", help="正文内容")
    content_group.add_argument("--content-file", "-f", help="正文 Markdown 文件路径")

    parser.add_argument("--tags", help="标签，逗号分隔 (例: 美食,推荐,探店)")
    parser.add_argument("--cover", help="封面图路径")
    parser.add_argument("--draft", "-d", action="store_true", default=False,
                        help="仅保存草稿，不直接发布")
    parser.add_argument("--skip-check", action="store_true", default=False,
                        help="跳过内容质量检查")
    parser.add_argument("--facts", help='用户提供的真实事实 JSON (例: \'{"prices": ["68"], "places": ["xx咖啡馆"]}\')')

    args = parser.parse_args()

    # 处理正文内容
    if args.content_file:
        content = _read_content_file(args.content_file)
    else:
        content = args.content

    # 处理标签
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []

    # 解析用户提供的事实
    user_facts = json.loads(args.facts) if args.facts else None

    asyncio.run(publish(
        title=args.title,
        content=content,
        tags=tags,
        cover=args.cover,
        draft=args.draft,
        skip_check=args.skip_check,
        user_facts=user_facts,
    ))


if __name__ == "__main__":
    main()

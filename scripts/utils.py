"""
通用工具函数
提供反检测延时、模拟人工输入、安全点击、数据处理等通用功能
"""

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()


async def random_delay(min_s: float = 1, max_s: float = 3):
    """随机等待，模拟人工操作节奏"""
    delay = random.uniform(min_s, max_s)
    await asyncio.sleep(delay)


async def human_type(page, selector: str, text: str, char_delay: tuple = (0.05, 0.15)):
    """
    模拟人工逐字输入
    每个字符之间随机延迟，避免被检测为自动化输入
    """
    element = await page.wait_for_selector(selector, timeout=10000)
    await element.click()
    await random_delay(0.3, 0.6)

    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(*char_delay))
    
    console.print(f"  [dim]已输入文本: {text[:30]}{'...' if len(text) > 30 else ''}[/dim]")


async def safe_click(page, selector: str, timeout: int = 10000, retry: int = 3):
    """
    带等待和重试的安全点击
    先等待元素出现，再执行点击，失败后自动重试
    """
    for attempt in range(retry):
        try:
            element = await page.wait_for_selector(selector, timeout=timeout)
            await element.scroll_into_view_if_needed()
            await random_delay(0.3, 0.8)
            await element.click()
            console.print(f"  [dim]已点击: {selector[:50]}[/dim]")
            return True
        except Exception as e:
            if attempt < retry - 1:
                console.print(f"  [yellow]点击重试 ({attempt + 1}/{retry}): {selector[:50]}[/yellow]")
                await random_delay(1, 2)
            else:
                console.print(f"  [red]点击失败: {selector[:50]} - {e}[/red]")
                return False


async def extract_text(element, default: str = "") -> str:
    """安全提取元素文本内容"""
    try:
        if element is None:
            return default
        text = await element.text_content()
        return text.strip() if text else default
    except Exception:
        return default


async def extract_attribute(element, attr: str, default: str = "") -> str:
    """安全提取元素属性"""
    try:
        if element is None:
            return default
        value = await element.get_attribute(attr)
        return value.strip() if value else default
    except Exception:
        return default


def parse_count(text: str) -> int:
    """
    解析数量文本为整数
    处理 "1.2万"、"10w"、"1k" 等格式
    """
    if not text:
        return 0
    
    text = text.strip().lower()
    text = re.sub(r'[,\s]', '', text)

    try:
        if '万' in text or 'w' in text:
            num = float(re.sub(r'[万w]', '', text))
            return int(num * 10000)
        elif '千' in text or 'k' in text:
            num = float(re.sub(r'[千k]', '', text))
            return int(num * 1000)
        elif '亿' in text:
            num = float(text.replace('亿', ''))
            return int(num * 100000000)
        else:
            return int(float(re.sub(r'[^0-9.]', '', text) or '0'))
    except (ValueError, TypeError):
        return 0


def save_to_json(data, filepath: str | Path):
    """保存数据到 JSON 文件"""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    console.print(f"  [green]数据已保存: {filepath}[/green]")


def load_from_json(filepath: str | Path) -> Optional[dict | list]:
    """从 JSON 文件加载数据"""
    filepath = Path(filepath)
    if not filepath.exists():
        console.print(f"  [yellow]文件不存在: {filepath}[/yellow]")
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


async def smooth_scroll(page, distance: int = 300, times: int = 3, delay: tuple = (0.5, 1.5)):
    """
    模拟人工平滑滚动
    每次滚动随机距离，带随机延迟
    """
    for _ in range(times):
        scroll_amount = random.randint(distance - 100, distance + 100)
        await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        await asyncio.sleep(random.uniform(*delay))


async def wait_for_any_selector(page, selectors: list[str], timeout: int = 10000):
    """
    等待多个选择器中的任意一个出现
    用于处理页面元素不确定的情况（多个备选选择器）
    """
    combined = ", ".join(selectors)
    try:
        element = await page.wait_for_selector(combined, timeout=timeout)
        return element
    except Exception:
        # 逐个尝试
        for sel in selectors:
            try:
                element = await page.query_selector(sel)
                if element:
                    return element
            except Exception:
                continue
        return None


def truncate_text(text: str, max_length: int = 100) -> str:
    """截断文本，超出部分用省略号替代"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."

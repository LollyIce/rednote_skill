"""
浏览器生命周期管理
使用 Playwright 启动 Chrome，支持持久化 Context 复用登录态
提供统一的 ensure_login() 入口，其他脚本只需调用此函数即可
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page
from rich.console import Console
from rich.panel import Panel

import config
from utils import random_delay, wait_for_any_selector

console = Console()


def _load_selectors() -> dict:
    """加载选择器配置"""
    with open(config.SELECTORS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


# ============================================================
# 浏览器启动与关闭
# ============================================================

async def launch_browser() -> tuple[BrowserContext, Page]:
    """
    启动浏览器并返回 context 和 page
    使用持久化 Context + Chrome channel，复用登录态
    """
    config.ensure_dirs()

    console.print(Panel("🚀 正在启动浏览器...", style="blue"))

    playwright = await async_playwright().start()

    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.BROWSER_USER_DATA_DIR),
            channel="chrome",
            headless=False,
            viewport={
                "width": config.VIEWPORT_WIDTH,
                "height": config.VIEWPORT_HEIGHT,
            },
            user_agent=config.USER_AGENT,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
            ],
        )
    except Exception as e:
        console.print(f"  [yellow]Chrome 通道启动失败 ({e})，回退到 Chromium[/yellow]")
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.BROWSER_USER_DATA_DIR),
            headless=False,
            viewport={
                "width": config.VIEWPORT_WIDTH,
                "height": config.VIEWPORT_HEIGHT,
            },
            user_agent=config.USER_AGENT,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
            ],
        )

    # 注入反检测脚本
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
        );
    """)

    pages = context.pages
    page = pages[0] if pages else await context.new_page()

    console.print("  [green]✓ 浏览器已启动[/green]")

    context._playwright_instance = playwright
    return context, page


async def close_browser(context: BrowserContext):
    """安全关闭浏览器"""
    try:
        await context.close()
        if hasattr(context, '_playwright_instance'):
            await context._playwright_instance.stop()
        console.print("  [green]✓ 浏览器已关闭[/green]")
    except Exception as e:
        console.print(f"  [yellow]关闭浏览器时出错: {e}[/yellow]")


async def navigate_to(page: Page, url: str, wait_until: str = "domcontentloaded"):
    """安全导航到指定 URL"""
    console.print(f"  [dim]导航到: {url[:80]}[/dim]")
    await page.goto(url, wait_until=wait_until)
    await random_delay(*config.PAGE_LOAD_WAIT)


# ============================================================
# 登录检测（内部函数）
# ============================================================

async def _has_login_popup(page: Page) -> bool:
    """
    检测当前页面是否有登录弹窗
    这是最可靠的「未登录」信号 — 如果有登录弹窗，一定没登录
    """
    try:
        return await page.evaluate("""
            () => {
                // 查找含有登录相关文本的弹窗
                const loginTexts = ['扫码登录', '手机号登录', '密码登录', '短信登录', '其他登录方式'];
                const allElements = document.querySelectorAll('div, section, form');
                for (const el of allElements) {
                    if (el.offsetParent === null) continue;
                    const text = el.innerText || '';
                    if (loginTexts.some(t => text.includes(t))) {
                        const style = window.getComputedStyle(el);
                        const zIndex = parseInt(style.zIndex) || 0;
                        // 确认是弹窗（fixed/absolute/高 z-index）
                        if (zIndex > 100 || style.position === 'fixed' || style.position === 'absolute') {
                            return true;
                        }
                        let parent = el.parentElement;
                        while (parent) {
                            const pStyle = window.getComputedStyle(parent);
                            if (pStyle.position === 'fixed' || parseInt(pStyle.zIndex) > 100) {
                                return true;
                            }
                            parent = parent.parentElement;
                        }
                    }
                }
                // 检查 QR 码
                const qrImgs = document.querySelectorAll('img[src*="qrcode"], .qrcode-img, canvas.qr-code');
                for (const img of qrImgs) {
                    if (img.offsetParent !== null) return true;
                }
                return false;
            }
        """)
    except Exception:
        return False


async def _is_logged_in(page: Page) -> bool:
    """
    在当前页面检测是否已登录
    使用反向检测优先：有登录弹窗 → 一定没登录
    然后正向检测：cookie 中有 auth token → 已登录
    """
    # 反向检测：如果有登录弹窗 → 未登录
    has_popup = await _has_login_popup(page)
    if has_popup:
        return False

    # 正向检测：检查 cookie
    try:
        cookies = await page.context.cookies("https://www.xiaohongshu.com")
        auth_cookie_names = ["web_session", "galaxy_creator_session_id", "xsecappid", "a1"]
        for cookie in cookies:
            if cookie["name"] in auth_cookie_names and cookie["value"]:
                return True
    except Exception:
        pass

    # 备用正向检测：用 JS 检查 localStorage 或页面状态
    try:
        result = await page.evaluate("""
            () => {
                // 检查是否有显示用户名的元素
                const userEl = document.querySelector('.user-name, .nickname, .name');
                if (userEl && userEl.innerText && userEl.innerText.length > 0) return true;

                // 检查是否有登录按钮（有 → 未登录）
                const allButtons = document.querySelectorAll('button, .login-btn');
                for (const btn of allButtons) {
                    if (btn.textContent && btn.textContent.trim() === '登录') return false;
                }

                return false;
            }
        """)
        return result
    except Exception:
        return False


# ============================================================
# 统一登录入口
# ============================================================

async def ensure_login(page: Page, timeout: int = 180) -> bool:
    """
    统一登录入口 — 所有脚本只需调用此函数。

    流程：
    1. 导航到小红书首页
    2. 等页面加载完毕后检测登录状态
    3. 如果已登录 → 返回 True
    4. 如果有登录弹窗 → 提示用户扫码 → 等待弹窗消失 → 验证
    5. 如果未登录且无弹窗 → 刷新触发登录弹窗 → 等待用户操作
    """
    console.print("  [dim]检查登录状态...[/dim]")

    # 导航到首页
    try:
        await page.goto(config.XIAOHONGSHU_HOME, wait_until="domcontentloaded")
        await random_delay(2, 3)
        # 额外等待 SPA 渲染
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    except Exception as e:
        console.print(f"  [yellow]导航失败: {e}[/yellow]")

    # 检测是否已登录
    logged_in = await _is_logged_in(page)
    if logged_in:
        console.print("  [green]✓ 已登录[/green]")
        return True

    # 未登录 — 等待用户操作
    console.print(Panel(
        "🔐 请在浏览器中登录小红书\n"
        "   支持: 扫码登录 / 手机号登录 / 密码登录\n"
        f"   等待超时: {timeout} 秒",
        style="yellow",
    ))

    elapsed = 0
    poll_interval = 3
    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        # 检查是否登录成功
        logged_in = await _is_logged_in(page)
        if logged_in:
            console.print(Panel("✅ 登录成功！Session 已保存", style="green"))
            return True

        # 每 15 秒提示一次 + 刷新页面（扫码后可能需要刷新）
        if elapsed % 15 == 0:
            remaining = timeout - elapsed
            console.print(f"  [dim]等待登录中... 剩余 {remaining} 秒[/dim]")
            try:
                await page.reload(wait_until="domcontentloaded")
                await random_delay(2, 3)
                # 刷新后再检测
                logged_in = await _is_logged_in(page)
                if logged_in:
                    console.print(Panel("✅ 登录成功！Session 已保存", style="green"))
                    return True
            except Exception:
                pass

    console.print(Panel("❌ 登录超时，请重新运行", style="red"))
    return False


async def ensure_login_on_page(page: Page, timeout: int = 120) -> bool:
    """
    在当前页面（如搜索页）检测登录弹窗，仅在需要时等待登录。
    不会导航到首页 — 适用于已在目标页面上的场景。
    """
    has_popup = await _has_login_popup(page)
    if not has_popup:
        return True

    console.print(Panel(
        "🔐 页面弹出了登录窗口，请在浏览器中登录\n"
        "   登录成功后弹窗会自动关闭，脚本将继续\n"
        f"   等待超时: {timeout} 秒",
        style="yellow",
    ))

    elapsed = 0
    poll_interval = 3
    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        has_popup = await _has_login_popup(page)
        if not has_popup:
            # 弹窗消失，验证登录
            await random_delay(1, 2)
            logged_in = await _is_logged_in(page)
            if logged_in:
                console.print(Panel("✅ 登录成功！继续执行...", style="green"))
                return True
            # 弹窗关了但没登录，刷新试试
            try:
                await page.reload(wait_until="domcontentloaded")
                await random_delay(2, 3)
            except Exception:
                pass

        if elapsed % 15 == 0:
            remaining = timeout - elapsed
            if remaining > 0:
                console.print(f"  [dim]等待登录中... 剩余 {remaining} 秒[/dim]")

    console.print(Panel("❌ 登录超时", style="red"))
    return False


# ============================================================
# 独立运行入口 - 用于首次登录
# ============================================================

async def main():
    """独立运行：启动浏览器并等待用户登录"""
    console.print(Panel(
        "🌟 小红书 Skill - 浏览器登录助手\n"
        "   首次使用请在打开的浏览器中登录小红书",
        style="bold blue",
    ))

    context, page = await launch_browser()

    try:
        success = await ensure_login(page, timeout=180)
        if success:
            console.print(Panel(
                "✅ 登录成功！Session 已保存\n"
                "   后续运行将自动复用登录态",
                style="green",
            ))
        else:
            console.print(Panel("❌ 登录失败，请重试", style="red"))
    finally:
        await close_browser(context)


if __name__ == "__main__":
    asyncio.run(main())

"""
小红书 Skill 配置管理
集中管理所有 URL、路径、延时参数等配置项
"""

import os
from pathlib import Path

# ============================================================
# 路径配置
# ============================================================

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 浏览器用户数据目录（持久化登录态）
BROWSER_USER_DATA_DIR = PROJECT_ROOT / ".browser_data"

# 输出目录
OUTPUT_DIR = PROJECT_ROOT / "output"

# 资源目录
RESOURCES_DIR = PROJECT_ROOT / "resources"

# 选择器配置文件
SELECTORS_FILE = RESOURCES_DIR / "selectors.json"

# ============================================================
# 小红书 URL 配置
# ============================================================

# 小红书首页
XIAOHONGSHU_HOME = "https://www.xiaohongshu.com"

# 小红书搜索页 URL 模板
XIAOHONGSHU_SEARCH = "https://www.xiaohongshu.com/search_result?keyword={keyword}&type=51"

# 小红书创作者中心 - 发布笔记
XIAOHONGSHU_PUBLISH = "https://creator.xiaohongshu.com/publish/publish"

# 小红书创作者中心首页
XIAOHONGSHU_CREATOR = "https://creator.xiaohongshu.com"

# 小红书探索/发现页
XIAOHONGSHU_EXPLORE = "https://www.xiaohongshu.com/explore"

# 写作质量指南
WRITING_GUIDELINES_FILE = RESOURCES_DIR / "writing_guidelines.json"

# ============================================================
# 操作延时配置（秒）- 模拟人工操作
# ============================================================

# 页面加载等待
PAGE_LOAD_WAIT = (2, 4)

# 操作间隔（点击、输入等）
ACTION_DELAY = (1, 3)

# 输入单个字符间隔
TYPE_CHAR_DELAY = (0.05, 0.15)

# 滚动间隔
SCROLL_DELAY = (1, 2)

# 翻页间隔
PAGE_TURN_DELAY = (3, 6)

# 搜索结果抓取间隔
SCRAPE_DELAY = (2, 4)

# ============================================================
# 浏览器配置
# ============================================================

# 浏览器视口大小
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800

# User-Agent（模拟普通用户）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# ============================================================
# 抓取配置
# ============================================================

# 默认抓取笔记数量
DEFAULT_ARTICLE_COUNT = 20

# 最大抓取笔记数量
MAX_ARTICLE_COUNT = 100

# 每页笔记数量（用于计算翻页）
ARTICLES_PER_PAGE = 20

# ============================================================
# 工具函数
# ============================================================

def ensure_dirs():
    """确保必要目录存在"""
    BROWSER_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

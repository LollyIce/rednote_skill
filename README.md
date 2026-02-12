# 2. 首次登录（手动扫码）
python browser_helper.py
# 3. 分析热门文章
python analyze_articles.py --keyword "美食推荐" --count 3
# 4. 发布文章（草稿模式）
python publish_article.py --title "测试" --content "内容" --tags "标签1,标签2" --draft
# CatchIt 🎬

抓取 Instagram **最近一周发布的热门视频 Top 50**（按点赞/评论/播放量排序），在本地网页界面中浏览（封面、标题、描述、URL、互动数据），勾选全部或部分视频，一键下载到项目目录 `videos/<当天日期>/`。

已抓取过的视频会记录在本地历史中，下次抓取**不会重复出现**。

## 工作原理

- Instagram 没有公开的"每周热门榜"接口，工具用 Playwright 打开真实浏览器，登录后从多个来源采集：**Reels 流、Explore 探索页、若干热门标签页**（`#reels` `#viral` `#trending` 等，可在 [scraper.py](scraper.py) 顶部的 `HASHTAGS` 中配置），拦截页面加载时的 JSON 接口响应解析视频数据。
- 采集到的视频在本地按互动分排序（点赞 + 3×评论 + 0.01×播放），过滤出**最近 7 天**发布、且**未抓取过**的视频，取 Top 50。
- 下载功能内置 **yt-dlp**（自动带上你的登录 cookie），无需第三方网站；单个视频下载失败时界面会给出 savefrom.net 的备用链接手动下载。

> ⚠️ Instagram 不对外公开"收藏数"（saves），任何工具都无法获取，因此热度指标使用点赞/评论/播放量。

## 安装

需要 Python 3.9+：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

## 使用

```bash
.venv/bin/python app.py
```

浏览器会自动打开 `http://127.0.0.1:5000`：

1. 点击 **「🔍 抓取最新热门」**——会弹出一个 Chromium 浏览器窗口打开 Instagram。
2. **首次使用请在该窗口中手动登录你的 Instagram 账号**（工具不接触、不保存你的密码，只保存登录后的会话到本地 `ig_state.json`，该文件已被 gitignore，绝不会上传）。之后运行会自动复用登录态。
3. 登录后工具自动滚动采集各来源页面，等待 2–4 分钟，页面自动刷新展示 Top 50 视频卡片。
4. 勾选想要的视频（或点「全选」），点击 **「⬇️ 下载所选」**，视频保存到 `videos/<YYYY-MM-DD>/`，每张卡片上实时显示下载进度/结果。

## 目录说明

```
data/seen.json          已抓取过的视频记录（去重用）
data/results/<日期>.json 每次抓取的 Top 50 结果
data/thumbs/            封面图缓存
videos/<日期>/           下载的视频
ig_state.json           Instagram 登录会话（本地私密文件）
```

## 风险提示

- 抓取依赖 Instagram 页面/接口结构，IG 改版后可能需要维护。
- 频繁抓取有账号被限流的风险，**建议使用小号**，不要过于频繁地运行。
- 下载的视频版权归原作者所有，仅供个人学习研究使用，请勿二次分发。

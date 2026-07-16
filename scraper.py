"""用 Playwright 登录 Instagram，抓取最近一周的热门视频。

策略：Instagram 没有公开的"每周热门榜"接口，这里从多个来源采集
（Reels 流、Explore 页、若干热门标签页），拦截页面加载时的
GraphQL / api/v1 JSON 响应解析出视频数据，再在本地按互动量
（点赞 + 评论 + 播放）排序取 top N。
"""
import json
import os
import re
import time
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "ig_state.json")

# 可按需调整的参数
TOP_N = 50
DAYS = 7                     # 只保留最近 N 天发布的视频
SCROLLS_PER_PAGE = 12        # 每个来源页向下滚动的次数
HASHTAGS = ["reels", "viral", "trending", "explore"]  # 采集的热门标签
LOGIN_TIMEOUT = 300          # 等待用户手动登录的秒数

SOURCES = (
    ["https://www.instagram.com/reels/", "https://www.instagram.com/explore/"]
    + ["https://www.instagram.com/explore/tags/%s/" % t for t in HASHTAGS]
)


def _first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def _dig(d, *path):
    for key in path:
        if isinstance(d, dict):
            d = d.get(key)
        elif isinstance(d, list) and isinstance(key, int) and len(d) > key:
            d = d[key]
        else:
            return None
    return d


def _normalize_api_item(item):
    """api/v1 形状的 media 对象（有 code + media_type）。"""
    if item.get("media_type") != 2:  # 2 = 视频
        return None
    code = item.get("code")
    if not code:
        return None
    caption = _dig(item, "caption", "text") or ""
    thumb = _dig(item, "image_versions2", "candidates", 0, "url")
    return {
        "code": code,
        "url": "https://www.instagram.com/reel/%s/" % code,
        "caption": caption,
        "author": _dig(item, "user", "username") or _dig(item, "owner", "username") or "",
        "likes": max(item.get("like_count") or 0, 0),
        "comments": max(item.get("comment_count") or 0, 0),
        "plays": max(_first(item.get("play_count"), item.get("ig_play_count"),
                            item.get("view_count"), 0) or 0, 0),
        "taken_at": item.get("taken_at") or 0,
        "thumb_url": thumb,
    }


def _normalize_gql_node(node):
    """GraphQL 形状的节点（有 shortcode）。"""
    if not node.get("is_video"):
        return None
    code = node.get("shortcode")
    if not code:
        return None
    caption = _dig(node, "edge_media_to_caption", "edges", 0, "node", "text") or ""
    return {
        "code": code,
        "url": "https://www.instagram.com/reel/%s/" % code,
        "caption": caption,
        "author": _dig(node, "owner", "username") or "",
        "likes": max(_first(_dig(node, "edge_media_preview_like", "count"),
                            _dig(node, "edge_liked_by", "count"), 0) or 0, 0),
        "comments": max(_dig(node, "edge_media_to_comment", "count") or 0, 0),
        "plays": max(node.get("video_view_count") or 0, 0),
        "taken_at": node.get("taken_at_timestamp") or 0,
        "thumb_url": node.get("thumbnail_src") or node.get("display_url"),
    }


def _walk(obj, found):
    """递归遍历 JSON，把所有能识别的视频对象收进 found（按 code 去重）。"""
    if isinstance(obj, dict):
        v = None
        if "code" in obj and "media_type" in obj:
            v = _normalize_api_item(obj)
        elif "shortcode" in obj and ("is_video" in obj or "display_url" in obj):
            v = _normalize_gql_node(obj)
        if v:
            old = found.get(v["code"])
            # 同一视频可能出现多次，保留互动数据更全的一份
            if not old or (v["likes"] + v["plays"]) > (old["likes"] + old["plays"]):
                found[v["code"]] = v
        for val in obj.values():
            _walk(val, found)
    elif isinstance(obj, list):
        for val in obj:
            _walk(val, found)


def _score(v):
    return v["likes"] + 3 * v["comments"] + 0.01 * v["plays"]


def _wait_for_login(page, context, report):
    """检测登录态；未登录则等用户在弹出的浏览器里手动登录。"""
    def logged_in():
        return any(c["name"] == "sessionid" and c["value"]
                   for c in context.cookies("https://www.instagram.com"))

    if logged_in():
        return True
    report("waiting_login", "请在弹出的浏览器窗口中登录 Instagram（工具不会读取你的密码）…")
    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        if logged_in():
            time.sleep(3)  # 等登录后的跳转稳定
            return True
        time.sleep(2)
    return False


def run_scrape(report=None):
    """执行一次抓取。report(stage, message, videos_found=0) 用于回报进度。

    返回 (videos, error)：videos 为写入结果文件的 top N 列表。
    """
    def _report(stage, message, count=0):
        if report:
            report(stage, message, count)

    seen = storage.load_seen()
    found = {}

    def on_response(resp):
        url = resp.url
        if "/api/v1/" not in url and "/graphql" not in url:
            return
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            _walk(resp.json(), found)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx_args = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }
        if os.path.exists(STATE_PATH):
            ctx_args["storage_state"] = STATE_PATH
        context = browser.new_context(**ctx_args)
        page = context.new_page()
        page.on("response", on_response)

        _report("login", "打开 Instagram，检查登录状态…")
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        if not _wait_for_login(page, context, _report):
            browser.close()
            return [], "等待登录超时（%d 秒内未检测到登录）。请重试。" % LOGIN_TIMEOUT
        context.storage_state(path=STATE_PATH)

        for i, src in enumerate(SOURCES):
            _report("scraping", "采集来源 %d/%d：%s" % (i + 1, len(SOURCES), src),
                    len(found))
            try:
                page.goto(src, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                for _ in range(SCROLLS_PER_PAGE):
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(1500)
                    _report("scraping",
                            "采集来源 %d/%d：%s" % (i + 1, len(SOURCES), src),
                            len(found))
            except Exception:
                continue  # 某个来源失败不影响其它来源

        # 过滤：最近 N 天、未出现在历史记录里
        cutoff = (datetime.now() - timedelta(days=DAYS)).timestamp()
        fresh = [v for v in found.values()
                 if v["taken_at"] >= cutoff and v["code"] not in seen]
        fresh.sort(key=_score, reverse=True)
        top = fresh[:TOP_N]

        # 下载封面图（用浏览器会话请求，避免 CDN 防盗链）
        _report("thumbs", "保存 %d 个视频的封面图…" % len(top), len(found))
        for v in top:
            v["thumb"] = ""
            if not v.get("thumb_url"):
                continue
            try:
                r = context.request.get(v["thumb_url"], timeout=15000)
                if r.ok:
                    path = os.path.join(storage.THUMBS_DIR, v["code"] + ".jpg")
                    with open(path, "wb") as f:
                        f.write(r.body())
                    v["thumb"] = "/thumbs/%s.jpg" % v["code"]
            except Exception:
                pass
            v.pop("thumb_url", None)

        browser.close()

    for v in top:
        cap = (v["caption"] or "").strip()
        v["title"] = re.split(r"[\n\r]", cap, 1)[0][:120] if cap else "(无标题)"
        v["taken_at_str"] = datetime.fromtimestamp(v["taken_at"]).strftime("%Y-%m-%d %H:%M")

    storage.save_results(top)
    storage.add_seen([v["code"] for v in top])
    _report("done", "抓取完成，共 %d 条新视频。" % len(top), len(found))
    return top, None

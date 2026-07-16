"""用 Playwright 登录 Instagram，按频道抓取最近一周的热门视频。

策略：Instagram 没有公开的"热门榜"接口，从多个来源采集（Reels 流、
Explore 页、频道配置的标签页），拦截页面加载时的 GraphQL / api/v1
JSON 响应解析视频数据，本地按互动量排序取 top N 入库。
"""
import json
import os
import re
import time
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "ig_state.json")
LOGIN_TIMEOUT = 300  # 等待用户手动登录的秒数


# ---------- 登录 ----------

def login_status():
    """不打开浏览器的快速检查：本地是否存有未过期的会话 cookie。"""
    if not os.path.exists(STATE_PATH):
        return {"logged_in": False, "detail": "尚未登录"}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        for c in state.get("cookies", []):
            if c.get("name") == "sessionid" and c.get("value"):
                exp = c.get("expires") or 0
                if exp and exp < time.time():
                    return {"logged_in": False, "detail": "登录已过期，请重新登录"}
                return {"logged_in": True,
                        "detail": "已保存登录会话（若抓取失败请重新登录）"}
    except Exception:
        pass
    return {"logged_in": False, "detail": "尚未登录"}


def _has_session(context):
    return any(c["name"] == "sessionid" and c["value"]
               for c in context.cookies("https://www.instagram.com"))


def run_login(task):
    """独立的登录任务：弹出浏览器让用户手动登录，保存会话后关闭。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx_args = {"viewport": {"width": 1100, "height": 800}, "locale": "en-US"}
        if os.path.exists(STATE_PATH):
            ctx_args["storage_state"] = STATE_PATH
        context = browser.new_context(**ctx_args)
        page = context.new_page()
        task.message = "已打开浏览器，请在窗口中登录 Instagram（工具不读取密码）…"
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        deadline = time.time() + LOGIN_TIMEOUT
        ok = False
        while time.time() < deadline and not task.cancel_event.is_set():
            if _has_session(context):
                ok = True
                break
            time.sleep(2)
        if ok:
            time.sleep(3)  # 等登录后的跳转稳定
            context.storage_state(path=STATE_PATH)
            task.message = "登录成功，会话已保存。"
        elif task.cancel_event.is_set():
            task.message = "登录已取消。"
        else:
            task.error = "等待登录超时（%d 秒）。" % LOGIN_TIMEOUT
        browser.close()


# ---------- 数据解析（与页面响应的两种 JSON 形状对应） ----------

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
    return {
        "code": code,
        "url": "https://www.instagram.com/reel/%s/" % code,
        "caption": _dig(item, "caption", "text") or "",
        "author": _dig(item, "user", "username") or _dig(item, "owner", "username") or "",
        "likes": max(item.get("like_count") or 0, 0),
        "comments": max(item.get("comment_count") or 0, 0),
        "plays": max(_first(item.get("play_count"), item.get("ig_play_count"),
                            item.get("view_count"), 0) or 0, 0),
        "taken_at": item.get("taken_at") or 0,
        "thumb_url": _dig(item, "image_versions2", "candidates", 0, "url"),
    }


def _normalize_gql_node(node):
    """GraphQL 形状的节点（有 shortcode）。"""
    if not node.get("is_video"):
        return None
    code = node.get("shortcode")
    if not code:
        return None
    return {
        "code": code,
        "url": "https://www.instagram.com/reel/%s/" % code,
        "caption": _dig(node, "edge_media_to_caption", "edges", 0, "node", "text") or "",
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


# ---------- 抓取任务 ----------

def run_scrape(task, channel):
    """抓取一个频道（由 TaskManager 调度）。channel 为 db.list_channels() 的一项。"""
    settings = db.get_settings()
    sources = (["https://www.instagram.com/reels/", "https://www.instagram.com/explore/"]
               + ["https://www.instagram.com/explore/tags/%s/" % t
                  for t in channel["hashtags"]])
    known = db.known_codes()
    found = {}

    def on_response(resp):
        if "/api/v1/" not in resp.url and "/graphql" not in resp.url:
            return
        try:
            if "json" in resp.headers.get("content-type", ""):
                _walk(resp.json(), found)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx_args = {"viewport": {"width": 1280, "height": 900}, "locale": "en-US"}
        if os.path.exists(STATE_PATH):
            ctx_args["storage_state"] = STATE_PATH
        context = browser.new_context(**ctx_args)
        page = context.new_page()
        page.on("response", on_response)

        task.message = "打开 Instagram，检查登录状态…"
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if not _has_session(context):
            browser.close()
            task.error = "未登录或登录已失效，请先到「设置」页登录 Instagram。"
            return
        context.storage_state(path=STATE_PATH)

        cancelled = False
        for i, src in enumerate(sources):
            if task.cancel_event.is_set():
                cancelled = True
                break
            task.message = "频道「%s」采集来源 %d/%d" % (channel["name"], i + 1, len(sources))
            task.progress = {"found": len(found)}
            try:
                page.goto(src, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                for _ in range(settings["scrolls"]):
                    if task.cancel_event.is_set():
                        cancelled = True
                        break
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(1500)
                    task.progress = {"found": len(found)}
            except Exception:
                continue  # 某个来源失败不影响其它来源

        # 过滤：最近 N 天、库中没有的，按互动分取 top N
        cutoff = (datetime.now() - timedelta(days=settings["days"])).timestamp()
        fresh = [v for v in found.values()
                 if v["taken_at"] >= cutoff and v["code"] not in known]
        fresh.sort(key=_score, reverse=True)
        top = fresh[:settings["top_n"]]

        task.message = "保存 %d 条新视频的封面…" % len(top)
        for v in top:
            v["thumb"] = ""
            if v.get("thumb_url"):
                try:
                    r = context.request.get(v["thumb_url"], timeout=15000)
                    if r.ok:
                        path = os.path.join(db.THUMBS_DIR, v["code"] + ".jpg")
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
        v["score"] = _score(v)
        db.insert_video(v, source="scrape", channel_id=channel["id"])

    if cancelled:
        task.message = "抓取已取消，已入库 %d 条新视频。" % len(top)
    else:
        task.message = "抓取完成：共发现 %d 条，入库 %d 条新视频。" % (len(found), len(top))
    task.progress = {"found": len(found), "added": len(top)}

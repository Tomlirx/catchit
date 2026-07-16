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
IG_APP_ID = "936619743392459"  # instagram.com 网页端调用内部 API 的固定 app id
DETAIL_FETCH_MAX = 60          # 每次抓取最多补抓多少条视频详情
DETAIL_FETCH_DELAY_MS = 1200   # 补抓详情的间隔，避免触发限流


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


def _hot_enough(v, min_likes):
    """热度达标：点赞过线；点赞显示为 0（作者隐藏）时才允许用播放量豁免，
    点赞可见但不达标的一律拒绝——高播放低点赞是低质信号，不能钻豁免的空子。"""
    return (v["likes"] >= min_likes
            or (v["likes"] == 0 and v["plays"] >= min_likes * 20))


# ---------- Instagram 网页版内部 API（带登录态直接请求，数据比页面拦截全） ----------

def _fetch_json(context, url):
    try:
        r = context.request.get(url, headers={
            "x-ig-app-id": IG_APP_ID,
            "accept": "*/*",
            "referer": "https://www.instagram.com/",
        }, timeout=15000)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def _fetch_tag_api(context, tag, found):
    """标签热门帖 API。成功返回新增条数，失败返回 None（调用方回退到页面滚动）。"""
    data = _fetch_json(
        context, "https://www.instagram.com/api/v1/tags/web_info/?tag_name=%s" % tag)
    if not data:
        return None
    before = len(found)
    _walk(data, found)
    added = len(found) - before
    return added if added > 0 else None


def _fetch_user_api(context, username, found):
    """博主近期作品 API。成功返回新增条数，失败返回 None（调用方回退到主页滚动）。"""
    data = _fetch_json(
        context,
        "https://www.instagram.com/api/v1/feed/user/%s/username/?count=30" % username)
    if not data:
        return None
    before = len(found)
    _walk(data, found)
    added = len(found) - before
    return added if added > 0 else None


def _fetch_detail(context, code):
    """单条视频详情：补齐列表页缺失的点赞/播放/发布时间。"""
    data = _fetch_json(
        context, "https://www.instagram.com/reel/%s/?__a=1&__d=dis" % code)
    if not data:
        return None
    tmp = {}
    _walk(data, tmp)
    return tmp.get(code)


# ---------- 抓取任务 ----------

def run_scrape(task, channel):
    """抓取一个频道（由 TaskManager 调度）。channel 为 db.list_channels() 的一项。"""
    settings = db.get_settings()
    # Reels 流的元数据最全（点赞/播放/时间齐备），滚动加倍提高采样量
    scroll_sources = [
        ("https://www.instagram.com/reels/", settings["scrolls"] * 2),
        ("https://www.instagram.com/explore/", settings["scrolls"]),
    ]
    if settings.get("follow_feed"):
        # 首页「关注」流：内容全部来自用户关注的账号、按时间排列，
        # 一个来源即可覆盖整个关注列表的近期发布
        scroll_sources.insert(
            0, ("https://www.instagram.com/?variant=following", settings["scrolls"]))
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

        # 1) 标签：优先走 web_info API（快且字段全），失败回退到标签页滚动
        fallback_tags = []
        for i, tag in enumerate(channel["hashtags"]):
            if task.cancel_event.is_set():
                cancelled = True
                break
            task.message = "频道「%s」标签 #%s（%d/%d）" % (
                channel["name"], tag, i + 1, len(channel["hashtags"]))
            task.progress = {"found": len(found)}
            if _fetch_tag_api(context, tag, found) is None:
                fallback_tags.append(tag)
            page.wait_for_timeout(800)

        # 2) 频道配置的博主：优先走作品流 API，失败回退到主页 Reels 滚动
        fallback_accounts = []
        accounts = channel.get("accounts", [])
        for i, acc in enumerate(accounts):
            if task.cancel_event.is_set():
                cancelled = True
                break
            task.message = "频道「%s」博主 @%s（%d/%d）" % (
                channel["name"], acc, i + 1, len(accounts))
            task.progress = {"found": len(found)}
            if _fetch_user_api(context, acc, found) is None:
                fallback_accounts.append(acc)
            page.wait_for_timeout(800)

        # 3) 滚动来源：Reels 流 + Explore + API 失败的标签页/博主主页
        scrolls_list = (scroll_sources
                        + [("https://www.instagram.com/explore/tags/%s/" % t,
                            settings["scrolls"]) for t in fallback_tags]
                        + [("https://www.instagram.com/%s/reels/" % a,
                            settings["scrolls"]) for a in fallback_accounts])
        for i, (src, n_scrolls) in enumerate(scrolls_list):
            if task.cancel_event.is_set():
                cancelled = True
                break
            task.message = "频道「%s」采集来源 %d/%d" % (channel["name"], i + 1, len(scrolls_list))
            task.progress = {"found": len(found)}
            try:
                page.goto(src, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                for _ in range(n_scrolls):
                    if task.cancel_event.is_set():
                        cancelled = True
                        break
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(1500)
                    task.progress = {"found": len(found)}
            except Exception:
                continue  # 某个来源失败不影响其它来源

        # 4) 过滤 + 补抓：最近 N 天、库中没有、热度达标；字段缺失的补抓详情再判定。
        #    门槛不达标宁缺毋滥，不凑数。
        cutoff = (datetime.now() - timedelta(days=settings["days"])).timestamp()
        min_likes = settings.get("min_likes", 0)
        stats = {"found": len(found), "dup": 0, "old": 0, "low": 0,
                 "no_data": 0, "rescued": 0}
        fresh, need_detail = [], []
        for v in found.values():
            if v["code"] in known:
                stats["dup"] += 1
            elif not v["taken_at"] or (v["likes"] == 0 and v["plays"] == 0):
                need_detail.append(v)  # 列表页没给全字段，先别下结论
            elif v["taken_at"] < cutoff:
                stats["old"] += 1
            elif not _hot_enough(v, min_likes):
                stats["low"] += 1
            else:
                fresh.append(v)

        # 有互动信号的排前面优先补抓，上限 DETAIL_FETCH_MAX 条
        need_detail.sort(key=_score, reverse=True)
        need_detail = need_detail[:DETAIL_FETCH_MAX]
        for i, v in enumerate(need_detail):
            if task.cancel_event.is_set():
                cancelled = True
                break
            task.message = "补抓详情 %d/%d（捞回字段缺失的候选）…" % (i + 1, len(need_detail))
            d = _fetch_detail(context, v["code"])
            page.wait_for_timeout(DETAIL_FETCH_DELAY_MS)
            if not d or not d["taken_at"]:
                stats["no_data"] += 1
                continue
            if d["taken_at"] < cutoff:
                stats["old"] += 1
            elif not _hot_enough(d, min_likes):
                stats["low"] += 1
            else:
                stats["rescued"] += 1
                fresh.append(d)

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

    detail = ("共发现 %d 条：重复 %d、超出时间窗 %d、热度不达标 %d、数据不全 %d、"
              "详情补抓捞回 %d → 入库 %d 条" % (
                  stats["found"], stats["dup"], stats["old"], stats["low"],
                  stats["no_data"], stats["rescued"], len(top)))
    task.message = ("抓取已取消。" if cancelled else "抓取完成。") + detail
    task.progress = dict(stats, added=len(top))

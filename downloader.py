"""下载模块：yt-dlp 下载队列 + 粘贴 URL 的解析入库。

队列以 DB 为准（dl_status='queued'），worker 逐个下载到 videos/<当天日期>/。
"""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime

import yt_dlp

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "ig_state.json")
COOKIES_PATH = os.path.join(db.DATA_DIR, "cookies.txt")

SAVEFROM_URL = "https://en1.savefrom.net/14xK/download-from-instagram#url="
URL_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def savefrom_link(video_url):
    """下载失败时给用户的 savefrom.net 手动下载备用链接。"""
    return SAVEFROM_URL + urllib.parse.quote(video_url, safe="")


def _write_cookies_txt():
    """把 Playwright 登录态转换成 yt-dlp 可用的 Netscape cookie 文件。"""
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
    lines = ["# Netscape HTTP Cookie File"]
    for c in state.get("cookies", []):
        if "instagram" not in c.get("domain", ""):
            continue
        domain = c["domain"]
        expires = int(c.get("expires") or 0)
        if expires < 0:
            expires = 2147483647
        lines.append("\t".join([
            domain, "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path", "/"), "TRUE" if c.get("secure") else "FALSE",
            str(expires), c["name"], c["value"]]))
    os.makedirs(db.DATA_DIR, exist_ok=True)
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return COOKIES_PATH


def _ydl_opts(extra=None):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "retries": 2}
    if _write_cookies_txt():
        opts["cookiefile"] = COOKIES_PATH
    if extra:
        opts.update(extra)
    return opts


def _safe_name(s):
    return re.sub(r"[^\w.-]+", "_", s or "").strip("_")[:40] or "unknown"


def _save_thumb(code, thumb_url):
    """尽力保存封面图，失败不影响主流程。"""
    if not thumb_url:
        return ""
    try:
        req = urllib.request.Request(thumb_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        path = os.path.join(db.THUMBS_DIR, code + ".jpg")
        with open(path, "wb") as f:
            f.write(data)
        return "/thumbs/%s.jpg" % code
    except Exception:
        return ""


def extract_codes(text):
    """从任意文本中提取 Instagram 视频 shortcode 列表（保序去重）。"""
    seen, codes = set(), []
    for m in URL_RE.finditer(text or ""):
        c = m.group(1)
        if c not in seen:
            seen.add(c)
            codes.append(c)
    return codes


def resolve_and_queue(text):
    """解析粘贴的 URL：拉元数据入库并加入下载队列。

    返回 {"added": [...], "skipped": [...], "failed": {code: err}}
    """
    codes = extract_codes(text)
    known = db.known_codes()
    result = {"added": [], "skipped": [], "failed": {}}
    for code in codes:
        if code in known:
            result["skipped"].append(code)
            continue
        url = "https://www.instagram.com/reel/%s/" % code
        try:
            with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            result["failed"][code] = str(e)[:200]
            continue
        caption = info.get("description") or info.get("title") or ""
        title = re.split(r"[\n\r]", caption.strip(), 1)[0][:120] if caption.strip() else "(无标题)"
        video = {
            "code": code,
            "url": url,
            "title": title,
            "caption": caption,
            "author": info.get("uploader") or info.get("channel") or "",
            "likes": info.get("like_count") or 0,
            "comments": info.get("comment_count") or 0,
            "plays": info.get("view_count") or 0,
            "taken_at": info.get("timestamp") or 0,
            "score": 0,
            "thumb": _save_thumb(code, info.get("thumbnail")),
        }
        db.insert_video(video, source="manual", dl_status="queued")
        result["added"].append(code)
    return result


def queue_downloads(codes):
    """把一批已入库的视频标记为排队下载。"""
    for code in codes:
        v = db.get_video(code)
        if v and v["dl_status"] in ("none", "failed"):
            db.update_video(code, {"dl_status": "queued", "error": ""})


def run_download_worker(task):
    """下载 worker（由 TaskManager 调度）：处理完队列中全部视频后退出。"""
    date = datetime.now().strftime("%Y-%m-%d")
    out_dir = os.path.join(db.VIDEOS_DIR, date)
    os.makedirs(out_dir, exist_ok=True)
    done = failed = 0

    while not task.cancel_event.is_set():
        v = db.next_queued()
        if not v:
            break
        code = v["code"]
        db.update_video(code, {"dl_status": "downloading"})
        task.message = "正在下载 @%s 的视频 %s…" % (v["author"] or "?", code)
        task.progress = {"done": done, "failed": failed,
                         "left": db.counts()["queued"], "current": code}
        outtmpl = os.path.join(out_dir, "%s_%s.%%(ext)s" % (_safe_name(v["author"]), code))
        opts = _ydl_opts({
            "outtmpl": outtmpl,
            "format": "mp4/bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(v["url"], download=True)
            path = ""
            reqs = (info or {}).get("requested_downloads") or []
            if reqs and reqs[0].get("filepath"):
                path = os.path.relpath(reqs[0]["filepath"], db.VIDEOS_DIR)
            db.update_video(code, {"dl_status": "done", "file_path": path, "error": ""})
            done += 1
        except Exception as e:
            db.update_video(code, {"dl_status": "failed", "error": str(e)[:300]})
            failed += 1

    if task.cancel_event.is_set():
        db.reset_queued()
        task.message = "下载已取消（完成 %d，失败 %d）" % (done, failed)
    else:
        task.message = "下载完成：成功 %d，失败 %d" % (done, failed)
    task.progress = {"done": done, "failed": failed, "left": 0, "current": ""}

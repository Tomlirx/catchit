"""用 yt-dlp 把勾选的 Instagram 视频下载到 videos/<当天日期>/。"""
import json
import os
import re
import urllib.parse

import yt_dlp

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "ig_state.json")
COOKIES_PATH = os.path.join(storage.DATA_DIR, "cookies.txt")

SAVEFROM_URL = "https://en1.savefrom.net/14xK/download-from-instagram#url="


def savefrom_link(video_url):
    """某个视频下载失败时给用户的 savefrom.net 手动下载备用链接。"""
    return SAVEFROM_URL + urllib.parse.quote(video_url, safe="")


def _write_cookies_txt():
    """把 Playwright 的登录态转换成 yt-dlp 可用的 Netscape cookie 文件。

    Instagram 的部分视频未登录时 yt-dlp 会下载失败，所以带上会话 cookie。
    """
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
    lines = ["# Netscape HTTP Cookie File"]
    for c in state.get("cookies", []):
        if "instagram" not in c.get("domain", ""):
            continue
        domain = c["domain"]
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires < 0:
            expires = 2147483647  # 会话 cookie 给一个远期时间
        lines.append("\t".join([domain, include_sub, c.get("path", "/"),
                                secure, str(expires), c["name"], c["value"]]))
    os.makedirs(storage.DATA_DIR, exist_ok=True)
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return COOKIES_PATH


def _safe_name(s):
    return re.sub(r"[^\w.-]+", "_", s or "").strip("_")[:40] or "unknown"


def download_videos(videos, report=None):
    """逐个下载。videos 为结果记录列表；report(code, status, detail) 回报进度。

    返回 {code: {"ok": bool, "file"/"error": str, "savefrom": str}}
    """
    def _report(code, status, detail=""):
        if report:
            report(code, status, detail)

    out_dir = storage.download_dir()
    cookiefile = _write_cookies_txt()
    results = {}

    for i, v in enumerate(videos, 1):
        code = v["code"]
        _report(code, "downloading", "")
        outtmpl = os.path.join(
            out_dir, "%02d_%s_%s.%%(ext)s" % (i, _safe_name(v.get("author")), code))
        opts = {
            "outtmpl": outtmpl,
            "format": "mp4/bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 2,
        }
        if cookiefile:
            opts["cookiefile"] = cookiefile
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(v["url"], download=True)
                path = None
                if info:
                    reqs = info.get("requested_downloads") or []
                    if reqs:
                        path = reqs[0].get("filepath")
                results[code] = {"ok": True,
                                 "file": os.path.basename(path) if path else ""}
                _report(code, "done", results[code]["file"])
        except Exception as e:
            results[code] = {"ok": False, "error": str(e)[:300],
                             "savefrom": savefrom_link(v["url"])}
            _report(code, "failed", results[code]["error"])

    return results

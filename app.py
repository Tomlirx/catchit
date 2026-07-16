"""CatchIt v2 — Instagram 热门视频搬运工作台。

运行：.venv/bin/python app.py  →  浏览器自动打开 http://127.0.0.1:5000
"""
import logging
import os
import threading
import webbrowser

from flask import Flask, jsonify, render_template, request, send_from_directory

# 前端每 2 秒轮询一次任务状态，默认的访问日志会刷屏——只保留报错
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import db
import downloader
import scraper
from tasks import manager

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/thumbs/<path:name>")
def thumbs(name):
    return send_from_directory(db.THUMBS_DIR, name)


@app.route("/media/<path:name>")
def media(name):
    return send_from_directory(db.VIDEOS_DIR, name, conditional=True)


# ---------- 状态 ----------

@app.route("/api/status")
def api_status():
    return jsonify({
        "tasks": manager.status(),
        "counts": db.counts(),
        "login": scraper.login_status(),
    })


@app.route("/api/cancel/<name>", methods=["POST"])
def api_cancel(name):
    if name not in ("scrape", "download", "login"):
        return jsonify({"ok": False, "error": "未知任务"}), 400
    return jsonify({"ok": manager.cancel(name)})


# ---------- 视频 ----------

@app.route("/api/videos")
def api_videos():
    return jsonify(db.list_videos(
        view=request.args.get("view", "discover"),
        channel_id=request.args.get("channel", type=int),
        pub_filter=request.args.get("pub"),
        sort=request.args.get("sort", "score"),
    ))


@app.route("/api/video/<code>", methods=["PATCH"])
def api_video_patch(code):
    if not db.get_video(code):
        return jsonify({"ok": False, "error": "视频不存在"}), 404
    fields = request.get_json(silent=True) or {}
    db.update_video(code, fields)
    return jsonify({"ok": True, "video": db.get_video(code)})


@app.route("/api/video/<code>/savefrom")
def api_video_savefrom(code):
    v = db.get_video(code)
    if not v:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "link": downloader.savefrom_link(v["url"])})


# ---------- 下载 ----------

def _start_download_worker():
    return manager.start("download", downloader.run_download_worker)


@app.route("/api/download", methods=["POST"])
def api_download():
    codes = (request.get_json(silent=True) or {}).get("codes", [])
    if not codes:
        return jsonify({"ok": False, "error": "未选择任何视频"}), 400
    downloader.queue_downloads(codes)
    _start_download_worker()
    return jsonify({"ok": True, "queued": db.counts()["queued"]})


@app.route("/api/add_urls", methods=["POST"])
def api_add_urls():
    text = (request.get_json(silent=True) or {}).get("text", "")
    codes = downloader.extract_codes(text)
    if not codes:
        return jsonify({"ok": False, "error": "没有识别到 Instagram 视频链接"}), 400

    task_started = manager.start("resolve", lambda task: _resolve_worker(task, text))
    if not task_started:
        return jsonify({"ok": False, "error": "正在解析上一批链接，请稍候"}), 409
    return jsonify({"ok": True, "count": len(codes)})


def _resolve_worker(task, text):
    task.message = "正在解析链接并获取视频信息…"
    result = downloader.resolve_and_queue(text)
    task.progress = result
    parts = []
    if result["added"]:
        parts.append("新增 %d 条并开始下载" % len(result["added"]))
        _start_download_worker()
    if result["skipped"]:
        parts.append("%d 条已在库中" % len(result["skipped"]))
    if result["failed"]:
        parts.append("%d 条解析失败" % len(result["failed"]))
    task.message = "，".join(parts) if parts else "没有新链接。"


# ---------- 抓取 / 登录 ----------

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    channel_id = (request.get_json(silent=True) or {}).get("channel_id")
    channels = {c["id"]: c for c in db.list_channels()}
    channel = channels.get(channel_id) or (list(channels.values()) or [None])[0]
    if not channel:
        return jsonify({"ok": False, "error": "请先在设置里创建频道"}), 400
    if not manager.start("scrape", lambda task: scraper.run_scrape(task, channel)):
        return jsonify({"ok": False, "error": "抓取任务已在运行"}), 409
    return jsonify({"ok": True})


@app.route("/api/login", methods=["POST"])
def api_login():
    if not manager.start("login", scraper.run_login):
        return jsonify({"ok": False, "error": "登录窗口已打开"}), 409
    return jsonify({"ok": True})


# ---------- 频道 / 设置 ----------

@app.route("/api/channels", methods=["GET", "POST"])
def api_channels():
    if request.method == "GET":
        return jsonify(db.list_channels())
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "频道名不能为空"}), 400
    cid = db.upsert_channel(name, data.get("hashtags", []),
                            data.get("accounts", []), data.get("id"))
    return jsonify({"ok": True, "id": cid})


@app.route("/api/channels/<int:cid>", methods=["DELETE"])
def api_channel_delete(cid):
    db.delete_channel(cid)
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(db.get_settings())
    db.set_settings(request.get_json(silent=True) or {})
    return jsonify({"ok": True, "settings": db.get_settings()})


if __name__ == "__main__":
    db.conn()  # 建表 + v1 数据迁移
    if not os.environ.get("CATCHIT_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)

"""CatchIt — Instagram 每周热门视频抓取与下载工具（本地网页界面）。

运行：python app.py  →  浏览器自动打开 http://127.0.0.1:5000
"""
import threading
import webbrowser

from flask import Flask, jsonify, render_template, request, send_from_directory

import downloader
import scraper
import storage

app = Flask(__name__)

# 全局任务状态（单用户本地工具，简单起见用内存字典 + 锁）
_lock = threading.Lock()
STATE = {
    "scrape": {"running": False, "stage": "", "message": "", "found": 0, "error": None},
    "download": {"running": False, "items": {}},  # items: code -> {status, detail}
}


@app.route("/")
def index():
    results = storage.load_latest_results()
    return render_template("index.html",
                           videos=(results or {}).get("videos", []),
                           result_date=(results or {}).get("date", ""))


@app.route("/thumbs/<path:name>")
def thumbs(name):
    return send_from_directory(storage.THUMBS_DIR, name)


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    with _lock:
        if STATE["scrape"]["running"] or STATE["download"]["running"]:
            return jsonify({"ok": False, "error": "已有任务在运行"}), 409
        STATE["scrape"] = {"running": True, "stage": "starting",
                           "message": "启动浏览器…", "found": 0, "error": None}

    def progress(stage, message, count=0):
        STATE["scrape"].update({"stage": stage, "message": message, "found": count})

    def worker():
        try:
            _, err = scraper.run_scrape(progress)
            STATE["scrape"]["error"] = err
        except Exception as e:
            STATE["scrape"]["error"] = "抓取出错：%s" % e
        finally:
            STATE["scrape"]["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/download", methods=["POST"])
def api_download():
    codes = (request.get_json(silent=True) or {}).get("codes", [])
    if not codes:
        return jsonify({"ok": False, "error": "未选择任何视频"}), 400
    results = storage.load_latest_results()
    if not results:
        return jsonify({"ok": False, "error": "没有可下载的抓取结果"}), 400
    videos = [v for v in results["videos"] if v["code"] in set(codes)]

    with _lock:
        if STATE["scrape"]["running"] or STATE["download"]["running"]:
            return jsonify({"ok": False, "error": "已有任务在运行"}), 409
        STATE["download"] = {
            "running": True,
            "items": {v["code"]: {"status": "pending", "detail": ""} for v in videos},
        }

    def progress(code, status, detail):
        STATE["download"]["items"][code] = {"status": status, "detail": detail}

    def worker():
        try:
            res = downloader.download_videos(videos, progress)
            for code, r in res.items():
                if not r["ok"]:
                    STATE["download"]["items"][code] = {
                        "status": "failed", "detail": r["error"],
                        "savefrom": r["savefrom"]}
        finally:
            STATE["download"]["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "dir": storage.download_dir()})


@app.route("/api/status")
def api_status():
    return jsonify(STATE)


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)

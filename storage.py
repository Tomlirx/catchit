"""历史记录（去重）与抓取结果的持久化。"""
import json
import os
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
RESULTS_DIR = os.path.join(DATA_DIR, "results")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")
VIDEOS_DIR = os.path.join(BASE_DIR, "videos")

_lock = threading.Lock()


def _ensure_dirs():
    for d in (DATA_DIR, RESULTS_DIR, THUMBS_DIR, VIDEOS_DIR):
        os.makedirs(d, exist_ok=True)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def load_seen():
    """返回已抓取过的 shortcode 集合。"""
    _ensure_dirs()
    if not os.path.exists(SEEN_PATH):
        return set()
    with open(SEEN_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f))


def add_seen(shortcodes):
    _ensure_dirs()
    with _lock:
        seen = load_seen()
        seen.update(shortcodes)
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f, ensure_ascii=False, indent=1)


def save_results(videos, date=None):
    """保存一次抓取的 top 结果到 data/results/<date>.json。"""
    _ensure_dirs()
    date = date or today_str()
    path = os.path.join(RESULTS_DIR, date + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": date, "videos": videos}, f, ensure_ascii=False, indent=1)
    return path


def load_latest_results():
    """读取最近一次的抓取结果，没有则返回 None。"""
    _ensure_dirs()
    files = sorted(f for f in os.listdir(RESULTS_DIR) if f.endswith(".json"))
    if not files:
        return None
    with open(os.path.join(RESULTS_DIR, files[-1]), "r", encoding="utf-8") as f:
        return json.load(f)


def download_dir(date=None):
    """返回（并创建）当天的下载目录 videos/<YYYY-MM-DD>/。"""
    d = os.path.join(VIDEOS_DIR, date or today_str())
    os.makedirs(d, exist_ok=True)
    return d

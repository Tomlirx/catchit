"""SQLite 数据层：素材库、频道、设置，以及 v1 JSON 数据的一次性迁移。"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "catchit.db")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")
VIDEOS_DIR = os.path.join(BASE_DIR, "videos")

_lock = threading.Lock()
_conn = None

DEFAULT_SETTINGS = {
    "days": 10,         # 只保留最近 N 天发布的视频
    "top_n": 50,        # 每次抓取入库的条数上限
    "scrolls": 12,      # 每个来源页滚动次数
    "min_likes": 10000, # 热度门槛：低于此点赞数不入库（播放量达到 20 倍门槛也算过）
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT UNIQUE NOT NULL,
    url         TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'scrape',   -- scrape | manual
    channel_id  INTEGER,
    title       TEXT DEFAULT '',
    caption     TEXT DEFAULT '',
    author      TEXT DEFAULT '',
    likes       INTEGER DEFAULT 0,
    comments    INTEGER DEFAULT 0,
    plays       INTEGER DEFAULT 0,
    taken_at    INTEGER DEFAULT 0,
    score       REAL DEFAULT 0,
    thumb       TEXT DEFAULT '',
    dl_status   TEXT DEFAULT 'none',              -- none|queued|downloading|done|failed
    file_path   TEXT DEFAULT '',                  -- 相对 videos/ 的路径
    error       TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    pub_xhs     INTEGER DEFAULT 0,
    pub_xhs_at  TEXT DEFAULT '',
    pub_douyin  INTEGER DEFAULT 0,
    pub_douyin_at TEXT DEFAULT '',
    ignored     INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT UNIQUE NOT NULL,
    hashtags TEXT NOT NULL DEFAULT '[]',
    enabled  INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conn():
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(THUMBS_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _init_defaults()
        _migrate_v1()
    return _conn


def _exec(sql, params=()):
    with _lock:
        cur = conn().execute(sql, params)
        conn().commit()
        return cur


def _init_defaults():
    if not _conn.execute("SELECT 1 FROM channels LIMIT 1").fetchone():
        _conn.execute(
            "INSERT INTO channels (name, hashtags) VALUES (?, ?)",
            ("萌宠", json.dumps(["reels", "viral", "trending", "explore"])))
    for k, v in DEFAULT_SETTINGS.items():
        _conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                      (k, str(v)))
    _conn.commit()


def _migrate_v1():
    """把 v1 的 data/results/*.json 和 data/seen.json 迁移进 DB（只跑一次）。"""
    if _conn.execute("SELECT 1 FROM settings WHERE key='migrated_v1'").fetchone():
        return
    results_dir = os.path.join(DATA_DIR, "results")
    default_ch = _conn.execute("SELECT id FROM channels LIMIT 1").fetchone()[0]
    now = _now()
    if os.path.isdir(results_dir):
        for fn in sorted(os.listdir(results_dir)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(results_dir, fn), encoding="utf-8") as f:
                    for v in json.load(f).get("videos", []):
                        _conn.execute(
                            """INSERT OR IGNORE INTO videos
                               (code, url, source, channel_id, title, caption, author,
                                likes, comments, plays, taken_at, score, thumb, created_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (v["code"], v["url"], "scrape", default_ch,
                             v.get("title", ""), v.get("caption", ""), v.get("author", ""),
                             v.get("likes", 0), v.get("comments", 0), v.get("plays", 0),
                             v.get("taken_at", 0),
                             v.get("likes", 0) + 3 * v.get("comments", 0) + 0.01 * v.get("plays", 0),
                             v.get("thumb", ""), now))
            except Exception:
                pass
    seen_path = os.path.join(DATA_DIR, "seen.json")
    if os.path.exists(seen_path):
        try:
            with open(seen_path, encoding="utf-8") as f:
                for code in json.load(f):
                    # 只有 code 没有详情的历史记录，作为"已忽略"占位保证去重
                    _conn.execute(
                        """INSERT OR IGNORE INTO videos
                           (code, url, ignored, created_at)
                           VALUES (?, ?, 1, ?)""",
                        (code, "https://www.instagram.com/reel/%s/" % code, now))
        except Exception:
            pass
    _conn.execute("INSERT INTO settings (key, value) VALUES ('migrated_v1', '1')")
    _conn.commit()


# ---------- settings ----------

def get_settings():
    rows = conn().execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r["key"] in DEFAULT_SETTINGS:
            try:
                s[r["key"]] = int(r["value"])
            except ValueError:
                pass
    return s


def set_settings(updates):
    for k, v in updates.items():
        if k in DEFAULT_SETTINGS:
            _exec("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                  (k, str(int(v))))


# ---------- channels ----------

def list_channels():
    rows = conn().execute("SELECT * FROM channels ORDER BY id").fetchall()
    return [{"id": r["id"], "name": r["name"],
             "hashtags": json.loads(r["hashtags"]), "enabled": r["enabled"]}
            for r in rows]


def upsert_channel(name, hashtags, channel_id=None):
    tags = json.dumps([t.strip().lstrip("#") for t in hashtags if t.strip()])
    if channel_id:
        _exec("UPDATE channels SET name=?, hashtags=? WHERE id=?",
              (name, tags, channel_id))
        return channel_id
    return _exec("INSERT INTO channels (name, hashtags) VALUES (?, ?)",
                 (name, tags)).lastrowid


def delete_channel(channel_id):
    _exec("DELETE FROM channels WHERE id=?", (channel_id,))


# ---------- videos ----------

def known_codes():
    return {r["code"] for r in conn().execute("SELECT code FROM videos").fetchall()}


def insert_video(v, source, channel_id=None, dl_status="none"):
    _exec("""INSERT OR IGNORE INTO videos
             (code, url, source, channel_id, title, caption, author,
              likes, comments, plays, taken_at, score, thumb, dl_status, created_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (v["code"], v["url"], source, channel_id,
           v.get("title", ""), v.get("caption", ""), v.get("author", ""),
           v.get("likes", 0), v.get("comments", 0), v.get("plays", 0),
           v.get("taken_at", 0), v.get("score", 0), v.get("thumb", ""),
           dl_status, _now()))


def get_video(code):
    r = conn().execute("SELECT * FROM videos WHERE code=?", (code,)).fetchone()
    return dict(r) if r else None


def list_videos(view="discover", channel_id=None, pub_filter=None, sort="score"):
    """view: discover(未下载未忽略) | library(已下载) | failed | all"""
    where, params = ["ignored=0"], []
    if view == "discover":
        where.append("dl_status IN ('none','queued','downloading')")
    elif view == "library":
        where.append("dl_status='done'")
    elif view == "failed":
        where.append("dl_status='failed'")
    if channel_id:
        where.append("channel_id=?")
        params.append(channel_id)
    if pub_filter == "pending":
        where.append("(pub_xhs=0 OR pub_douyin=0)")
    elif pub_filter == "published":
        where.append("(pub_xhs=1 AND pub_douyin=1)")
    order = {
        "score": "score DESC", "likes": "likes DESC",
        "time": "taken_at DESC", "added": "created_at DESC",
        # 日增热度：互动分 / 发布至今的天数（不足半天按半天算），发现正在上升的视频
        "velocity": "(score / MAX((strftime('%s','now') - taken_at) / 86400.0, 0.5)) DESC",
    }.get(sort, "score DESC")
    rows = conn().execute(
        "SELECT * FROM videos WHERE %s ORDER BY %s" % (" AND ".join(where), order),
        params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["taken_at_str"] = (datetime.fromtimestamp(d["taken_at"]).strftime("%Y-%m-%d %H:%M")
                             if d["taken_at"] else "")
        out.append(d)
    return out


def update_video(code, fields):
    """允许更新的字段白名单。发布状态置 1 时自动记录时间。"""
    allowed = {"notes", "pub_xhs", "pub_douyin", "ignored",
               "dl_status", "file_path", "error", "thumb"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append("%s=?" % k)
        params.append(v)
        if k == "pub_xhs":
            sets.append("pub_xhs_at=?")
            params.append(_now() if v else "")
        if k == "pub_douyin":
            sets.append("pub_douyin_at=?")
            params.append(_now() if v else "")
    if not sets:
        return
    params.append(code)
    _exec("UPDATE videos SET %s WHERE code=?" % ", ".join(sets), params)


def next_queued():
    r = conn().execute(
        "SELECT * FROM videos WHERE dl_status='queued' ORDER BY id LIMIT 1").fetchone()
    return dict(r) if r else None


def reset_queued():
    """取消下载时，把还在排队的恢复为未下载。"""
    _exec("UPDATE videos SET dl_status='none' WHERE dl_status='queued'")
    _exec("UPDATE videos SET dl_status='failed', error='已取消' WHERE dl_status='downloading'")


def counts():
    row = conn().execute("""SELECT
        SUM(CASE WHEN ignored=0 AND dl_status IN ('none','queued','downloading') THEN 1 ELSE 0 END) AS discover,
        SUM(CASE WHEN ignored=0 AND dl_status='done' THEN 1 ELSE 0 END) AS library,
        SUM(CASE WHEN ignored=0 AND dl_status='failed' THEN 1 ELSE 0 END) AS failed,
        SUM(CASE WHEN dl_status='queued' THEN 1 ELSE 0 END) AS queued
        FROM videos""").fetchone()
    return {k: row[k] or 0 for k in row.keys()}

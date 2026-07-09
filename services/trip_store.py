"""
行程记忆系统 — SQLite 本地持久化
参照 TripStar travel-planning 的 Markdown 文件结构设计 Schema

表结构:
  trips:       行程元数据
  trip_days:   每日行程条目
  user_prefs:  用户偏好
  packing_lists: 打包清单模板
"""

import os
import re
import sqlite3
import json
import time
import uuid
from typing import Optional
from pathlib import Path


# 支持通过环境变量 TRIP_STORE_DB_PATH 覆盖数据库路径（测试环境用，避免污染真实数据库）。
# 未设置时保持原有行为：项目根目录下的 travel_data.db。
DB_PATH = Path(os.environ["TRIP_STORE_DB_PATH"]) if os.environ.get("TRIP_STORE_DB_PATH") \
    else Path(__file__).parent.parent / "travel_data.db"


def _get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化数据库表"""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trips (
            id TEXT PRIMARY KEY,
            destination TEXT NOT NULL,
            origin TEXT DEFAULT '',
            start_date TEXT DEFAULT '',
            end_date TEXT DEFAULT '',
            days INTEGER DEFAULT 0,
            travelers INTEGER DEFAULT 1,
            budget REAL DEFAULT 0,
            preferences TEXT DEFAULT '',
            markdown TEXT DEFAULT '',
            status TEXT DEFAULT 'draft',
            created_at REAL DEFAULT 0,
            updated_at REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS trip_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id TEXT NOT NULL,
            day_number INTEGER NOT NULL,
            theme TEXT DEFAULT '',
            budget REAL DEFAULT 0,
            created_at REAL DEFAULT 0,
            FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS trip_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_id INTEGER NOT NULL,
            time_slot TEXT DEFAULT '',
            activity TEXT NOT NULL,
            duration TEXT DEFAULT '',
            cost REAL DEFAULT 0,
            notes TEXT DEFAULT '',
            item_type TEXT DEFAULT 'attraction',
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (day_id) REFERENCES trip_days(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_prefs (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS packing_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            items TEXT DEFAULT '[]',
            category TEXT DEFAULT 'general',
            created_at REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS guides (
            id TEXT PRIMARY KEY,
            html TEXT DEFAULT '',
            markdown TEXT DEFAULT '',
            created_at REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS wendao_cache (
            query_hash TEXT PRIMARY KEY,
            query TEXT DEFAULT '',
            result TEXT DEFAULT '',
            created_at REAL DEFAULT 0
        );
    """)
    conn.commit()

    # 兼容旧库：trips 表可能是在加 markdown 列之前建的，用 PRAGMA 检测后补列
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trips)").fetchall()}
    if "markdown" not in existing_cols:
        try:
            conn.execute("ALTER TABLE trips ADD COLUMN markdown TEXT DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    conn.close()


# ---------- 行程 CRUD ----------

def save_trip(
    destination: str,
    markdown: str = "",
    origin: str = "",
    start_date: str = "",
    end_date: str = "",
    days: int = 0,
    travelers: int = 1,
    budget: float = 0,
    preferences: str = "",
) -> str:
    """保存行程，返回行程 ID"""
    trip_id = str(uuid.uuid4())[:8]
    now = time.time()
    conn = _get_db()
    conn.execute("""
        INSERT INTO trips (id, destination, origin, start_date, end_date,
                          days, travelers, budget, preferences, markdown, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
    """, (trip_id, destination, origin, start_date, end_date,
          days, travelers, budget, preferences, markdown, now, now))
    conn.commit()
    conn.close()
    return trip_id


def list_trips(limit: int = 20) -> list[dict]:
    """列出最近行程"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM trips ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trip(trip_id: str) -> Optional[dict]:
    """获取行程详情"""
    conn = _get_db()
    row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_trip(trip_id: str) -> bool:
    """删除行程"""
    conn = _get_db()
    conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    affected = conn.total_changes
    conn.commit()
    conn.close()
    return affected > 0


# ---------- 行程字段解析 ----------

def parse_trip_fields(raw_text: str, markdown: str = "") -> dict:
    """尽力而为地从原始输入文本 + 生成的 markdown 中解析出结构化字段。
    解析失败的字段返回 None，调用方自行决定是否使用默认值。

    Returns:
        {"destination": str, "days": Optional[int], "travelers": Optional[int], "budget": Optional[float]}
    """
    markdown = markdown or ""
    raw_text = raw_text or ""

    # ---- destination ----
    dest = None
    heading_match = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
    heading = heading_match.group(1) if heading_match else ""
    if heading:
        m = re.search(r"🗺️\s*([^\s0-9·，,。！？]{1,10})", heading)
        if m:
            dest = m.group(1)
    if not dest:
        try:
            from services.data_collector import _extract_destination
            dest = _extract_destination(raw_text)
        except Exception:
            dest = None
    if not dest:
        dest = raw_text.strip()[:30] or None

    # ---- days / travelers / budget：在标题和正文中一起找 ----
    combined = f"{heading}\n{markdown}\n{raw_text}"

    days = None
    # 排除日期写法（"7月9日"）：数字前是"月"的不算天数；同时限定合理区间
    m = re.search(r"(?<!月)(\d{1,2})\s*[日天]", combined)
    if m:
        try:
            candidate = int(m.group(1))
            if 1 <= candidate <= 90:
                days = candidate
        except ValueError:
            days = None

    travelers = None
    m = re.search(r"(\d{1,2})\s*(?:个)?人", combined)
    if m:
        try:
            candidate = int(m.group(1))
            if 1 <= candidate <= 50:
                travelers = candidate
        except ValueError:
            travelers = None

    budget = None
    # 支持 "预算15000"、"预算 ¥15,000"、"预算1.5万"、"预算2千" 等写法
    m = re.search(r"预算\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)\s*([万千wWkK])?", combined)
    if m:
        try:
            budget = float(m.group(1).replace(",", ""))
            unit = m.group(2) or ""
            if unit in ("万", "w", "W"):
                budget *= 10000
            elif unit in ("千", "k", "K"):
                budget *= 1000
        except ValueError:
            budget = None

    return {"destination": dest, "days": days, "travelers": travelers, "budget": budget}


# ---------- 用户偏好 ----------

def get_pref(key: str, default: str = "") -> str:
    conn = _get_db()
    row = conn.execute("SELECT value FROM user_prefs WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_pref(key: str, value: str):
    conn = _get_db()
    conn.execute("""
        INSERT INTO user_prefs (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
    """, (key, value, time.time(), value, time.time()))
    conn.commit()
    conn.close()


# ---------- 攻略缓存（SQLite 持久化） ----------

def save_guide(guide_id: str, html: str, markdown: str) -> None:
    """保存/更新攻略缓存"""
    now = time.time()
    conn = _get_db()
    conn.execute("""
        INSERT INTO guides (id, html, markdown, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET html = ?, markdown = ?, created_at = ?
    """, (guide_id, html, markdown, now, html, markdown, now))
    conn.commit()
    conn.close()


def get_guide(guide_id: str) -> Optional[dict]:
    """读取攻略缓存（不做过期判断，由调用方结合 clean_expired_guides 处理）"""
    conn = _get_db()
    row = conn.execute("SELECT * FROM guides WHERE id = ?", (guide_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def clean_expired_guides(ttl_seconds: int) -> None:
    """清理过期攻略缓存"""
    conn = _get_db()
    conn.execute(
        "DELETE FROM guides WHERE (? - created_at) > ?",
        (time.time(), ttl_seconds),
    )
    conn.commit()
    conn.close()


# ---------- 携程问道查询缓存 ----------

def get_cached_wendao(query_hash: str) -> Optional[dict]:
    """按 query_hash 读取缓存记录（不做过期判断，由调用方结合 TTL 处理）"""
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM wendao_cache WHERE query_hash = ?", (query_hash,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_wendao_cache(query_hash: str, query: str, result: str) -> None:
    """写入/更新携程问道查询缓存"""
    now = time.time()
    conn = _get_db()
    conn.execute("""
        INSERT INTO wendao_cache (query_hash, query, result, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(query_hash) DO UPDATE SET query = ?, result = ?, created_at = ?
    """, (query_hash, query, result, now, query, result, now))
    conn.commit()
    conn.close()


def clean_expired_wendao_cache(ttl_seconds: int) -> None:
    """清理过期的携程问道查询缓存"""
    conn = _get_db()
    conn.execute(
        "DELETE FROM wendao_cache WHERE (? - created_at) > ?",
        (time.time(), ttl_seconds),
    )
    conn.commit()
    conn.close()


# 初始化数据库
init_db()

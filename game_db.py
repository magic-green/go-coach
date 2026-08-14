"""棋局存档 —— SQLite 轻量存储，支持棋谱记录、历史列表、加载复盘。"""
import json
import os
import sqlite3
import time
from typing import Dict, List, Optional

import config
from core_logger import setup_logging

logger = setup_logging("game_db")

DB_PATH = os.path.join(config.PROJECT_ROOT, "games.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表（幂等）。"""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  REAL NOT NULL,
                board_size  INTEGER NOT NULL,
                komi        REAL NOT NULL,
                rules       TEXT NOT NULL DEFAULT 'chinese',
                mode        TEXT NOT NULL DEFAULT 'hva',
                ai_level    INTEGER NOT NULL DEFAULT 5,
                result      TEXT NOT NULL DEFAULT '',
                black_score REAL,
                white_score REAL,
                winner      TEXT,
                moves_json  TEXT NOT NULL DEFAULT '[]',
                metadata    TEXT NOT NULL DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_games_created
            ON games(created_at DESC)
        """)
    logger.info("棋局数据库就绪: %s", DB_PATH)


def save_game(
    board_size: int,
    komi: float,
    rules: str,
    mode: str,
    ai_level: int,
    result: str,
    black_score: Optional[float],
    white_score: Optional[float],
    winner: Optional[str],
    moves: List[Dict],
    metadata: Optional[Dict] = None,
) -> int:
    """保存一局棋谱，返回自增 ID。"""
    with _get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO games
               (created_at, board_size, komi, rules, mode, ai_level,
                result, black_score, white_score, winner,
                moves_json, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                board_size,
                komi,
                rules,
                mode,
                ai_level,
                result,
                black_score,
                white_score,
                winner,
                json.dumps(moves, ensure_ascii=False),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        gid = cur.lastrowid
        conn.commit()
    logger.info("棋谱已存档 id=%d moves=%d result=%s", gid, len(moves), result)
    return gid


def list_games(limit: int = 50) -> List[Dict]:
    """获取最近棋局列表（不含 moves 详情）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT id, created_at, board_size, komi, rules, mode,
                      ai_level, result, black_score, white_score, winner
               FROM games
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_game(gid: int) -> Optional[Dict]:
    """获取单局棋谱详情（含 moves）。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM games WHERE id = ?",
            (gid,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["moves"] = json.loads(d.pop("moves_json"))
    d["metadata"] = json.loads(d.pop("metadata"))
    return d


def delete_game(gid: int) -> bool:
    """删除一局棋谱。"""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM games WHERE id = ?", (gid,))
        conn.commit()
        return cur.rowcount > 0

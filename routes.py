"""API 路由：分析局面、AI 落子、健康检查 / 接入统计 / 排队。

每次 API 调用生成独立 request_id 贯穿日志；按阶段统计耗时并在结束时汇总。
对 KataGo 引擎（analyze / ai_move / lookahead / lookahead_trend / score）
采用全局串行锁 + 入队统计，避免多用户并发写 KataGo 子进程导致 GTP 乱序。
"""
import os
import time
import uuid

from flask import Blueprint, g, jsonify, request, after_this_request

import config
from core_logger import StageTimer, set_request_id, setup_logging, get_request_id
from commentary import generate_commentary
from engine.katago_engine import engine, gtp_to_xy
from engine.level import LEVEL_LABELS, get_ai_level_params
from engine.llm_client import (
    polish_commentary, make_moves_hash, ollama_available,
)
from engine.version_info import probe_katago_version
from game_db import init_db, save_game, list_games, get_game, delete_game

from access_stats import (
    real_client_ip, short_ua,
    record_request,
    engine_lock, queue_enqueue, queue_dequeue,
    engine_enter, engine_leave,
    snapshot as stats_snapshot,
    query_history as stats_history,
    reset_stats as stats_reset,
)

# GTP 列序（保持 katago_engine 一致）
_GTP_COLS = "ABCDEFGHJKLMNOPQRST"

bp = Blueprint("api", __name__, url_prefix="/api")
logger = setup_logging("api")

# 哪些 API 属于「重引擎调用」，需要进入全局引擎串行排队
_ENGINE_HEAVY_ENDPOINTS = {
    "/api/analyze",
    "/api/ai-move",
    "/api/lookahead",
    "/api/lookahead-trend",
    "/api/score",
}


def _current_req_id() -> str:
    return get_request_id() or ("g-" + uuid.uuid4().hex[:8])


# --- 全局请求钩子：统一分配 req id + 记录基础信息 ---
@bp.before_request
def _before_req():
    rid = set_request_id()
    g._start_ts = time.perf_counter()
    g._resp_status = 200  # 默认为 200；after_request 时用真实 status 覆盖
    ip = real_client_ip(request)
    ua = request.user_agent.string if request.user_agent else "-"
    g._client_ip = ip
    g._client_ua = ua
    logger.info(
        "REQ_IN method=%s path=%s from=%s ua=%s body=%dB",
        request.method, request.path,
        ip, short_ua(ua, 60),
        len(request.get_data(cache=True) or b""),
    )


@bp.after_request
def _after_req(resp):
    try:
        dt_ms = (time.perf_counter() - getattr(g, "_start_ts", time.perf_counter())) * 1000
        ip = getattr(g, "_client_ip", request.remote_addr or "-")
        ua = getattr(g, "_client_ua", "-")
        path = request.path
        sc = int(resp.status_code)
        record_request(path, ip, ua, dt_ms, sc)
        logger.info(
            "REQ_OUT method=%s path=%s status=%d elapsed_ms=%.0f from=%s",
            request.method, path, sc, dt_ms, ip,
        )
    except Exception as e:
        logger.warning("REQ_AFTER_HOOK_FAIL %s", e)
    return resp


# --- KataGo 引擎调用包装：自动排队 + 锁 + 统计 ---
def _run_heavy(endpoint_tag: str, func, *args, **kwargs):
    """串行执行重引擎调用，自动记排队/运行统计。

    用法：return _run_heavy("analyze", engine.analyze, moves, ...)
    """
    req_id = _current_req_id()
    ip = getattr(g, "_client_ip", "-")

    # 1) 先登记进入等待队列
    queue_enqueue(req_id, endpoint_tag, ip)

    # 2) 拿全局引擎锁（阻塞，按 Python 线程唤醒顺序 FIFO-ish）
    lock = engine_lock()
    wait_start = time.perf_counter()
    lock.acquire()
    wait_ms = int((time.perf_counter() - wait_start) * 1000)

    # 3) 出等待队列，记录等待时长
    queue_dequeue(req_id)

    # 4) 进入引擎运行态
    engine_ts = engine_enter(req_id, endpoint_tag, ip)
    logger.info(
        "ENGINE_RUN_BEGIN req=%s endpoint=%s wait_ms=%d queue_left=%d from=%s",
        req_id, endpoint_tag, wait_ms, _queue_len_now(), ip,
    )
    try:
        result = func(*args, **kwargs)
        run_ms = engine_leave(engine_ts)
        logger.info(
            "ENGINE_RUN_END req=%s endpoint=%s run_ms=%d wait_ms=%d from=%s",
            req_id, endpoint_tag, run_ms, wait_ms, ip,
        )
        return result
    except Exception:
        engine_leave(engine_ts)
        raise
    finally:
        lock.release()


def _queue_len_now() -> int:
    """读取引擎队列当前深度（用于日志）。"""
    try:
        s = stats_snapshot()
        return int(s.get("engine", {}).get("queue_len", 0))
    except Exception:
        return -1


def _parse_moves(data):
    """从请求体提取落子列表，保证字段完整。"""
    moves = data.get("moves", [])
    cleaned = []
    for m in moves:
        cleaned.append({
            "x": int(m["x"]),
            "y": int(m["y"]),
            "color": int(m.get("color", 1)),
        })
    return cleaned


# ------------------------------------------------------------------
@bp.route("/health", methods=["GET"])
def health():
    status = config.engine_status()
    ready = engine.is_ready()
    engine_lifecycle = engine.get_status() if hasattr(engine, "get_status") else {"state": status.get("mode", "unknown")}
    logger.debug(
        "HEALTH_CHECK engine_mode=%s ready=%s pid=%s lifecycle=%s",
        status["mode"], ready, os_pid(), engine_lifecycle.get("state"),
    )
    return jsonify({
        "status": "ok",
        "engine": status,
        "engine_ready": ready,
        "engine_lifecycle": engine_lifecycle,  # running / asleep / mock
        "app": {
            "name": config.APP_NAME,
            "version": config.APP_VERSION,
        },
        "katago": probe_katago_version(),
    })


# ------------------------------------------------------------------
@bp.route("/stats", methods=["GET"])
def stats():
    """接入统计 + 引擎队列状态快照。

    公共可访问（亲戚朋友也能打开看谁在排队）。
    """
    data = stats_snapshot()
    # 附加基本信息
    data["app"] = {
        "name": config.APP_NAME,
        "version": config.APP_VERSION,
        "server_ts": time.time(),
    }
    return jsonify({"ok": True, "stats": data})


# ------------------------------------------------------------------
@bp.route("/stats/history", methods=["GET"])
def stats_history_api():
    """查询历史接入记录（从持久化文件读取，最新在前）。

    参数：
      limit=N       （默认 100，最大 1000）
      offset=N      （默认 0，用于翻页）
      ip=xxx        （按 IP 模糊筛选）
      endpoint=xxx  （按端点名模糊筛选，如 /api/analyze）
      days=N        （扫描最近 N 天，默认 3，最大 30）
    """
    limit = min(int(request.args.get("limit", 100)), 1000)
    offset = max(int(request.args.get("offset", 0)), 0)
    ip_filter = request.args.get("ip") or None
    endpoint_filter = request.args.get("endpoint") or None
    days = min(int(request.args.get("days", 3)), 30)

    records = stats_history(
        limit=limit, offset=offset,
        ip_filter=ip_filter, endpoint_filter=endpoint_filter,
    )

    # 附加文件信息（哪些日期有记录）
    file_list = []
    for d in range(days):
        date_str = time.strftime("%Y-%m-%d", time.localtime(time.time() - d * 86400))
        fpath = os.path.join(config.PROJECT_ROOT, "logs", f"access.{date_str}.jsonl")
        try:
            size = os.path.getsize(fpath)
            file_list.append({"date": date_str, "size_bytes": size, "exists": True})
        except OSError:
            file_list.append({"date": date_str, "size_bytes": 0, "exists": False})

    return jsonify({
        "ok": True,
        "total": len(records),
        "limit": limit,
        "offset": offset,
        "records": records,
        "files": file_list,
    })


# ------------------------------------------------------------------
@bp.route("/stats/download", methods=["GET"])
def stats_download():
    """下载原始 access 日志文件（当日）。
    支持 ?date=2026-08-14 参数指定日期。
    """
    date_str = request.args.get("date") or time.strftime("%Y-%m-%d")
    fpath = os.path.join(config.PROJECT_ROOT, "logs", f"access.{date_str}.jsonl")
    if not os.path.isfile(fpath):
        return jsonify({"ok": False, "error": f"该日期无记录: {date_str}"}), 404

    from flask import send_file
    return send_file(
        fpath,
        as_attachment=True,
        download_name=f"access.{date_str}.jsonl",
        mimetype="application/x-ndjson",
    )


# ------------------------------------------------------------------
@bp.route("/stats/reset", methods=["POST"])
def stats_reset_api():
    """重置内存统计（不清除文件记录）。"""
    stats_reset()
    logger.info("STATS_RESET from=%s", getattr(g, "_client_ip", "-"))
    return jsonify({"ok": True, "message": "内存统计已重置，历史文件不受影响"})


def _moves_to_gtp(moves_list, size):
    """把 {x,y,color} 数组转成 GTP 字符串列表（供坐标语义识别 + LLM 缓存 hash）。"""
    result = []
    for m in moves_list:
        x = int(m["x"]); y = int(m["y"])
        if 0 <= x < size and 0 <= y < size:
            result.append(f"{_GTP_COLS[x]}{size - y}")
        else:
            result.append("pass")
    return result


# ------------------------------------------------------------------
@bp.route("/analyze", methods=["POST"])
def analyze():
    """分析当前局面，返回候选点 + 规则模板解说。排队串行调用 KataGo。"""
    t0 = time.perf_counter()
    timer = StageTimer(logger, "analyze")
    try:
        with timer("parse"):
            data = request.get_json(force=True, silent=False)
            moves = _parse_moves(data)
            board_size = int(data.get("boardSize", config.DEFAULT_BOARD_SIZE))
            komi = float(data.get("komi", config.DEFAULT_KOMI))
            rules = data.get("rules", config.DEFAULT_RULES)
            level = int(data.get("level", config.DEFAULT_AI_LEVEL))
            max_visits = int(data.get("maxVisits", 0))
            history_gtp = _moves_to_gtp(moves, board_size)

        logger.info(
            "ANALYZE_BEGIN moves=%d size=%dx%d komi=%.1f rules=%s level=%d maxVisits=%d from=%s",
            len(moves), board_size, board_size, komi, rules, level, max_visits,
            getattr(g, "_client_ip", "-"),
        )

        with timer("engine"):
            analysis = _run_heavy(
                "analyze",
                engine.analyze,
                moves, board_size, komi, rules, level,
                top_n=config.TOP_CANDIDATES,
                max_visits_override=max_visits,
            )

        with timer("commentary"):
            commentary = generate_commentary(
                analysis, len(moves), board_size,
                history_moves=history_gtp,
            )

        # 缓存 hash（用于 LLM 异步润色接口识别同一局面，避免重复生成）
        root_info = analysis.get("rootInfo") or {}
        moves_hash = make_moves_hash(history_gtp, board_size, level, root_info)
        llm_ok = ollama_available()

        with timer("jsonify"):
            resp = jsonify({
                "ok": True,
                "analysis": analysis,
                "commentary": commentary,
                "commentary_hash": moves_hash,
                "ollama_ready": llm_ok,
                "history_gtp": history_gtp,
            })

        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "ANALYZE_END total_ms=%d mode=%s cand_count=%d "
            "bestMove=%s root_wr=%.2f%% root_score=%+.2f to_move=%s ollama=%s",
            total_ms, analysis.get("mode"),
            len(analysis.get("moveInfos", [])),
            (analysis.get("moveInfos") or [{}])[0].get("move"),
            analysis.get("rootInfo", {}).get("winrate", 0) * 100,
            analysis.get("rootInfo", {}).get("scoreLead", 0),
            commentary.get("to_move", "-"),
            llm_ok,
        )
        timer.summary(
            moves=len(moves), size=board_size, level=level,
            mode=analysis.get("mode"), total_ms=total_ms,
        )
        return resp
    except Exception as e:
        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "ANALYZE_ERR total_ms=%d moves=%d err=%s",
            total_ms, len(data.get("moves", [])) if "data" in locals() else -1, e,
        )
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------
@bp.route("/commentary-enhance", methods=["POST"])
def commentary_enhance():
    """异步 LLM 润色解说（不走引擎锁，不占用 KataGo）。"""
    t0 = time.perf_counter()
    try:
        data = request.get_json(force=True, silent=False)
        moves_hash = data.get("commentary_hash") or ""
        if not moves_hash:
            return jsonify({"ok": False, "error": "缺少 commentary_hash"}), 400

        if moves_hash in config.LLM_CACHE:
            logger.info("LLM_CACHE_HIT hash=%s", moves_hash[:8])
            return jsonify({
                "ok": True,
                "polished": config.LLM_CACHE[moves_hash],
                "source_cache": True,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            })

        outline = data.get("commentary")
        if not outline:
            return jsonify({
                "ok": False,
                "error": "未命中缓存，请带 commentary 重试（或重新请求 /api/analyze）",
            }), 400

        required = ["summary", "position", "candidate_strategies",
                    "comparison", "strategy"]
        if any(k not in outline for k in required):
            return jsonify({"ok": False, "error": "commentary 结构不完整"}), 400

        logger.info("LLM_POLISH_BEGIN hash=%s from=%s",
                    moves_hash[:8], getattr(g, "_client_ip", "-"))
        polished = polish_commentary(outline, moves_hash)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if polished:
            logger.info("LLM_POLISH_END hash=%s elapsed_ms=%d",
                        moves_hash[:8], elapsed_ms)
            return jsonify({
                "ok": True,
                "polished": polished,
                "source_cache": False,
                "elapsed_ms": elapsed_ms,
            })
        else:
            return jsonify({
                "ok": False,
                "error": "LLM 润色不可用（保留原规则解说即可）",
                "elapsed_ms": elapsed_ms,
            }), 503
    except Exception as e:
        logger.exception("LLM_ENHANCE_ERR: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------
@bp.route("/ai-move", methods=["POST"])
def ai_move():
    """AI 落子请求（重引擎调用，排队）。"""
    t0 = time.perf_counter()
    timer = StageTimer(logger, "ai-move")
    try:
        with timer("parse"):
            data = request.get_json(force=True, silent=False)
            moves = _parse_moves(data)
            board_size = int(data.get("boardSize", config.DEFAULT_BOARD_SIZE))
            komi = float(data.get("komi", config.DEFAULT_KOMI))
            rules = data.get("rules", config.DEFAULT_RULES)
            level = int(data.get("level", config.DEFAULT_AI_LEVEL))
            max_visits = int(data.get("maxVisits", 0))

        logger.info(
            "AI_MOVE_BEGIN moves=%d size=%dx%d komi=%.1f rules=%s level=%d(%s) maxVisits=%d from=%s",
            len(moves), board_size, board_size, komi, rules, level,
            LEVEL_LABELS.get(level, ""), max_visits,
            getattr(g, "_client_ip", "-"),
        )

        with timer("pick"):
            move = _run_heavy(
                "ai-move",
                engine.pick_ai_move,
                moves, board_size, komi, rules, level,
                max_visits_override=max_visits,
            )

        with timer("meta"):
            params = get_ai_level_params(level)
            resp_payload = {
                "ok": True,
                "move": move,
                "level": level,
                "level_label": LEVEL_LABELS.get(level, ""),
                "params": params,
            }

        with timer("jsonify"):
            resp = jsonify(resp_payload)

        total_ms = int((time.perf_counter() - t0) * 1000)
        gtp = "-" if (move.get("x", -1) < 0) else engine_xy_to_gtp(move["x"], move["y"], board_size)
        logger.info(
            "AI_MOVE_END total_ms=%d pick=%s(%s) color=%d wr=%.2f%% score=%+.2f",
            total_ms, gtp, (move.get("x"), move.get("y")), move.get("color", 0),
            move.get("winrate", 0) * 100, move.get("scoreLead", 0),
        )
        timer.summary(
            moves=len(moves), size=board_size, level=level,
            pick=gtp, total_ms=total_ms,
        )
        return resp
    except Exception as e:
        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(
            "AI_MOVE_ERR total_ms=%d moves=%d err=%s",
            total_ms, len(data.get("moves", [])) if "data" in locals() else -1, e,
        )
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------
@bp.route("/lookahead", methods=["POST"])
def lookahead():
    """10 步深度推演（重引擎调用，排队）。"""
    t0 = time.perf_counter()
    try:
        data = request.get_json(force=True, silent=False)
        moves = _parse_moves(data)
        board_size = int(data.get("boardSize", config.DEFAULT_BOARD_SIZE))
        komi = float(data.get("komi", config.DEFAULT_KOMI))
        rules = data.get("rules", config.DEFAULT_RULES)
        level = int(data.get("level", config.DEFAULT_AI_LEVEL))
        candidate = data.get("candidate", "")

        logger.info(
            "LOOKAHEAD_BEGIN moves=%d size=%dx%d cand=%s level=%d from=%s",
            len(moves), board_size, board_size, candidate, level,
            getattr(g, "_client_ip", "-"),
        )

        result = _run_heavy(
            "lookahead",
            engine.lookahead,
            moves, board_size, komi, rules, candidate, level,
        )

        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "LOOKAHEAD_END total_ms=%d cand=%s pv_len=%d pv=%s",
            total_ms, candidate, len(result.get("pv", [])),
            (result.get("pv", []))[:6],
        )
        result["elapsed_ms"] = total_ms
        return jsonify(result)
    except Exception as e:
        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception("LOOKAHEAD_ERR total_ms=%d err=%s", total_ms, e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------
@bp.route("/lookahead-trend", methods=["POST"])
def lookahead_trend():
    """沿 PV 逐步重分析胜率趋势（重引擎调用，排队）。"""
    t0 = time.perf_counter()
    try:
        data = request.get_json(force=True, silent=False)
        moves = _parse_moves(data)
        board_size = int(data.get("boardSize", config.DEFAULT_BOARD_SIZE))
        komi = float(data.get("komi", config.DEFAULT_KOMI))
        rules = data.get("rules", config.DEFAULT_RULES)
        level = int(data.get("level", config.DEFAULT_AI_LEVEL))
        pv = data.get("pv", []) or []
        if not isinstance(pv, list) or not pv:
            return jsonify({"ok": False, "error": "缺少 pv 序列"}), 400

        logger.info(
            "LOOKAHEAD_TREND_BEGIN moves=%d size=%dx%d pv_len=%d level=%d from=%s",
            len(moves), board_size, board_size, len(pv), level,
            getattr(g, "_client_ip", "-"),
        )

        result = _run_heavy(
            "lookahead-trend",
            engine.lookahead_trend,
            moves, board_size, komi, rules, pv, level,
        )

        total_ms = int((time.perf_counter() - t0) * 1000)
        result["elapsed_ms"] = total_ms
        logger.info(
            "LOOKAHEAD_TREND_END total_ms=%d steps=%d mode=%s",
            total_ms, result.get("steps", 0), result.get("mode"),
        )
        return jsonify(result)
    except Exception as e:
        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception("LOOKAHEAD_TREND_ERR total_ms=%d err=%s", total_ms, e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------
@bp.route("/levels", methods=["GET"])
def levels():
    result = []
    for lv in range(1, 21):
        result.append({
            "level": lv,
            "label": LEVEL_LABELS.get(lv, str(lv)),
            "params": get_ai_level_params(lv),
        })
    return jsonify({"ok": True, "levels": result})


# ------------------------------------------------------------------
@bp.route("/score", methods=["POST"])
def score():
    """终局数子（基于 KataGo ownership，重引擎调用，排队）。"""
    t0 = time.perf_counter()
    try:
        data = request.get_json(force=True, silent=False)
        moves = _parse_moves(data)
        board_size = int(data.get("boardSize", config.DEFAULT_BOARD_SIZE))
        komi = float(data.get("komi", config.DEFAULT_KOMI))
        rules = data.get("rules", config.DEFAULT_RULES)

        logger.info("SCORE_BEGIN moves=%d size=%dx%d komi=%.1f from=%s",
                     len(moves), board_size, board_size, komi,
                     getattr(g, "_client_ip", "-"))

        result = _run_heavy(
            "score",
            engine.score_estimate,
            moves, board_size, komi, rules,
        )

        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("SCORE_END total_ms=%d winner=%s diff=%+.1f",
                     total_ms, result["winner"], result["diff"])
        result["elapsed_ms"] = total_ms
        return jsonify(result)
    except Exception as e:
        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception("SCORE_ERR total_ms=%d err=%s", total_ms, e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------
@bp.route("/games", methods=["GET", "POST"])
def games():
    """棋局存档：GET 列表 / POST 保存（不走引擎锁）。"""
    if request.method == "GET":
        limit = int(request.args.get("limit", 50))
        games_list = list_games(limit=limit)
        return jsonify({"ok": True, "games": games_list})

    t0 = time.perf_counter()
    try:
        data = request.get_json(force=True, silent=False)
        moves = _parse_moves(data)
        board_size = int(data.get("boardSize", config.DEFAULT_BOARD_SIZE))
        komi = float(data.get("komi", config.DEFAULT_KOMI))
        rules = data.get("rules", config.DEFAULT_RULES)
        mode = data.get("mode", "hva")
        ai_level = int(data.get("level", config.DEFAULT_AI_LEVEL))
        result_str = data.get("result", "")
        black_score = data.get("blackScore")
        white_score = data.get("whiteScore")
        winner = data.get("winner")
        metadata = data.get("metadata", {})

        gid = save_game(
            board_size, komi, rules, mode, ai_level,
            result_str, black_score, white_score, winner,
            moves, metadata,
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("GAME_SAVED id=%d moves=%d elapsed_ms=%d from=%s",
                     gid, len(moves), elapsed_ms,
                     getattr(g, "_client_ip", "-"))
        return jsonify({"ok": True, "id": gid, "elapsed_ms": elapsed_ms})
    except Exception as e:
        logger.exception("GAME_SAVE_ERR: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/games/<int:gid>", methods=["GET", "DELETE"])
def game_detail(gid):
    """获取或删除单局棋谱。"""
    if request.method == "DELETE":
        ok = delete_game(gid)
        return jsonify({"ok": ok})
    game = get_game(gid)
    if not game:
        return jsonify({"ok": False, "error": "棋局不存在"}), 404
    return jsonify({"ok": True, "game": game})


# ---- helpers ----
def os_pid():
    import os
    return os.getpid()


def engine_xy_to_gtp(x, y, size):
    # 轻量 helper，避免引入 engine.katago_engine 循环依赖
    from engine.katago_engine import GTP_COLUMNS as _G
    if x < 0 or x >= size:
        return "pass"
    return f"{_G[x]}{size - y}"

"""接入情况 + 队列状态统计（内存版 + 文件持久化）。

对外提供：
  record_request(endpoint, real_ip, ua, elapsed_ms, status_code)
  record_queue_wait(endpoint, wait_ms)
  record_engine_call(endpoint, run_ms)
  snapshot() -> dict
  query_history(limit=100, offset=0, **filters) -> list
"""
import json
import os
import threading
import time
from collections import defaultdict, deque

import config

_STAT_LOCK = threading.Lock()
_started_at = time.time()

# ---- 文件持久化（access.jsonl，按天滚动） ----
_ACCESS_LOG_DIR = os.path.join(config.PROJECT_ROOT, "logs")
_ACCESS_LOG_PREFIX = "access"
_ACCESS_LOG_RETENTION_DAYS = 60  # 保留 60 天
_ACCESS_LOG_LOCK = threading.Lock()
_current_date = time.strftime("%Y-%m-%d", time.localtime(_started_at))
_current_fh = None  # 当前文件句柄，懒打开

# ---- 请求接入统计 ----
_total_requests = 0
_total_errors = 0
_total_elapsed_ms = 0
_per_endpoint = defaultdict(lambda: {"count": 0, "errors": 0, "elapsed_ms": 0})
_per_ip = defaultdict(lambda: {"count": 0, "errors": 0, "last_ts": 0.0})
_per_ua = defaultdict(lambda: {"count": 0, "last_ts": 0.0})

# Top-K 滑动窗口：最近 N 条请求，便于看实时动态
_RECENT_WINDOW = 200
_recent_requests = deque(maxlen=_RECENT_WINDOW)

# ---- KataGo 引擎排队统计 ----
_queue_total = 0              # 累计经历过排队的请求数
_queue_total_wait_ms = 0      # 累计等待时间
_engine_total_calls = 0       # 累计进入引擎的调用次数
_engine_total_run_ms = 0      # 累计引擎计算耗时
_engine_current = None        # 当前占用引擎的请求：{"req_id","endpoint","start_ts","ip"}
_engine_queue = deque()       # 当前等待队列（存 {"req_id","endpoint","enqueue_ts","ip"}）
_engine_lock = threading.Lock()  # 全局引擎串行锁（真正防并发写 KataGo 的锁）


def real_client_ip(request) -> str:
    """在 Cloudflare + 反向代理链下拿真实客户端 IP。

    优先级：CF-Connecting-IP > X-Forwarded-For(最左) > X-Real-IP > remote_addr
    """
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()

    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first

    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri.strip()

    return request.remote_addr or "0.0.0.0"


def short_ua(ua: str, limit: int = 80) -> str:
    if not ua:
        return "-"
    return ua if len(ua) <= limit else ua[: limit - 3] + "..."


# ---- 文件持久化 ----
def _access_log_path(date_str: str = None) -> str:
    """返回指定日期的 JSONL 文件路径。"""
    d = date_str or time.strftime("%Y-%m-%d")
    return os.path.join(_ACCESS_LOG_DIR, f"{_ACCESS_LOG_PREFIX}.{d}.jsonl")


def _rotate_access_log():
    """检查日期是否变更，若变更则关闭旧文件句柄。"""
    global _current_date, _current_fh
    today = time.strftime("%Y-%m-%d")
    if today != _current_date:
        if _current_fh:
            try:
                _current_fh.close()
            except Exception:
                pass
        _current_date = today
        _current_fh = None


def _cleanup_old_access_logs():
    """删除超过保留天数的旧 access 日志文件。"""
    now = time.time()
    cutoff = now - _ACCESS_LOG_RETENTION_DAYS * 86400
    try:
        for fname in os.listdir(_ACCESS_LOG_DIR):
            if fname.startswith(_ACCESS_LOG_PREFIX + ".") and fname.endswith(".jsonl"):
                fpath = os.path.join(_ACCESS_LOG_DIR, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    if mtime < cutoff:
                        os.remove(fpath)
                except Exception:
                    pass
    except Exception:
        pass


def _write_access_record(record: dict):
    """线程安全地追加一条 JSONL 记录到当前日期文件。"""
    global _current_fh
    with _ACCESS_LOG_LOCK:
        _rotate_access_log()
        if _current_fh is None:
            fpath = _access_log_path(_current_date)
            _current_fh = open(fpath, "a", encoding="utf-8")
            # 每 30 次写入做一次清理检查（避免频繁 stat）
            if getattr(_current_fh, "_cleanup_counter", 0) % 30 == 0:
                _cleanup_old_access_logs()
            _current_fh._cleanup_counter = getattr(_current_fh, "_cleanup_counter", 0) + 1
        _current_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        _current_fh.flush()


def record_request(endpoint: str, real_ip: str, ua: str,
                   elapsed_ms: float, status_code: int):
    global _total_requests, _total_errors, _total_elapsed_ms
    with _STAT_LOCK:
        _total_requests += 1
        _total_elapsed_ms += max(0, int(elapsed_ms))
        is_err = 0 if (200 <= status_code < 500) else 1
        if is_err:
            _total_errors += 1

        ep = _per_endpoint[endpoint]
        ep["count"] += 1
        ep["errors"] += is_err
        ep["elapsed_ms"] += max(0, int(elapsed_ms))

        ip = _per_ip[real_ip]
        ip["count"] += 1
        ip["errors"] += is_err
        ip["last_ts"] = time.time()

        u = _per_ua[ua]
        u["count"] += 1
        u["last_ts"] = time.time()

        now = time.time()
        _recent_requests.append({
            "ts": now,
            "endpoint": endpoint,
            "ip": real_ip,
            "ua": short_ua(ua, 60),
            "ms": max(0, int(elapsed_ms)),
            "status": status_code,
        })

        # ---- 持久化到文件 ----
        _write_access_record({
            "ts": now,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "endpoint": endpoint,
            "ip": real_ip,
            "ua": ua,
            "ms": max(0, int(elapsed_ms)),
            "status": status_code,
        })


def record_queue_wait(endpoint: str, wait_ms: float):
    global _queue_total, _queue_total_wait_ms
    with _STAT_LOCK:
        _queue_total += 1
        _queue_total_wait_ms += max(0, int(wait_ms))


def engine_enter(req_id: str, endpoint: str, ip: str) -> float:
    """记录引擎进入（返回当前时间戳，方便调用者算出耗时）。"""
    global _engine_total_calls
    with _STAT_LOCK:
        _engine_total_calls += 1
        now = time.time()
        global _engine_current
        _engine_current = {
            "req_id": req_id,
            "endpoint": endpoint,
            "start_ts": now,
            "ip": ip,
        }
        return now


def engine_leave(start_ts: float):
    """记录引擎离开，并累加计算耗时。"""
    global _engine_total_run_ms, _engine_current
    run_ms = max(0, int((time.time() - start_ts) * 1000))
    with _STAT_LOCK:
        _engine_total_run_ms += run_ms
        _engine_current = None
    return run_ms


def queue_enqueue(req_id: str, endpoint: str, ip: str) -> float:
    """加入引擎等待队列（只做统计，不阻塞调用者），返回入队时间。"""
    enq = time.time()
    with _STAT_LOCK:
        _engine_queue.append({
            "req_id": req_id,
            "endpoint": endpoint,
            "enqueue_ts": enq,
            "ip": ip,
        })
    return enq


def queue_dequeue(req_id: str):
    """离开等待队列（拿到锁后调用）。返回等待时长 ms。"""
    enq = 0.0
    with _STAT_LOCK:
        # 找到对应项并移除（可能顺序不完全一致，按 req_id 精确删除）
        for i, item in enumerate(list(_engine_queue)):
            if item["req_id"] == req_id:
                enq = item["enqueue_ts"]
                del _engine_queue[i]
                break
    wait_ms = max(0, int((time.time() - enq) * 1000)) if enq else 0
    if wait_ms:
        record_queue_wait("engine", wait_ms)
    return wait_ms


def engine_lock():
    """返回全局引擎串行锁（调用者用 with 语句 acquire/release）。"""
    return _engine_lock


def snapshot() -> dict:
    with _STAT_LOCK:
        now = time.time()
        uptime_s = int(now - _started_at)
        avg_ms = int(_total_elapsed_ms / _total_requests) if _total_requests else 0

        top_ips = sorted(
            _per_ip.items(), key=lambda kv: kv[1]["count"], reverse=True
        )[:15]
        top_uas = sorted(
            _per_ua.items(), key=lambda kv: kv[1]["count"], reverse=True
        )[:10]
        endpoints = sorted(
            _per_endpoint.items(), key=lambda kv: kv[1]["count"], reverse=True
        )

        ep_list = []
        for name, s in endpoints:
            avg = int(s["elapsed_ms"] / s["count"]) if s["count"] else 0
            ep_list.append({
                "endpoint": name,
                "count": s["count"],
                "errors": s["errors"],
                "avg_ms": avg,
                "total_ms": s["elapsed_ms"],
            })

        ip_list = [
            {
                "ip": ip,
                "count": s["count"],
                "errors": s["errors"],
                "last_seen_s": int(now - s["last_ts"]),
            }
            for ip, s in top_ips
        ]

        ua_list = [
            {
                "ua": short_ua(ua, 80),
                "count": s["count"],
                "last_seen_s": int(now - s["last_ts"]),
            }
            for ua, s in top_uas
        ]

        recent = list(_recent_requests)
        recent.reverse()  # 最新在最前

        # 引擎运行中
        cur = None
        if _engine_current:
            cur = {
                "req_id": _engine_current["req_id"],
                "endpoint": _engine_current["endpoint"],
                "ip": _engine_current["ip"],
                "run_for_ms": int((now - _engine_current["start_ts"]) * 1000),
            }

        queue_list = [
            {
                "req_id": x["req_id"],
                "endpoint": x["endpoint"],
                "ip": x["ip"],
                "wait_ms": int((now - x["enqueue_ts"]) * 1000),
            }
            for x in list(_engine_queue)
        ]

        avg_wait_ms = int(_queue_total_wait_ms / _queue_total) if _queue_total else 0
        avg_run_ms = int(_engine_total_run_ms / _engine_total_calls) if _engine_total_calls else 0

    return {
        "uptime_s": uptime_s,
        "requests": {
            "total": _total_requests,
            "errors": _total_errors,
            "avg_ms": avg_ms,
            "total_elapsed_ms": _total_elapsed_ms,
        },
        "endpoints": ep_list,
        "top_ips": ip_list,
        "top_uas": ua_list,
        "recent": recent,
        "engine": {
            "current": cur,
            "queue": queue_list,
            "queue_len": len(queue_list),
            "calls_total": _engine_total_calls,
            "avg_run_ms": avg_run_ms,
            "total_run_ms": _engine_total_run_ms,
            "wait_total": _queue_total,
            "avg_wait_ms": avg_wait_ms,
            "total_wait_ms": _queue_total_wait_ms,
        },
    }


def query_history(limit: int = 100, offset: int = 0,
                  ip_filter: str = None, endpoint_filter: str = None,
                  since_ts: float = None, until_ts: float = None) -> list:
    """从 access 日志文件查询历史记录（倒序，最新在前）。

    不指定日期则从今天开始反向扫描（最多 3 天）。
    """
    results = []
    today = time.strftime("%Y-%m-%d")
    # 扫描最近 date_scan_days 天的文件
    date_scan_days = 3
    scanned = 0
    for day_offset in range(date_scan_days):
        if len(results) >= limit + offset:
            break
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - day_offset * 86400))
        fpath = _access_log_path(d)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # 行内倒序（当日文件最新行在末尾）
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 过滤
                if since_ts and rec.get("ts", 0) < since_ts:
                    continue
                if until_ts and rec.get("ts", 0) > until_ts:
                    continue
                if ip_filter and ip_filter not in rec.get("ip", ""):
                    continue
                if endpoint_filter and endpoint_filter not in rec.get("endpoint", ""):
                    continue
                results.append(rec)
                scanned += 1
                if len(results) >= limit + offset:
                    break
        except Exception:
            continue
    # 倒序（最新在前），取分页
    results.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return results[offset:offset + limit]


def reset_stats():
    """重置所有内存统计（不清除日志文件）。"""
    global _total_requests, _total_errors, _total_elapsed_ms
    global _queue_total, _queue_total_wait_ms
    global _engine_total_calls, _engine_total_run_ms
    global _engine_current, _started_at
    _per_endpoint.clear()
    _per_ip.clear()
    _per_ua.clear()
    _recent_requests.clear()
    _engine_queue.clear()
    with _STAT_LOCK:
        _total_requests = 0
        _total_errors = 0
        _total_elapsed_ms = 0
        _queue_total = 0
        _queue_total_wait_ms = 0
        _engine_total_calls = 0
        _engine_total_run_ms = 0
        _engine_current = None
        _started_at = time.time()

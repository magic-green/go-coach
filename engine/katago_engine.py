"""KataGo 分析引擎封装。

通过 analysis 模式与 KataGo 子进程通信（stdin 写 JSON 查询，stdout 读行分隔 JSON 响应）。
未检测到 KataGo 二进制/模型时，自动降级为 Mock 模式，返回模拟分析数据，
使前端与解说流程可独立测试。
"""
import json
import os
import random
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

import config
from core_logger import setup_logging, StageTimer
from engine.level import get_ai_level_params

logger = setup_logging("engine")

# GTP 列字母（跳过 I）
GTP_COLUMNS = "ABCDEFGHJKLMNOPQRST"


def xy_to_gtp(x: int, y: int, board_size: int) -> str:
    """(x, y) 坐标转 GTP 字符串。x=列(0起), y=行(0起, 0=顶部)"""
    if x < 0 or x >= board_size or y < 0 or y >= board_size:
        return "pass"
    col = GTP_COLUMNS[x]
    row = board_size - y  # GTP 行号从底部 1 开始
    return f"{col}{row}"


def gtp_to_xy(move: str, board_size: int) -> Tuple[int, int]:
    """GTP 字符串转 (x, y)。x=列(0起), y=行(0起, 0=顶部)"""
    if not move or move.lower() in ("pass", "resign"):
        return (-1, -1)
    col_char = move[0].upper()
    x = GTP_COLUMNS.index(col_char)
    row_num = int(move[1:])
    y = board_size - row_num
    return (x, y)


def moves_to_gtp(moves: List[Dict], board_size: int) -> List[List[str]]:
    """前端 moves [{'x','y','color'}] -> KataGo [['B','D4'], ...]"""
    result = []
    for m in moves:
        color = "B" if m.get("color", 1) == 1 else "W"
        gtp = xy_to_gtp(int(m["x"]), int(m["y"]), board_size)
        result.append([color, gtp])
    return result


class KataGoEngine:
    """KataGo analysis 模式封装（含 Mock 降级）。

    特性：
      - 懒启动：首次 analyze/lookahead 调用时才启动 KataGo 子进程（节约显存）
      - 空闲自动休眠：IDLE_TIMEOUT_SEC 内无调用自动终止子进程释放显存
      - 按需重启：休眠后再次调用会重新拉起
    """

    IDLE_TIMEOUT_SEC = 5 * 60  # 5 分钟空闲 -> 终止 KataGo 进程

    def __init__(self):
        self._force_mode = "katago" if config.katago_available() else "mock"
        # 当前实际运行模式（katago 子进程启动后才是 "katago"，否则休眠时按 mock 但其实没运行）
        self.mode = self._force_mode
        self._proc = None
        self._lock = threading.Lock()
        self._query_id = 0
        # 休眠/懒加载控制
        self._last_call_ts = 0.0          # 最后一次调用 analyze/lookahead 的时间戳
        self._idle_watcher = None         # 后台巡检线程
        self._watcher_stop = threading.Event()
        self._process_launch_lock = threading.Lock()  # 进程启动锁，避免并发启动多个
        logger.info("引擎初始化 检测结果 katago_available=%s force_mode=%s",
                    config.katago_available(), self._force_mode)
        if self._force_mode == "mock":
            logger.info("未检测到 KataGo 二进制/模型，使用 Mock 模式（模拟分析数据）")
            self.mode = "mock"
        else:
            # 懒启动：先不启动子进程，第一次调用再启动
            logger.info("KataGo 就绪但采用懒启动：首次 analyze/ai-move 调用时拉起进程")
            self.mode = "katago-asleep"  # 表示"有 KataGo 但没启动（休眠）"
            self._start_idle_watcher()

    # ------------------------------------------------------------------
    # 进程管理
    # ------------------------------------------------------------------
    def _start_idle_watcher(self):
        """后台线程：每 30 秒巡检一次，超过 IDLE_TIMEOUT_SEC 无操作则停进程"""
        if self._idle_watcher and self._idle_watcher.is_alive():
            return
        self._watcher_stop.clear()

        def _watcher_loop():
            logger.info("KataGo 空闲巡检线程启动（timeout=%ds）", self.IDLE_TIMEOUT_SEC)
            while not self._watcher_stop.is_set():
                self._watcher_stop.wait(30)  # 每 30 秒检查一次
                if self._watcher_stop.is_set():
                    break
                try:
                    self._check_idle_and_sleep()
                except Exception as e:
                    logger.warning("空闲巡检异常: %s", e)
            logger.info("KataGo 空闲巡检线程退出")

        t = threading.Thread(target=_watcher_loop, daemon=True, name="katago-idle-watcher")
        t.start()
        self._idle_watcher = t

    def _check_idle_and_sleep(self):
        """超过 IDLE_TIMEOUT_SEC 无调用，且子进程还活着 -> 终止"""
        if self._proc is None:
            return
        if self._proc.poll() is not None:  # 已经退出了
            self._proc = None
            if self._force_mode == "katago":
                self.mode = "katago-asleep"
            return
        idle = time.time() - self._last_call_ts
        if self._last_call_ts > 0 and idle >= self.IDLE_TIMEOUT_SEC:
            pid = self._proc.pid
            logger.info("KataGo 空闲 %.0fs >= %ds，终止进程 PID=%s 释放显存",
                        idle, self.IDLE_TIMEOUT_SEC, pid)
            self._stop_process()

    def _ensure_process(self) -> bool:
        """确保 KataGo 子进程启动（懒加载入口）。True=已就绪"""
        if self._force_mode == "mock":
            return False  # mock 模式不需要进程
        # 已经启动且还活着
        if self._proc is not None and self._proc.poll() is None:
            self._last_call_ts = time.time()
            return True
        # 需要启动（加锁防并发）
        with self._process_launch_lock:
            # double-check：加锁期间可能已被其他线程启动
            if self._proc is not None and self._proc.poll() is None:
                self._last_call_ts = time.time()
                return True
            # 如果之前还活着但已退出 -> 清理
            if self._proc is not None:
                self._proc = None
            logger.info("按需拉起 KataGo 进程（懒启动触发）")
            self._start_process()
            if self._proc is not None and self._proc.poll() is None:
                self.mode = "katago"
                self._last_call_ts = time.time()
                return True
            # 启动失败（可能降级 mock）
            return False

    def _start_process(self):
        """启动 KataGo analysis 子进程"""
        cmd = [
            config.KATAGO_EXE,
            "analysis",
            "-config", config.KATAGO_CONFIG,
            "-model", config.KATAGO_MODEL,
        ]
        t_start = time.perf_counter()
        try:
            logger.info("启动 KataGo 子进程 exe=%s config=%s cwd=%s",
                        os.path.basename(config.KATAGO_EXE),
                        os.path.basename(config.KATAGO_CONFIG),
                        config.KATAGO_DIR)
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # 行缓冲
                cwd=config.KATAGO_DIR,
            )
            pid = self._proc.pid
            logger.info("KataGo 子进程已启动 PID=%s 启动耗时=%.2fs",
                        pid, time.perf_counter() - t_start)
            # 启动日志线程，把 stderr 转发到 logger（便于排障）
            t = threading.Thread(
                target=self._drain_stderr,
                args=(pid,),
                daemon=True,
            )
            t.start()
        except Exception as e:
            logger.error("KataGo 启动失败，降级为 Mock 模式: %s", e)
            self.mode = "mock"
            self._proc = None

    def _drain_stderr(self, pid):
        if not self._proc or not self._proc.stderr:
            return
        for line in self._proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            # 日志级别分流：包含 error/warn/fail 用 WARNING，其他 DEBUG
            low = line.lower()
            if any(k in low for k in ("error", "warn", "fail", "abort", "exception", "oom")):
                logger.warning("[katago-pid%s] %s", pid, line)
            else:
                logger.debug("[katago-pid%s] %s", pid, line)

    def _stop_process(self):
        """终止 KataGo 子进程并释放显存"""
        old_pid = None
        try:
            if self._proc is not None and self._proc.poll() is None:
                old_pid = self._proc.pid
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.stdout.close()
                except Exception:
                    pass
                try:
                    self._proc.stderr.close()
                except Exception:
                    pass
                # 先温柔 SIGTERM
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 5 秒不退就强杀
                    logger.warning("KataGo PID=%s terminate 5s 超时，强杀", old_pid)
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=3)
                    except Exception:
                        pass
                logger.info("KataGo 进程已终止 PID=%s", old_pid)
        except Exception as e:
            logger.warning("KataGo 进程终止异常: %s", e)
        finally:
            self._proc = None
            if self._force_mode == "katago":
                # 休眠状态：等待下次调用再拉起来
                self.mode = "katago-asleep"

    def is_ready(self) -> bool:
        if self._force_mode == "mock":
            return True
        # "katago-asleep" 也算 ready（因为有进程会按需拉起）
        if self.mode == "katago-asleep":
            return True
        return self._proc is not None and self._proc.poll() is None

    def get_status(self) -> Dict:
        """供健康检查使用：进程运行/休眠/mock"""
        if self._force_mode == "mock":
            return {"state": "mock"}
        if self.mode == "katago-asleep":
            return {"state": "asleep", "idle_timeout_sec": self.IDLE_TIMEOUT_SEC}
        if self._proc is not None and self._proc.poll() is None:
            idle = max(0, int(time.time() - self._last_call_ts))
            remain = max(0, self.IDLE_TIMEOUT_SEC - idle) if self._last_call_ts > 0 else self.IDLE_TIMEOUT_SEC
            return {"state": "running", "pid": self._proc.pid,
                    "idle_sec": idle, "remain_sec": remain}
        return {"state": "asleep", "idle_timeout_sec": self.IDLE_TIMEOUT_SEC}

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def analyze(self, moves: List[Dict], board_size: int, komi: float,
                rules: str, level: int, top_n: int = 5,
                max_visits_override: int = 0) -> Dict:
        """分析当前局面，返回候选点与根信息（含阶段计时日志）。

        Args:
            max_visits_override: >0 时覆盖等级对应的 maxVisits（用户手动选算力）
        """
        t_start = time.perf_counter()
        params = get_ai_level_params(level)
        if max_visits_override and max_visits_override > 0:
            params = dict(params)  # 浅拷贝，不修改等级映射表
            params["maxVisits"] = max_visits_override
        move_count = len(moves)
        last_move = moves[-1] if moves else None

        # 按需拉起进程（会刷新 last_call_ts，重置空闲计时）
        katago_ready = self._ensure_process()
        # 判断当前该用哪个模式
        run_mode = "katago" if katago_ready else "mock"

        if run_mode == "katago":
            try:
                result = self._katago_analyze(moves, board_size, komi, rules, params, top_n)
            except Exception as e:
                logger.warning("KataGo 分析失败，降级 Mock: %s", e)
                result = self._mock_analyze(moves, board_size, komi, params, top_n)
        else:
            result = self._mock_analyze(moves, board_size, komi, params, top_n)

        # 汇总关键指标日志（用于性能排查：耗时/候选点数量/首点目差/visits）
        elapsed = time.perf_counter() - t_start
        root = result.get("rootInfo", {})
        infos = result.get("moveInfos", [])
        best = infos[0] if infos else {}
        next_p = "B" if (move_count % 2 == 0) else "W"
        last_gtp = ""
        if last_move:
            last_gtp = xy_to_gtp(int(last_move["x"]), int(last_move["y"]), board_size)
        logger.info(
            "ENGINE_ANALYZE elapsed_ms=%d mode=%s moves=%d last=%s next=%s "
            "size=%dx%d level=%d maxVisits=%d temp=%.2f "
            "root_wr=%.2f%% scoreLead=%+.2f visits=%d "
            "cands=%d bestMove=%s bestWr=%.2f%% bestScore=%+.2f",
            int(elapsed * 1000), result.get("mode"),
            move_count, last_gtp, next_p,
            board_size, board_size, level, params["maxVisits"], params["temperature"],
            root.get("winrate", 0) * 100, root.get("scoreLead", 0), root.get("visits", 0),
            len(infos), best.get("move"), best.get("winrate", 0) * 100, best.get("scoreLead", 0),
        )
        # 性能告警：Mock 模式下阈值 500ms，KataGo 下 > ANALYZE_TIMEOUT/2
        warn_ms = 500 if result.get("mode") == "mock" else config.ANALYZE_TIMEOUT * 1000 / 2
        if elapsed * 1000 > warn_ms:
            logger.warning(
                "ENGINE_ANALYZE_SLOW %.0fms (超过阈值 %.0fms) mode=%s moves=%d",
                elapsed * 1000, warn_ms, result.get("mode"), move_count,
            )
        return result

    def pick_ai_move(self, moves: List[Dict], board_size: int, komi: float,
                     rules: str, level: int, max_visits_override: int = 0) -> Dict:
        """让 AI 选一步棋。低等级按 temperature 加权随机；高等级取第一选点。"""
        t_start = time.perf_counter()
        timer = StageTimer(logger, "engine-pick")
        with timer("analyze"):
            result = self.analyze(moves, board_size, komi, rules, level, top_n=5,
                                  max_visits_override=max_visits_override)
        infos = result.get("moveInfos", [])
        if not infos:
            timer.summary(move_count=len(moves), level=level, status="no_candidates")
            return {"x": -1, "y": -1, "color": 2, "winrate": 0.5, "scoreLead": 0.0}

        params = get_ai_level_params(level)
        temp = params["temperature"]
        chosen = None
        with timer("select"):
            if temp > 0.01 and len(infos) > 1:
                weights = []
                for info in infos[:5]:
                    v = max(1, info.get("visits", 1))
                    weights.append(v ** (1.0 / max(temp, 0.1)))
                chosen = random.choices(infos[:5], weights=weights, k=1)[0]
            else:
                chosen = infos[0]

        x, y = gtp_to_xy(chosen["move"], board_size)
        # move_count=0(空盘) 轮到黑=1；move_count=1 轮到白=2
        next_color = 1 if (len(moves) % 2 == 0) else 2
        elapsed = time.perf_counter() - t_start
        logger.info(
            "ENGINE_AI_MOVE elapsed_ms=%d mode=%s moves=%d next=%s level=%d "
            "pick=%s(wr=%.2f%% score=%+.2f order=%d) "
            "temperature=%.2f strategy=%s",
            int(elapsed * 1000), result.get("mode"),
            len(moves), "B" if next_color == 1 else "W", level,
            chosen.get("move"), chosen.get("winrate", 0) * 100,
            chosen.get("scoreLead", 0), chosen.get("order", 0),
            temp, "weighted_random" if (temp > 0.01 and len(infos) > 1) else "top1",
        )
        timer.summary(
            move_count=len(moves), level=level,
            pick=chosen.get("move"), elapsed_ms=int(elapsed * 1000),
        )
        return {
            "x": x, "y": y, "color": next_color,
            "winrate": chosen.get("winrate", 0.5),
            "scoreLead": chosen.get("scoreLead", 0.0),
        }

    def lookahead(self, moves: List[Dict], board_size: int, komi: float,
                  rules: str, candidate_gtp: str, level: int) -> Dict:
        """对指定候选点做深度推演：用较高 maxVisits 重新分析，返回该候选点的 pv（最多 10 步）。

        Args:
            candidate_gtp: 候选点的 GTP 坐标，如 "Q16"
        Returns:
            {"ok": True, "pv": ["Q16","D4",...], "move": "Q16", "visits": N}
        """
        t_start = time.perf_counter()
        # 推演用更高 visits 以获得更深的 pv（不受用户等级限制）
        deep_params = {"maxVisits": 500, "temperature": 0.0}
        top_n = 10

        # 按需拉起进程（刷新 last_call_ts）
        katago_ready = self._ensure_process()
        run_mode = "katago" if katago_ready else "mock"

        if run_mode == "katago":
            try:
                result = self._katago_analyze(moves, board_size, komi, rules, deep_params, top_n)
            except Exception as e:
                logger.warning("KataGo 推演失败，降级 Mock: %s", e)
                result = self._mock_analyze(moves, board_size, komi, deep_params, top_n)
        else:
            result = self._mock_analyze(moves, board_size, komi, deep_params, top_n)

        # 找到候选点的 pv
        pv = []
        chosen_visits = 0
        for mi in result.get("moveInfos", []):
            if mi.get("move", "").upper() == candidate_gtp.upper():
                pv = mi.get("pv", [])[:10]
                chosen_visits = mi.get("visits", 0)
                break

        # 如果没找到精确匹配，取首选项的 pv
        if not pv and result.get("moveInfos"):
            pv = result["moveInfos"][0].get("pv", [])[:10]
            chosen_visits = result["moveInfos"][0].get("visits", 0)

        elapsed = time.perf_counter() - t_start
        logger.info(
            "ENGINE_LOOKAHEAD elapsed_ms=%d mode=%s moves=%d cand=%s "
            "maxVisits=%d pv_len=%d visits=%d pv=%s",
            int(elapsed * 1000), result.get("mode"), len(moves), candidate_gtp,
            deep_params["maxVisits"], len(pv), chosen_visits, pv[:6],
        )
        return {
            "ok": True,
            "pv": pv,
            "move": candidate_gtp,
            "visits": chosen_visits,
            "mode": result.get("mode"),
            "elapsed_ms": int(elapsed * 1000),
        }

    # ------------------------------------------------------------------
    # 10 步推演胜率趋势：沿 PV 逐步重分析，收集每步后的黑方胜率/目差
    # ------------------------------------------------------------------
    def lookahead_trend(self, moves: List[Dict], board_size: int, komi: float,
                        rules: str, pv: List[str], level: int) -> Dict:
        """沿 PV 序列逐步重分析，返回每步后的黑方胜率（折线趋势用）。

        Args:
            pv: 已确定的 PV（GTP 坐标列表，如 ["Q16","D4",...]）
        Returns:
            {"ok": True, "winrates": [0.52, 0.49, ...], "scores": [3.1, 1.2, ...],
             "mode": "katago"/"mock", "steps": N}
            winrates/scores[i] = 走完 pv[0..i] 后局面的黑方胜率/目差。
            （KataGo 配置 reportAnalysisWinratesAs=BLACK，winrate 已是黑方视角）
        """
        t_start = time.perf_counter()
        # 趋势只取胜率估算，用低 visits 换速度（CUDA 约 0.3~0.4s/步）
        trend_params = {"maxVisits": 48, "temperature": 0.0}
        max_steps = min(len(pv), 10)

        katago_ready = self._ensure_process()
        run_mode = "katago" if katago_ready else "mock"

        winrates: List[float] = []
        scores: List[float] = []

        # 当前行棋方：moves 为空 → 黑(1)；否则按手数奇偶
        cur_color = 1 if (len(moves) % 2 == 0) else 2
        running_moves = list(moves)  # 浅拷贝，逐步追加 PV 中的手

        for i in range(max_steps):
            mv = pv[i]
            if not mv or mv.lower() in ("pass", "resign"):
                break
            xy = gtp_to_xy(mv, board_size)
            if xy[0] < 0:
                break
            running_moves.append({"x": xy[0], "y": xy[1], "color": cur_color})
            cur_color = 2 if cur_color == 1 else 1

            if run_mode == "katago":
                try:
                    result = self._katago_analyze(
                        running_moves, board_size, komi, rules, trend_params, top_n=1)
                except Exception as e:
                    # PV 中出现非法手（理论上不会发生，因 PV 来自 KataGo 自身）：
                    # 立即终止趋势，返回已收集的部分结果，避免后续步骤全部失败
                    logger.warning("KataGo 趋势第 %d 步失败，终止趋势: %s", i, e)
                    break
            else:
                result = self._mock_analyze(running_moves, board_size, komi, trend_params, 1)

            root = result.get("rootInfo", {})
            winrates.append(round(float(root.get("winrate", 0.5)), 4))
            scores.append(round(float(root.get("scoreLead", 0.0)), 2))

        elapsed = time.perf_counter() - t_start
        logger.info(
            "ENGINE_LOOKAHEAD_TREND elapsed_ms=%d mode=%s steps=%d winrates=%s",
            int(elapsed * 1000), run_mode, len(winrates),
            [round(w * 100, 1) for w in winrates],
        )
        return {
            "ok": True,
            "winrates": winrates,
            "scores": scores,
            "mode": run_mode,
            "steps": len(winrates),
            "elapsed_ms": int(elapsed * 1000),
        }

    # ------------------------------------------------------------------
    # 终局数子（中国规则，基于 KataGo ownership 数据估算）
    # ------------------------------------------------------------------
    def score_estimate(self, moves: List[Dict], board_size: int,
                       komi: float, rules: str) -> Dict:
        """用高 visits 分析 + ownership 数据估算终局比分（中国规则）。

        返回:
            {"ok": True, "black": float, "white": float, "winner": "B"/"W",
             "diff": float, "mode": "katago"/"mock",
             "ownership": [...], "board_size": N, "komi": float}
        """
        deep_params = {"maxVisits": 500, "temperature": 0.0}
        katago_ready = self._ensure_process()
        run_mode = "katago" if katago_ready else "mock"

        if run_mode == "katago":
            try:
                result = self._katago_analyze(moves, board_size, komi, rules,
                                              deep_params, top_n=1)
            except Exception as e:
                logger.warning("KataGo 数子失败，降级 Mock: %s", e)
                result = self._mock_analyze(moves, board_size, komi, deep_params, 1)
        else:
            result = self._mock_analyze(moves, board_size, komi, deep_params, 1)

        owner = result.get("ownerData")
        black_pts = 0.0
        white_pts = 0.0

        if owner and isinstance(owner, list) and len(owner) == board_size * board_size:
            # KataGo ownership: 正值=黑控制，负值=白控制
            for v in owner:
                if v > 0:
                    black_pts += 1
                else:
                    white_pts += 1
        else:
            # 无 ownership 数据：按棋盘上棋子数粗估
            for m in moves:
                if int(m.get("color", 1)) == 1:
                    black_pts += 1
                else:
                    white_pts += 1

        white_total = white_pts + komi
        black_total = black_pts
        diff = black_total - white_total
        winner = "B" if diff > 0 else "W"

        logger.info(
            "SCORE_ESTIMATE mode=%s black=%.1f white=%.1f(含贴目%.1f) diff=%+.1f winner=%s",
            result.get("mode"), black_total, white_total, komi, diff, winner,
        )
        return {
            "ok": True,
            "black": round(black_total, 1),
            "white": round(white_total, 1),
            "black_stones": black_pts,
            "white_stones": white_pts,
            "komi": komi,
            "diff": round(diff, 1),
            "winner": winner,
            "mode": result.get("mode"),
            "ownership": owner,
            "board_size": board_size,
        }

    # ------------------------------------------------------------------
    # KataGo 真实分析
    # ------------------------------------------------------------------
    def _katago_analyze(self, moves, board_size, komi, rules, params, top_n):
        timer = StageTimer(logger, "engine-katago")
        with timer("lock"):
            # 记录等锁耗时：并发分析时这步可能显著
            self._lock.acquire()
        try:
            with timer("gtp"):
                gtp_moves = moves_to_gtp(moves, board_size)
                self._query_id += 1
                qid = f"q{self._query_id}"
            with timer("build_query"):
                query = {
                    "id": qid,
                    "moves": gtp_moves,
                    "rules": rules,
                    "komi": komi,
                    "boardXSize": board_size,
                    "boardYSize": board_size,
                    "analyzeTurns": [len(gtp_moves)],
                    "maxVisits": params["maxVisits"],
                    "includeOwnership": True,
                    "includePolicy": True,
                    # v1.17.x analysis 模式下，搜索/评分参数需通过 overrideSettings 子对象注入
                    # （顶层 temperature 是 unused field 会触发警告，且不生效）
                    # wideRootNoise 控制搜索的探索性：>0.03 会严重劣化搜索质量，
                    # 导致 AI 在复杂局面下漏算低级错误甚至自杀手。
                    # 因此这里只取一个很小的值 (0~0.02)，真正的棋力差异由 maxVisits + pick_ai_move 的随机加权决定。
                    "overrideSettings": {
                        "wideRootNoise": min(0.02, params["temperature"] * 0.02),
                    },
                }
                payload = json.dumps(query) + "\n"
            with timer("stdin_send"):
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()

            # 读取响应行，匹配 id。注意：KataGo 可能先返回同 id 的 warning/error 行，
            # 只有包含 rootInfo/moveInfos 的行才是真正的分析结果。
            recv_lines = 0
            with timer("stdout_recv"):
                deadline = time.time() + config.ANALYZE_TIMEOUT
                resp = None
                while time.time() < deadline:
                    line = self._proc.stdout.readline()
                    if not line:
                        raise RuntimeError("KataGo 进程输出已关闭")
                    recv_lines += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if j.get("id") != qid:
                        continue
                    # KataGo 错误响应（如非法落子）：立即抛出，避免空等到超时
                    if "error" in j:
                        raise RuntimeError("KataGo 拒绝查询: %s" % j.get("error", "未知错误"))
                    # 跳过警告/中间响应；只有包含 rootInfo 才算分析结果
                    if "rootInfo" in j or "moveInfos" in j:
                        resp = j
                        break
            with timer("format"):
                if resp is None:
                    raise RuntimeError("KataGo 分析超时")
                result = self._format_result(resp, top_n, mode="katago")
        finally:
            self._lock.release()

        timer.summary(
            qid=qid, moves=len(moves), maxVisits=params["maxVisits"],
            temp=params["temperature"], recv_lines=recv_lines,
            best_visits=result.get("rootInfo", {}).get("visits", 0),
        )
        return result

    @staticmethod
    def _format_result(resp: Dict, top_n: int, mode: str) -> Dict:
        """提取 KataGo 原始响应中的关键字段"""
        move_infos = resp.get("moveInfos", [])[:top_n]
        # 按 order 排序（0 = 最优）
        move_infos.sort(key=lambda m: m.get("order", 999))
        # ownerData: 扁平数组，row-major，值 正=黑势/负=白势
        owner = resp.get("ownership",
                         resp.get("owner", resp.get("ownerData", None)))
        # policy: 策略网络先验概率，长度 = N*N + 1（末尾是 pass），值 0~1
        policy = resp.get("policy")
        board_x = int(resp.get("boardXSize") or resp.get("boardYSize") or 19)
        if isinstance(policy, list):
            policy = [round(float(p), 6) for p in policy]

        # 补全 moveInfos[i].policyPrior：
        # KataGo v1.17.x 有时 moveInfos 内的 policyPrior 为 null，此时从根 policy 数组按坐标反查
        def _lookup_prior(move_gtp):
            if not isinstance(policy, list) or not move_gtp:
                return None
            # 解析 move_gtp -> (x, y)
            if move_gtp.lower() in ("pass", "resign"):
                if len(policy) == board_x * board_x + 1:
                    return round(policy[-1], 6)
                return None
            try:
                col = move_gtp[0].upper()
                row_num = int(move_gtp[1:])
                x = GTP_COLUMNS.index(col)
                y = board_x - row_num  # row 1 = y = N-1
            except Exception:
                return None
            if not (0 <= x < board_x and 0 <= y < board_x):
                return None
            idx = y * board_x + x
            if 0 <= idx < len(policy):
                return round(policy[idx], 6)
            return None

        formatted_moves = []
        for m in move_infos:
            prior = None
            if m.get("policyPrior") is not None:
                try:
                    prior = round(float(m["policyPrior"]), 6)
                except Exception:
                    prior = None
            if prior is None or prior <= 0:
                prior = _lookup_prior(m.get("move", ""))
            formatted_moves.append({
                "move": m.get("move", ""),
                "winrate": m.get("winrate", 0.5),
                "scoreLead": m.get("scoreLead", 0.0),
                "visits": m.get("visits", 0),
                "order": m.get("order", 0),
                "pv": m.get("pv", [])[:10],
                "policyPrior": prior,
            })

        return {
            "mode": mode,
            "rootInfo": {
                "winrate": resp.get("rootInfo", {}).get("winrate", 0.5),
                "scoreLead": resp.get("rootInfo", {}).get("scoreLead", 0.0),
                "visits": resp.get("rootInfo", {}).get("visits", 0),
            },
            "moveInfos": formatted_moves,
            "ownerData": owner,
            "policyData": policy,
        }

    # ------------------------------------------------------------------
    # Mock 分析（无 KataGo 时使用）
    # ------------------------------------------------------------------
    def _mock_analyze(self, moves, board_size, komi, params, top_n):
        """生成模拟分析数据：空位候选点 + 合理的胜率/目数差排序。

        与真实 KataGo 保持一致：winrate / scoreLead 一律为"黑视角"，
        按"行棋方视角最优"重排 order，并且 visits 与胜率严格对应。
        """
        timer = StageTimer(logger, "engine-mock")
        with timer("occupy_build"):
            occupied = set()
            black_count = 0
            white_count = 0
            last_xy = None
            for m in moves:
                occupied.add((int(m["x"]), int(m["y"])))
                if int(m.get("color", 1)) == 1:
                    black_count += 1
                else:
                    white_count += 1
                last_xy = (int(m["x"]), int(m["y"]))

        with timer("candidates"):
            candidates = self._mock_candidates(occupied, last_xy, board_size)
            # 固定开局 1-4 手强制回到星位/小目，不随机
            if len(moves) == 0:
                # 黑第一手：右上角星位附近（R16 / Q16）
                star = board_size // 2 if board_size % 2 == 1 else board_size // 2 - 1
                right_up = (board_size - 4, 3)
                left_up = (3, 3)
                right_down = (board_size - 4, board_size - 4)
                tengen = (star, star)
                prefer = []
                for p in (right_up, left_up, right_down, tengen):
                    if p not in occupied and p not in prefer:
                        prefer.append(p)
                candidates = prefer + [c for c in candidates if c not in prefer]
            elif len(moves) == 1:
                # 白应手：对角线星位
                b = moves[0]
                bx, by = int(b["x"]), int(b["y"])
                mirror = (board_size - 1 - bx, board_size - 1 - by)
                star = board_size // 2 if board_size % 2 == 1 else board_size // 2 - 1
                alt = [(3, board_size - 4), (board_size - 4, board_size - 4),
                       (3, 3), (board_size - 4, 3), (star, star)]
                prefer = []
                for p in (mirror, *alt):
                    if p not in occupied and p not in prefer:
                        prefer.append(p)
                candidates = prefer + [c for c in candidates if c not in prefer]

            candidates = candidates[:max(top_n + 3, 6)]

        with timer("sleep_sim"):
            time.sleep(0.15)  # 模拟思考延时

        with timer("gen_scores"):
            # move_count=0 → 黑下=1；move_count=1 → 白下=2
            next_color = 1 if (len(moves) % 2 == 0) else 2
            next_black = (next_color == 1)

            # 基础胜率（黑视角，带合理的开局贴目偏移）
            # 空盘/开局：黑胜率约 40-50%（贴 7.5 目对白略微友好）
            step_bias = (random.random() - 0.5) * 0.06
            base_black_wr = 0.46 + step_bias
            if len(moves) >= 6:
                base_black_wr += (random.random() - 0.5) * 0.05
            base_black_wr = max(0.18, min(0.82, base_black_wr))
            # 黑视角基准目差：胜率 50% ≈ 0 目，每 ±10% 胜率 ≈ ±3 目
            base_black_score = (base_black_wr - 0.5) * 30 + random.uniform(-2, 2)

            # 给每个候选点一个"行棋方视角"的评分差
            # 规则：candidates[0] 是首选 → side_score_delta 最大正值
            # 相邻名次差距约 0.8~1.8 目，首选比末尾高 n*1.2 目左右
            cands_side = []
            n = len(candidates)
            # 把 gap_base 设好，idx 越小（越优）side_score_delta 越大
            for idx, (cx, cy) in enumerate(candidates):
                # idx=0 → gap = (n-1)*1.2 (最大正值)
                # idx=n-1 → gap = 0
                rank_gap = (n - 1 - idx) * (1.0 + random.random() * 0.5)
                noise = (random.random() - 0.5) * 0.4
                side_score_delta = rank_gap + noise - ((n - 1) * 0.6)
                # 行棋方视角胜率：3目 ≈ 10%
                side_wr_delta = side_score_delta / 30.0 + (random.random() - 0.5) * 0.01
                # visits：首选最多，约占总数的 30%~40%
                base_visits = max(20, params["maxVisits"])
                decay = 0.72 ** idx  # 指数衰减：首名 1.0, 次 0.72, 第三 0.52...
                visits = int(base_visits * (0.30 + 0.70 * decay))
                visits = max(5, visits + random.randint(-3, 3))
                cands_side.append({
                    "xy": (cx, cy),
                    "side_score_delta": side_score_delta,
                    "side_wr_delta": side_wr_delta,
                    "visits": visits,
                })

            # 转成黑视角输出
            move_infos = []
            for c in cands_side:
                cx, cy = c["xy"]
                if next_black:
                    wr = base_black_wr + c["side_wr_delta"]
                    sc = base_black_score + c["side_score_delta"]
                else:
                    wr = base_black_wr - c["side_wr_delta"]
                    sc = base_black_score - c["side_score_delta"]
                wr = max(0.05, min(0.95, wr))
                move_infos.append({
                    "move": xy_to_gtp(cx, cy, board_size),
                    "winrate": round(wr, 4),
                    "scoreLead": round(sc, 2),
                    "visits": c["visits"],
                    "pv": self._mock_pv(cx, cy, next_color, board_size, occupied),
                })

            # 按"行棋方视角最优"重排，保证 order=0 真正最优
            # 黑下 → 黑视角 scoreLead 越大越优；白下 → 黑视角 scoreLead 越小（越负）对白越优
            def _rank_key(m):
                return m["scoreLead"] if next_black else -m["scoreLead"]
            move_infos.sort(key=_rank_key, reverse=True)
            for i, m in enumerate(move_infos):
                m["order"] = i

            # 构造 rootInfo（黑视角基准）
            # ownerData: 模拟势力分布，与 KataGo 一致采用 [-1,+1] 约定
            # （正值=黑控制，负值=白控制，0=中立），保证 board.js 渲染与数子逻辑正确
            owner = [0.0] * (board_size * board_size)
            for mx, my in [(int(m["x"]), int(m["y"])) for m in moves]:
                c = int(m.get("color", 1))
                sign = 1.0 if c == 1 else -1.0  # 黑+白-
                idx = my * board_size + mx
                # 以该点为中心扩散影响
                for dy in range(-3, 4):
                    for dx in range(-3, 4):
                        ny, nx = my + dy, mx + dx
                        if 0 <= ny < board_size and 0 <= nx < board_size:
                            dist = max(abs(dx), abs(dy))
                            if dist <= 3:
                                ni = ny * board_size + nx
                                influence = sign * (1.0 - dist * 0.25)
                                if c == 1:
                                    owner[ni] = max(owner[ni], influence)  # 黑：向上推（正值）
                                else:
                                    owner[ni] = min(owner[ni], influence)  # 白：向下推（负值）
            # 有棋子的位置归零（不显示势力）
            for mx, my in [(int(m["x"]), int(m["y"])) for m in moves]:
                owner[my * board_size + mx] = 0.0
            owner = [round(max(-1.0, min(1.0, v)), 3) for v in owner]

            # policyData: 模拟策略网络先验概率，长度 N*N+1（最后是 pass）
            # 候选点优先级越高，policyPrior 越大；总和归一化到 1
            n_total = board_size * board_size + 1
            policy_raw = [0.0] * n_total
            # 基于 move_infos（排序后）分配先验，首名约 25~35%，按 0.55^rank 衰减
            for rank, mi in enumerate(move_infos):
                mv_gtp = mi["move"]
                mx, my = gtp_to_xy(mv_gtp, board_size)
                if 0 <= mx < board_size and 0 <= my < board_size:
                    pidx = my * board_size + mx
                    weight = (0.58 ** rank) * (0.30 + random.random() * 0.10)
                    policy_raw[pidx] = weight
            # 空位给一点噪声（避免全 0）
            for i in range(board_size * board_size):
                if policy_raw[i] <= 0:
                    iy, ix = divmod(i, board_size)
                    if (ix, iy) not in occupied:
                        policy_raw[i] = random.random() * 0.003
            # pass 概率很小
            policy_raw[-1] = random.random() * 0.005
            # 归一化
            psum = sum(policy_raw)
            if psum > 0:
                policy_data = [round(p / psum, 6) for p in policy_raw]
            else:
                policy_data = [round(1.0 / n_total, 6)] * n_total
            # 给 move_infos 补 policyPrior（从 policy_data 查）
            for mi in move_infos:
                mv_gtp = mi["move"]
                mx, my = gtp_to_xy(mv_gtp, board_size)
                if 0 <= mx < board_size and 0 <= my < board_size:
                    mi["policyPrior"] = policy_data[my * board_size + mx]
                else:
                    mi["policyPrior"] = policy_data[-1]

            result = {
                "mode": "mock",
                "rootInfo": {
                    "winrate": round(base_black_wr, 4),
                    "scoreLead": round(base_black_score, 2),
                    "visits": params["maxVisits"],
                },
                "moveInfos": move_infos[:top_n],
                "ownerData": owner,
                "policyData": policy_data,
            }
        timer.summary(
            moves=len(moves), maxVisits=params["maxVisits"],
            cands=len(candidates), next="B" if next_black else "W",
        )
        return result

    @staticmethod
    def _mock_pv(cx, cy, first_color, board_size, occupied):
        """生成模拟的 10 步推演序列（黑白交替，逐步远离起手点）。"""
        pv = [xy_to_gtp(cx, cy, board_size)]
        used = set(occupied)
        used.add((cx, cy))
        cur_x, cur_y = cx, cy
        color = first_color
        offsets = [(1, 0), (0, 1), (-1, 0), (0, -1),
                   (1, 1), (-1, 1), (1, -1), (-1, -1),
                   (2, 0), (0, 2)]
        for i in range(9):
            ox, oy = offsets[i % len(offsets)]
            step = (i // len(offsets)) + 1
            nx = max(0, min(board_size - 1, cur_x + ox * step))
            ny = max(0, min(board_size - 1, cur_y + oy * step))
            attempts = 0
            while (nx, ny) in used and attempts < 8:
                nx = max(0, min(board_size - 1, nx + 1))
                ny = max(0, min(board_size - 1, ny + 1))
                attempts += 1
            if (nx, ny) in used:
                break
            used.add((nx, ny))
            pv.append(xy_to_gtp(nx, ny, board_size))
            cur_x, cur_y = nx, ny
            color = 2 if color == 1 else 1
        return pv

    @staticmethod
    def _mock_candidates(occupied, last_xy, board_size):
        """生成候选落子点：开局走星位，中盘在已落子附近。"""
        corners = []
        star = board_size // 2 if board_size % 2 == 1 else board_size // 2 - 1
        offs = [3, star, board_size - 4] if board_size >= 13 else [2, star, board_size - 3]
        for ox in offs:
            for oy in offs:
                if (ox, oy) not in occupied:
                    corners.append((ox, oy))

        if not occupied:
            # 空盘：返回星位
            return corners[:6]

        cands = list(corners)
        # 在最后一手附近生成候选（扩展棋型）
        if last_xy:
            lx, ly = last_xy
            radius = 3 if board_size >= 13 else 2
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = lx + dx, ly + dy
                    if 0 <= nx < board_size and 0 <= ny < board_size and (nx, ny) not in occupied:
                        cands.append((nx, ny))
        # 补充随机空位
        attempts = 0
        while len(cands) < 8 and attempts < 50:
            attempts += 1
            rx, ry = random.randint(0, board_size - 1), random.randint(0, board_size - 1)
            if (rx, ry) not in occupied and (rx, ry) not in cands:
                cands.append((rx, ry))
        return cands


# 全局单例
engine = KataGoEngine()

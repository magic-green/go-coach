"""统一日志配置：控制台 + 文件双 handler，阶段计时工具，请求 ID 生成。"""
import logging
import logging.handlers
import os
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from typing import Dict, Optional

import config

# ---- 全局 Context ----
_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")

_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-7s | pid=%(process)-5d | req=%(request_id)s | "
    "%(name)-16s | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def set_request_id(rid: Optional[str] = None) -> str:
    """设置当前请求 ID，返回新 ID。"""
    if rid is None:
        rid = uuid.uuid4().hex[:8]
    _REQUEST_ID.set(rid)
    return rid


def get_request_id() -> str:
    return _REQUEST_ID.get()


class _RequestIdFilter(logging.Filter):
    """把 ContextVar 中的 request_id 注入到 log record。"""

    def filter(self, record):
        record.request_id = get_request_id()
        return True


# ---- 日志初始化 ----
_INIT_LOCK = threading.Lock()
_INITIALIZED = False

LOG_DIR = os.path.join(config.PROJECT_ROOT, "logs")
LOG_FILE = os.path.join(LOG_DIR, "go-coach.log")


def setup_logging(name: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    """初始化并返回命名 logger。重复调用安全（只配一次 handler）。"""
    global _INITIALIZED
    with _INIT_LOCK:
        if not _INITIALIZED:
            os.makedirs(LOG_DIR, exist_ok=True)
            root = logging.getLogger()
            root.setLevel(level)
            # 清理默认 handler（避免重复）
            for h in list(root.handlers):
                root.removeHandler(h)

            formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
            rid_filter = _RequestIdFilter()

            # 控制台 handler（Windows 下 stdout 即可）
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(level)
            ch.setFormatter(formatter)
            ch.addFilter(rid_filter)
            root.addHandler(ch)

            # 文件 handler：每日滚动，保留 14 天，UTF-8
            fh = logging.handlers.TimedRotatingFileHandler(
                LOG_FILE,
                when="midnight",
                interval=1,
                backupCount=14,
                encoding="utf-8",
            )
            fh.setLevel(level)
            fh.setFormatter(formatter)
            fh.addFilter(rid_filter)
            root.addHandler(fh)

            _INITIALIZED = True
            # 启动引导日志，便于定位日志落点
            logger = logging.getLogger("go-coach")
            logger.info(
                "=" * 50
            )
            logger.info(
                "日志系统启动 PID=%s  LOG_FILE=%s  level=%s",
                os.getpid(),
                LOG_FILE,
                logging.getLevelName(level),
            )

    return logging.getLogger(name or "go-coach")


# ---- 阶段计时（stage 字典 + 最终一次性汇总） ----
class StageTimer:
    """性能埋点：按阶段累计耗时，结束时统一输出一条汇总日志。

    Usage:
        t = StageTimer(logger, "analyze")
        with t("parse"):
            ...
        with t("engine"):
            ...
        t.summary(move_count=12, level=5, ...)
    """

    def __init__(self, logger: logging.Logger, label: str):
        self.logger = logger
        self.label = label
        self.times: Dict[str, float] = {}
        self._cur = None
        self._t0_all = time.perf_counter()

    def stage(self, name: str):
        return self._StageCtx(self, name)

    # 支持 with timer("xxx") 语法
    def __call__(self, name: str):
        return self.stage(name)

    class _StageCtx:
        def __init__(self, outer, name: str):
            self.outer = outer
            self.name = name

        def __enter__(self):
            self.outer._cur = self.name
            self.outer.times.setdefault(self.name, 0.0)
            self._t = time.perf_counter()

        def __exit__(self, exc_type, exc, tb):
            dt = time.perf_counter() - self._t
            self.outer.times[self.name] = self.outer.times[self.name] + dt
            # 单阶段超过 200ms 单独打一条 WARN，便于立即察觉异常
            if dt >= 0.2:
                self.outer.logger.warning(
                    "STAGE_SLOW %s.%s=%.3fs",
                    self.outer.label, self.name, dt,
                )
            return False

    def summary(self, **extra) -> float:
        """打印汇总日志，返回总耗时（秒）。"""
        total = time.perf_counter() - self._t0_all
        # 阶段明细字符串，按耗时降序排列
        sorted_stages = sorted(self.times.items(), key=lambda kv: kv[1], reverse=True)
        stages_str = " ".join(
            f"{n}={d*1000:.0f}ms" for n, d in sorted_stages
        )
        extras_str = " ".join(f"{k}={v}" for k, v in extra.items())
        self.logger.info(
            "SUMMARY %s total=%.3fs stages=[%s] %s",
            self.label, total, stages_str, extras_str,
        )
        return total

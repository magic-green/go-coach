"""围棋 AI 教练 - 全局配置"""
import os

# App 版本号
APP_VERSION = "1.2.0"
APP_NAME = "简思围棋教室"

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# KataGo 引擎目录
KATAGO_DIR = os.path.join(PROJECT_ROOT, "katago")
# 解压后的二进制与模型（用户已经把 zip 和模型下载好，我们移到了 katago/ 目录）
KATAGO_EXE = os.path.join(KATAGO_DIR, "katago.exe")
# 模型文件：kata1-b28c512nbt 系列（b28 对日常教学/工具站最合适，速度快 + Elo 14110 足够）
KATAGO_MODEL = os.path.join(KATAGO_DIR, "kata1-b28c512nbt.bin.gz")
# 配置：默认 KataGo 官方 default_gtp.cfg，叠加我们定制的 analysis_override.cfg（两份都会加载）
KATAGO_DEFAULT_CFG = os.path.join(KATAGO_DIR, "default_gtp.cfg")
KATAGO_OVERRIDE_CFG = os.path.join(KATAGO_DIR, "analysis_override.cfg")
# 真正传给 KataGo 的 -config：有 override 用 override（它内部可以 include default），否则直接用 default
if os.path.isfile(KATAGO_OVERRIDE_CFG):
    KATAGO_CONFIG = KATAGO_OVERRIDE_CFG
else:
    KATAGO_CONFIG = KATAGO_DEFAULT_CFG if os.path.isfile(KATAGO_DEFAULT_CFG) else os.path.join(KATAGO_DIR, "analysis.cfg")

# Flask 服务（被 jsai-server 反代，用 5001 避免和其他本地服务冲突）
HOST = "127.0.0.1"
PORT = 5001
DEBUG = False  # 工具站生产环境关闭 debug，降低日志量

# 围棋默认参数
DEFAULT_BOARD_SIZE = 19
DEFAULT_KOMI = 7.5
DEFAULT_RULES = "chinese"

# AI 默认等级（1-20）
DEFAULT_AI_LEVEL = 5

# 分析候选点数量
TOP_CANDIDATES = 5

# 引擎单步分析超时（秒）——真实 KataGo 在弱 GPU 上可能较慢
ANALYZE_TIMEOUT = 120

# ---------- Ollama LLM 配置（混合方案：规则模板 + LLM 教学口吻润色）----------
# 地址和端口（Ollama 默认 11434）
OLLAMA_BASE_URL = os.environ.get("GO_COACH_OLLAMA_URL", "http://127.0.0.1:11434")
# 使用的模型名称（qwen2.5 中文强）
OLLAMA_MODEL = os.environ.get("GO_COACH_OLLAMA_MODEL", "qwen2.5:7b-32k")
# 单次生成超时（秒）——7B CPU 推理可能慢，给充足时间
OLLAMA_TIMEOUT = 90
# 生成温度（越低越保守，润色教学口吻用 0.2 稳定）
OLLAMA_TEMPERATURE = 0.2
# 是否启用 LLM 增强（False 则只用规则模板，0 延迟）
LLM_ENABLED = os.environ.get("GO_COACH_LLM", "") != "0"

# LLM 增强缓存：同一 hand_hash（按棋盘+候选点）算过就不用再让 LLM 跑
LLM_CACHE = {}  # hand_hash -> 润色后的解说 dict

# 是否强制走 Mock 模式：工具站先上线，避免 KataGo DLL/运行时依赖缺失时卡死。
# 默认：只要 KataGo 二进制/模型/配置就位，就启用真实 KataGo 分析。
# 如需强制 Mock（例如工具站 CI 环境），设置环境变量 GO_COACH_FORCE_MOCK=1。
FORCE_MOCK = os.environ.get("GO_COACH_FORCE_MOCK", "") == "1"
FORCE_KATAGO = os.environ.get("GO_COACH_FORCE_KATAGO", "") == "1"


def katago_available():
    """检测 KataGo 引擎和模型是否就位；若显式 FORCE_MOCK=1 则永远返回 False。"""
    if FORCE_MOCK:
        return False
    # 只要文件就位就启用；FORCE_KATAGO=1 可作为兜底强制开关
    engine_ok = (
        os.path.isfile(KATAGO_EXE)
        and os.path.isfile(KATAGO_MODEL)
        and os.path.isfile(KATAGO_CONFIG)
    )
    return engine_ok or FORCE_KATAGO


def engine_status():
    """返回引擎状态描述，供健康检查与前端展示。
    后端类型优先依据 katago version 命令真实输出（CUDA/Eigen），再 fallback 到 cfg 文本。
    """
    if katago_available():
        # 尝试用真实版本输出判断后端
        backend_desc_cfg = "eigen CPU"
        try:
            cfg_path = KATAGO_CONFIG
            if os.path.isfile(cfg_path):
                with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        s = line.strip()
                        if s.lower().startswith("backend") and "=" in s:
                            v = s.split("=", 1)[1].strip().lower()
                            if "eigen" in v: backend_desc_cfg = "eigen CPU"
                            elif "cuda" in v: backend_desc_cfg = "CUDA GPU"
                            elif "tensorrt" in v or "trt" in v: backend_desc_cfg = "TensorRT GPU"
                            break
        except Exception:
            pass
        # 优先按 katago version 真实输出
        backend_desc = backend_desc_cfg
        try:
            from engine.version_info import probe_katago_version
            vi = probe_katago_version()
            b = (vi.get("backend") or "").lower()
            if b:
                if "eigen" in b: backend_desc = "eigen CPU"
                elif "tensorrt" in b or "trt" in b: backend_desc = "TensorRT GPU"
                elif "cuda" in b: backend_desc = "CUDA GPU"
        except Exception:
            pass
        return {
            "mode": "katago",
            "ready": True,
            "message": f"KataGo 引擎就绪（kata1-b28c512nbt / {backend_desc}）",
            "exe": os.path.basename(KATAGO_EXE),
            "model": os.path.basename(KATAGO_MODEL),
            "config": os.path.basename(KATAGO_CONFIG),
            "backend": backend_desc,
        }
    if FORCE_KATAGO:
        return {
            "mode": "mock",
            "ready": True,
            "message": "已强制启用 KataGo 但 exe/model 未就位（回退到 Mock）。补全依赖后即可切换。",
        }
    return {
        "mode": "mock",
        "ready": True,
        "message": (
            "Mock 模式（工具站先用，不阻塞）。"
            "装好 Eigen/CUDA 版 KataGo 二进制后，"
            "启动时设置 GO_COACH_FORCE_KATAGO=1 即可切到真实 KataGo。"
        ),
    }

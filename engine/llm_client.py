"""Ollama LLM 客户端 —— 只做"教学口吻润色"（不做围棋判断）。

所有判断数据来自 KataGo 规则模板，LLM 只负责：
  1. 把简洁的大纲文本改成"围棋老师讲课"的口吻
  2. 语气亲切，适合教学，不要太机械
  3. 绝不改写任何数值（胜率/目差/排名全部保留原样）

失败安全：
  - Ollama 不可达、超时、生成结果异常 —— 静默返回 None，调用方继续用规则模板结果。
  - 自带缓存：相同 hand_hash 不重复生成。
"""
import json
import logging
import time
import hashlib
from typing import Dict, List, Optional

import urllib.request
import urllib.error

import config

logger = logging.getLogger("go-coach.llm")

_SYSTEM_PROLOGUE = """你是围棋教学助手"教练小改"。你的唯一任务是把用户提供的"围棋解说大纲"改成亲切的中文老师口吻。
严格规则（违反任何一条就失败）：
1. 永远不改写任何数字、坐标、排名。用户给的 33.9% 胜率不能改成 33%，R16 不能改成 R17，"候选1/2/3"顺序不能变。
2. 你不懂围棋，所有判断都来自用户数据。不要新增任何评估结论。
3. 只在原大纲基础上润色语气、添加过渡句，让段落自然流畅。
4. 输出严格保持 JSON 结构，5 个字段 summary/position/candidate_strategies/comparison/strategy 一个都不能少。
5. candidate_strategies 必须是 5 个字符串的数组，和输入顺序一一对应。

只输出 JSON，不要其他文字，不要 markdown 代码块，不要解释。"""


def _post(url: str, payload: Dict, timeout: int) -> Optional[Dict]:
    """最小 urllib 请求（避免 requests 重复依赖检查）。"""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("Ollama 请求失败: %s", exc)
        return None


_OLLAMA_LAST_CHECK_TS = 0.0
_OLLAMA_LAST_RESULT = False
_OLLAMA_CHECK_TTL = 10.0  # 健康检查结果缓存 10 秒（避免每个 analyze 都探测）


def ollama_available() -> bool:
    """健康检查：Ollama API 是否可达（带 TTL 缓存，<=10s 内不会重复探测）。"""
    global _OLLAMA_LAST_CHECK_TS, _OLLAMA_LAST_RESULT
    if not config.LLM_ENABLED:
        return False
    now = time.time()
    if now - _OLLAMA_LAST_CHECK_TS < _OLLAMA_CHECK_TTL:
        return _OLLAMA_LAST_RESULT
    try:
        req = urllib.request.Request(config.OLLAMA_BASE_URL + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            ok = resp.status == 200
    except Exception:
        ok = False
    _OLLAMA_LAST_CHECK_TS = now
    _OLLAMA_LAST_RESULT = ok
    if not ok:
        logger.debug("Ollama 不可达: %s", config.OLLAMA_BASE_URL)
    return ok


def _build_prompt(outline: Dict) -> str:
    """把规则模板生成的解说大纲拼成 LLM 润色 prompt。"""
    cand_lines = "\n".join(
        f"  {i+1}. {s}" for i, s in enumerate(outline["candidate_strategies"])
    )
    return f"""以下是围棋解说大纲，请改成"围棋老师对学生讲解"的亲切自然口吻，保留所有数据：
- summary：{outline['summary']}
- position：{outline['position']}
- candidate_strategies（5条，顺序和内容必须保持，只改语气）：
{cand_lines}
- comparison：{outline['comparison']}
- strategy：{outline['strategy']}

输出严格 JSON：
{{"summary":"...","position":"...","candidate_strategies":["...","...","...","...","..."],"comparison":"...","strategy":"..."}}
"""


def _parse_llm_output(text: str, original: Dict) -> Optional[Dict]:
    """尽力解析 LLM 的输出，失败返回 None。"""
    if not text:
        return None
    # 1. 截取最外层的 {...}
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    body = text[start:end + 1]
    try:
        data = json.loads(body)
    except Exception:
        logger.warning("LLM 输出 JSON 解析失败")
        return None
    # 2. 检查结构完整性
    required = ["summary", "position", "candidate_strategies", "comparison", "strategy"]
    for k in required:
        if k not in data:
            logger.warning("LLM 输出缺少字段: %s", k)
            return None
    if not isinstance(data["candidate_strategies"], list) or \
            len(data["candidate_strategies"]) != 5:
        logger.warning("LLM 输出 candidate_strategies 长度异常")
        return None
    return {k: data[k] for k in required}


def polish_commentary(outline: Dict, moves_hash: str) -> Optional[Dict]:
    """请求 LLM 润色解说大纲，失败/禁用 返回 None。

    Args:
        outline: 规则模板生成的解说（5 个必需字段齐全）
        moves_hash: 本次局面的 hash 字符串，用于缓存
    """
    if not config.LLM_ENABLED:
        return None
    # 缓存命中
    if moves_hash in config.LLM_CACHE:
        return config.LLM_CACHE[moves_hash]
    # 健康检查
    if not ollama_available():
        return None

    prompt = _build_prompt(outline)
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "5m",  # 5 分钟不用自动卸载模型释放显存
        "options": {
            "temperature": config.OLLAMA_TEMPERATURE,
            "num_ctx": 8192,
        },
    }

    t0 = time.time()
    resp = _post(config.OLLAMA_BASE_URL + "/api/generate", payload, config.OLLAMA_TIMEOUT)
    t1 = time.time()
    if not resp or "response" not in resp:
        return None

    result = _parse_llm_output(resp["response"], outline)
    if result:
        # 补充 full_text
        ft_parts = [
            result["summary"],
            result["position"],
            "选点对比：" + result["comparison"],
            result["strategy"],
        ]
        result["full_text"] = "\n".join(ft_parts)
        # 保留原大纲的额外字段
        for k, v in outline.items():
            if k not in result:
                result[k] = v
        config.LLM_CACHE[moves_hash] = result
        logger.info("LLM 润色完成 %.1fs (缓存条目=%d)", t1 - t0, len(config.LLM_CACHE))
    else:
        logger.warning("LLM 润色输出无效，耗时 %.1fs", t1 - t0)
    return result


def make_moves_hash(moves: List[str], board_size: int, level: int, root_info: Dict) -> str:
    """生成局面指纹字符串（用于缓存，避免重复让 LLM 跑）。"""
    wr = round(root_info.get("winrate", 0), 3) if root_info else 0
    sc = round(root_info.get("scoreLead", 0), 2) if root_info else 0
    raw = f"{board_size}|{level}|{wr}|{sc}|" + ",".join(moves)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

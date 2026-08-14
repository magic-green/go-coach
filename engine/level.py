"""AI 等级参数：通过调整 KataGo 的 maxVisits 与 temperature 模拟不同棋力。

等级映射（近似）：
    1-4   ≈ 入门 10-5 级
    5-8   ≈ 5-1 级
    9-12  ≈ 1 级-业余初段
    13-20 ≈ 业余段位（搜索更充分）
"""
from typing import Dict

# 等级标签（供前端展示）
LEVEL_LABELS = {
    1: "10级", 2: "9级", 3: "8级", 4: "7级",
    5: "6级", 6: "5级", 7: "4级", 8: "3级",
    9: "2级", 10: "1级", 11: "业余1段", 12: "业余2段",
    13: "业余3段", 14: "业余4段", 15: "业余5段",
    16: "业余6段", 17: "业余7段", 18: "强业余", 19: "准职业", 20: "职业级",
}


def get_ai_level_params(level: int) -> Dict:
    """根据等级返回 KataGo 分析参数。

    Args:
        level: 1-20

    Returns:
        {'maxVisits': int, 'temperature': float}
    """
    level = max(1, min(20, int(level)))
    return {
        "maxVisits": 10 + level * 15,
        "temperature": round(max(0.0, 1.0 - level * 0.05), 2),
    }

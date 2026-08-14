"""模板解说生成器。

基于 KataGo 返回的 moveInfos 与 rootInfo，用规则模板生成自然语言解说。
输入是 engine.analyze() 的结构化结果。

关键点：
- KataGo 的 winrate/scoreLead 永远是"黑方视角"（winrate=黑胜率，scoreLead>0=黑领先）。
  因此轮到白棋下时，需要把结果翻译为"行棋方视角"，否则解说会完全反着。
"""
from typing import Dict, List

from engine.katago_engine import gtp_to_xy

# GTP 列字母（用于坐标中文化）
GTP_COLUMNS = "ABCDEFGHJKLMNOPQRST"


def coord_cn(move: str, board_size: int) -> str:
    """GTP 坐标转中文标记，如 'Q16' -> 'Q16'（保留字母+数字，便于对照棋盘）"""
    return move


def _winrate_pct(wr: float) -> float:
    """胜率 0-1 -> 百分比"""
    return round(wr * 100, 1)


def _to_move_view(root_winrate: float, root_score: float,
                  move_winrate: float, move_score: float,
                  to_move_black: bool):
    """把黑方视角的胜率/目差转换为"轮到行棋方视角"。

    返回 (side_winrate, side_score, side_sign, opp_sign)：
      - side_winrate：当前轮到的那一方的胜率（0-1）
      - side_score：当前轮到的那一方领先多少目
      - side_name / opp_name：行棋方 / 对方的中文名
    """
    if to_move_black:
        # 行棋方=黑，KataGo 视角一致
        side_wr = root_winrate
        side_sc = root_score
        move_wr = move_winrate
        move_sc = move_score
        side_name, opp_name = "黑", "白"
    else:
        # 行棋方=白，黑视角胜率要反过来
        side_wr = 1.0 - root_winrate
        side_sc = -root_score
        move_wr = 1.0 - move_winrate
        move_sc = -move_score
        side_name, opp_name = "白", "黑"
    return side_wr, side_sc, move_wr, move_sc, side_name, opp_name


def _describe_position(side_winrate: float, side_score: float,
                       side_name: str, opp_name: str) -> str:
    """用简洁的话判断局面走向，去掉重复的数值罗列（数值已在胜率条显示）。"""
    if side_score > 15:
        return f"{side_name}方大幅领先，{opp_name}方需要主动打入或搅乱，否则很难翻盘。"
    if side_score > 6:
        return f"{side_name}方稍占优势，{opp_name}方尚有逆转空间。"
    if side_score > -6:
        return "局面非常接近，下一步大场或攻防要点会直接影响胜负。"
    if side_score > -15:
        return f"{opp_name}方稍占优势，{side_name}方需要通过攻击或打入把局面追回来。"
    return f"{opp_name}方大幅领先，{side_name}方必须找最大大场加拼命，否则基本定局。"


def _describe_move(move_info: Dict, rank: int, board_size: int,
                   to_move_black: bool) -> str:
    """候选点简短描述（去掉目数重复，数值在列表条里已有）。"""
    move = move_info.get("move", "")
    wr = move_info.get("winrate", 0.5)
    visits = move_info.get("visits", 0)
    if to_move_black:
        side_wr = wr
    else:
        side_wr = 1.0 - wr
    wr_pct = _winrate_pct(side_wr)
    role = {0: "首选", 1: "次选", 2: "第三选"}.get(rank, f"第{rank+1}选")
    return f"{role} {move}：胜率 {wr_pct}%，搜索 {visits} 次"


def _compare_top_two(infos: List[Dict], to_move_black: bool) -> str:
    """前两选对比只给定性结论，不再重复说目数。"""
    if len(infos) < 2:
        return ""
    def side_sc(m):
        s = m.get("scoreLead", 0.0)
        return s if to_move_black else -s
    s0 = side_sc(infos[0])
    s1 = side_sc(infos[1])
    diff = s0 - s1
    m0 = infos[0].get("move", "")
    m1 = infos[1].get("move", "")
    if diff >= 3.0:
        return f"{m0} 明显好于 {m1}，属于「唯一大场」，强烈建议下 {m0}。"
    if diff >= 1.0:
        return f"{m0} 相对 {m1} 更实惠一些，{m1} 一般是取势的变化。"
    if diff <= -3.0:
        return f"{m0} 是取厚势的下法，{m1} 是捞实利的变化，按局面风格取舍。"
    return f"{m0} 与 {m1} 等价，根据风格或局部厚薄选择即可。"


def _strategy_advice(side_winrate: float, side_score: float, move_count: int,
                     side_name: str, opp_name: str, board_size: int) -> str:
    """基于行棋方视角给出策略建议（不再说"己方领先但其实是对方领先"的鬼话）。"""
    # 阶段划分（以 19 路为基准，小棋盘按比例压缩）
    s = max(9, board_size)
    scale = s / 19.0
    opening = max(10, int(15 * scale))
    late = max(60, int(150 * scale))

    if move_count < opening:
        return ("布局阶段：按占角 → 挂角/守角 → 拆边的基本顺序行棋，"
                "不要过早进入接触战，先把大角/大场走完。")
    if side_score > 10:
        return (f"当前{side_name}方明显领先（{side_score:+0.1f}目）："
                f"保持简明，尽量走定形、收官，"
                f"避开没把握的乱战，把优势稳稳转化成胜势。")
    if side_score < -10:
        return (f"当前{side_name}方落后较多（{side_score:+0.1f}目）："
                f"必须下得积极——优先打入{opp_name}方的实空、"
                f"攻击对方薄棋、制造复杂战斗，把局面拖入不明朗。")
    if move_count > late:
        return ("已进入官子阶段：目数优先，注意先手官子与大官子顺序，"
                "不要随手给对方送目。")
    # 中盘
    if -4 <= side_score <= 4 and side_winrate >= 0.55:
        return (f"中盘细棋、{side_name}方稍微看好："
                f"下一手既要抢实地，也要注意自己孤棋的厚薄，"
                f"避免被{opp_name}方先手打穿一块。")
    if -4 <= side_score <= 4:
        return (f"中盘细棋、形势五五开："
                f"重点看厚薄与攻防要点，不要为了 1-2 目小利去走重孤棋，"
                f"先把自己走畅再考虑打入。")
    if side_score > 0:
        return (f"中盘{side_name}方略好：继续扩大优势，不要随手走缓；"
                f"但也不要为了吃棋去冒不必要的险，稳中有攻。")
    return (f"中盘{side_name}方稍差：寻找{opp_name}方的弱点，"
            f"要么打入破空，要么通过攻击反超；"
            f"避免平稳收官。")


# ---------- 角部坐标语义识别 ----------
# 星位、小目、三三、目外、高目的相对坐标模式（角从 (0,size-1) 开始）
# 一个标准 19 路棋盘的四个角：
#   右上角 (col, row) = (15, 3) = R16；左上角 (3, 3) = D4；等等（row_idx 是倒序的）
# 我们用"距角的偏移"识别：相对该角的 corner_col, corner_row 偏移。

# 某点 (c, r) 归属哪个角（距离哪个角近）— 返回角名和相对偏移 (dx, dy)
# 四个角：TR=右上, TL=左上, BR=右下, BL=左下
_CORNERS_19 = [
    ("右上", 15, 3),   # R16 星位
    ("左上", 3, 3),    # D4 星位
    ("右下", 15, 15),  # R4 星位
    ("左下", 3, 15),   # D16 星位
]

def _nearest_corner_info(col_idx: int, row_idx: int, board_size: int):
    """返回 (角名, 相对星位偏移 dx, dy, corner_col, corner_row)。"""
    # 动态生成 corners：星位 = 距边 3 路，19路 = col 3/15 row 3/15；9路 = 2/6
    star = 3 if board_size >= 13 else 2
    far = board_size - 1 - star
    corners = [
        ("右上", far, star),
        ("左上", star, star),
        ("右下", far, far),
        ("左下", star, far),
    ]
    best = None
    best_dist = 999
    for name, cc, cr in corners:
        dist = abs(col_idx - cc) + abs(row_idx - cr)
        if dist < best_dist:
            best_dist = dist
            best = (name, col_idx - cc, row_idx - cr, cc, cr)
    return best


def _point_semantic(move: str, board_size: int, history_moves: List[str]) -> Dict:
    """返回该落点的丰富语义：
    {
      region: 角部/边上/中腹,
      corner: 右上/左上/右下/左下 (属于哪个角区域，角部才有)，
      point_type: 星位/小目/三三/目外/高目/空角/挂角/守角/拆边/夹攻/单关跳/尖/飞/其他，
      corner_mood: 空角/己方角/对方角（该角已有的棋子颜色判断：根据历史最后一手无法判断颜色，用 move_count%2 推断该角第一个子的颜色），
      dx, dy: 相对星位的偏移，
    }
    """
    result = {"region": "其他", "point_type": "其他", "corner": None,
              "dx": 0, "dy": 0, "corner_mood": "空角"}
    if not move or move == "pass":
        return result
    col = move[0].upper()
    row_str = move[1:]
    try:
        row = int(row_str)
    except ValueError:
        return result
    cols = "ABCDEFGHJKLMNOPQRST"
    cidx = cols.index(col)
    ridx = board_size - row  # row_idx：row 1 对应 ridx = size-1
    result["col_idx"] = cidx
    result["row_idx"] = ridx

    # 距边距离
    edge_dist = min(cidx, ridx, (board_size - 1 - cidx), (board_size - 1 - ridx))
    star = 3 if board_size >= 13 else 2
    far = board_size - 1 - star
    # 角部区域：cidx 和 ridx 都在「角宽范围」内（一路到星位 + 多一路）
    in_col_corner = (cidx <= star + 1) or (cidx >= far - 1)
    in_row_corner = (ridx <= star + 1) or (ridx >= far - 1)
    if in_col_corner and in_row_corner and edge_dist <= star + 2:
        result["region"] = "角部"
    elif edge_dist <= 4:
        result["region"] = "边上"
    else:
        result["region"] = "中腹"

    # 角部和边上区域：做详细类型识别
    if result["region"] in ("角部", "边上"):
        cname, dx, dy, cc, cr = _nearest_corner_info(cidx, ridx, board_size)
        result["corner"] = cname
        result["dx"] = dx
        result["dy"] = dy

        # ---- 角部坐标类型识别（相对星位）----
        # 星位 (0,0)；小目 (dx=-1, dy=1) 或 (dx=1, dy=-1) 的对角；
        # 三三 (-2, 2) 或 (2, -2)；目外 (-1, -1)/(1, 1) 外一路；高目 (-2,0)/(0,2) 等等。
        # 这里用 (dx, dy) 的"曼哈顿距离星位" + 模式匹配。
        if dx == 0 and dy == 0:
            result["point_type"] = "星位"
        # 小目：从星位向对角走（一路向边+一路向"中间方向"）
        elif abs(dx) == 1 and abs(dy) == 1:
            # 小目的 dx/dy 符号需按角的"方向"：右上星位(15,3) -> 小目是 Q17 (dx=-1, dy=-1) 或 Q15 (dx=-1, dy=1)？
            # 简化：只要距星位 1+1 的角部区域，就叫小目
            if result["region"] == "角部":
                result["point_type"] = "小目"
            else:
                result["point_type"] = "小飞"
        # 三三：距星位 2+2（对角线双2），即 corner 的 (star±2, star±2)，距边=1
        # 相对星位：右上 dx=-2 dy=+2（向左2 向边2）→ 距右边=2，距上边=1，edge_dist=1
        elif abs(dx) == 2 and abs(dy) == 2:
            result["point_type"] = "三三"
        # 目外：小目再向边一路（dx=-2 dy=1 之类，取决于角方向）
        elif abs(dx) + abs(dy) == 3 and (abs(dx) == 2 or abs(dy) == 2) and result["region"] == "角部":
            result["point_type"] = "目外"
        # 高目：向中腹高一路
        elif (abs(dx) == 2 and dy == 0) or (dx == 0 and abs(dy) == 2):
            result["point_type"] = "高目" if result["region"] == "角部" else "拆二"
        # 单关跳 (0,1) 或 (1,0) 距星位一格
        elif (abs(dx) == 1 and dy == 0) or (dx == 0 and abs(dy) == 1):
            if result["region"] == "角部":
                result["point_type"] = "单关守角" if history_moves else "单关跳"
            else:
                result["point_type"] = "拆边"
        elif abs(dx) + abs(dy) >= 6 and result["region"] == "边上":
            result["point_type"] = "大场拆边"
        else:
            result["point_type"] = "角部周边" if result["region"] == "角部" else "边上要点"

        # ---- 与已有棋子的关系（守角/挂角/夹攻）----
        # 汇总该角区域已有的棋子
        corner_owned = []  # (gtp, color)
        star = 3 if board_size >= 13 else 2
        far = board_size - 1 - star
        # 判断历史中哪些子落在该角 4x4 区域
        cr_left = star - 2 if cname in ("左上", "左下") else far - 2
        cr_right = star + 2 if cname in ("左上", "左下") else far + 2
        rr_top = star - 2 if cname in ("右上", "左上") else far - 2
        rr_bot = star + 2 if cname in ("右上", "左上") else far + 2
        for hi, hmove in enumerate(history_moves):
            if not hmove or hmove == "pass":
                continue
            # 排除候选点自身（候选点本身可能还没真正落子）
            # 避免 R16 这个候选点把历史里的 R16（黑棋，第0手）当"对方角"的参考
            if hmove == move:
                continue
            hc = cols.index(hmove[0].upper())
            hr = board_size - int(hmove[1:])
            if (cr_left <= hc <= cr_right) and (rr_top <= hr <= rr_bot):
                corner_owned.append((hc, hr, 1 if (hi % 2 == 0) else 2))
        # 候选点本身颜色（当前行棋方）
        me = 1 if (len(history_moves) % 2 == 0) else 2
        opp = 2 if me == 1 else 1
        # 判断该角已有的颜色和数量
        my_corner_stones = [x for x in corner_owned if x[2] == me]
        opp_corner_stones = [x for x in corner_owned if x[2] == opp]
        if my_corner_stones and not opp_corner_stones:
            result["corner_mood"] = "己方角"
            if result["point_type"] not in ("星位", "小目", "三三", "目外", "高目"):
                result["point_type"] = "守角"
        elif opp_corner_stones and not my_corner_stones:
            result["corner_mood"] = "对方角"
            if edge_dist < 5:
                result["point_type"] = "挂角"
        elif opp_corner_stones and my_corner_stones:
            result["corner_mood"] = "双方接触"
            if edge_dist < 5:
                result["point_type"] = "夹攻" if opp_corner_stones[0][0] == cidx and opp_corner_stones[0][1] == ridx else "定式应对"

    return result


def _phase_name(move_count: int, board_size: int) -> str:
    s = max(9, board_size)
    scale = s / 19.0
    if move_count < max(10, int(18 * scale)):
        return "布局"
    if move_count < max(60, int(120 * scale)):
        return "中盘"
    return "官子"


def _candidate_strategy(move_info: Dict, rank: int, board_size: int,
                        move_count: int, to_move_black: bool,
                        all_history: List[str]) -> str:
    """为单个候选点生成简短策略词（≤10字，1目了然）。"""
    move = move_info.get("move", "")
    sem = _point_semantic(move, board_size, all_history)
    pt = sem.get("point_type", "其他")
    region = sem.get("region", "")
    corner = sem.get("corner", "")

    # 布局短标签
    if pt in ("星位", "小目", "三三", "目外", "高目"):
        return f"{corner or ''}{pt}".strip() or pt
    if pt == "挂角":
        return f"{corner}挂角".strip()
    if pt in ("守角", "单关守角"):
        return f"{corner}守角".strip()
    if pt == "夹攻":
        return f"{corner}夹攻".strip()
    if pt == "定式应对":
        return "定式正解"
    if pt in ("拆边", "大场拆边"):
        return f"{region}拆边".strip() or "拆边大场"

    # 通用区域
    if region == "中腹":
        return "中腹要点"
    if region == "边上" and pt != "其他":
        return f"{region}{pt}".strip()

    # 兜底（按排名）
    if rank == 0:
        return "首选正解"
    if rank == 1:
        return "风格备选"
    return "参考变化"


def _describe_position_type(move: str, board_size: int) -> str:
    """兼容旧接口：判断落点位置类型：角部/边上/中腹。"""
    if not move or move == "pass":
        return "其他"
    col = move[0].upper()
    row_str = move[1:]
    try:
        row = int(row_str)
    except ValueError:
        return "其他"
    col_idx = "ABCDEFGHJKLMNOPQRST".index(col)
    edge_dist = min(col_idx, row - 1, board_size - 1 - col_idx, board_size - 1 - row)
    if edge_dist <= 2:
        return "角部"
    if edge_dist <= 4:
        return "边上"
    return "中腹"


def evaluate_move_quality(prev_winrate: float, curr_winrate: float,
                          mover_black: bool) -> Dict:
    """根据前后两手的 KataGo 黑方胜率变化评价手质量。

    Args:
        prev_winrate: 落子前的黑方胜率（0-1）
        curr_winrate: 落子后的黑方胜率（0-1）
        mover_black: 落子方是否黑棋

    Returns:
        {label, color, delta_pct, description}
    """
    # 从落子方视角计算胜率变化
    if mover_black:
        delta = curr_winrate - prev_winrate  # 黑下，黑胜率应升
    else:
        delta = prev_winrate - curr_winrate  # 白下，黑胜率应降

    delta_pct = round(delta * 100, 1)

    if delta_pct >= -0.5:
        return {
            "label": "妙手",
            "color": "#00CC00",
            "delta_pct": delta_pct,
            "description": f"胜率变化 {delta_pct:+.1f}%，与 AI 最优解几乎一致！",
        }
    if delta_pct >= -3.0:
        return {
            "label": "本手",
            "color": None,
            "delta_pct": delta_pct,
            "description": f"胜率变化 {delta_pct:+.1f}%，稳健正着，接近 AI 推荐。",
        }
    if delta_pct >= -8.0:
        return {
            "label": "俗手",
            "color": "#FFCC00",
            "delta_pct": delta_pct,
            "description": f"胜率变化 {delta_pct:+.1f}%，略有损失，AI 有更优选点。",
        }
    return {
        "label": "恶手",
        "color": "#FF4444",
        "delta_pct": delta_pct,
        "description": f"胜率变化 {delta_pct:+.1f}%，损失较大，建议考虑其他选点。",
    }


def generate_commentary(analysis: Dict, move_count: int, board_size: int,
                        history_moves: List[str] = None) -> Dict:
    """生成精简解说（去掉重复的数值，保留判断和教学建议）。

    Args:
        analysis: engine.analyze() 的返回值（winrate/scoreLead 均为黑视角）
        move_count: 已落子数（用于判断阶段，也用于推算轮到谁下）
        board_size: 棋盘大小
        history_moves: 历史棋谱列表（GTP 坐标，如 ["R16","D4",...]），用于坐标语义识别
    """
    history_moves = history_moves or []
    root = analysis.get("rootInfo", {})
    infos = analysis.get("moveInfos", [])
    owner_data = analysis.get("ownerData")
    root_wr = root.get("winrate", 0.5)
    root_sc = root.get("scoreLead", 0.0)
    to_move_black = (move_count % 2 == 0)
    to_move_name = "黑" if to_move_black else "白"

    # 转成"行棋方视角"
    side_wr, side_sc, _, _, side_name, opp_name = _to_move_view(
        root_wr, root_sc, root_wr, root_sc, to_move_black
    )

    # 前 5 个候选点的策略思路（一句话，基于坐标语义 + 历史）
    top = infos[:5]
    cand_strategies = [
        _candidate_strategy(m, i, board_size, move_count, to_move_black, history_moves)
        for i, m in enumerate(top)
    ]

    position = _describe_position(side_wr, side_sc, side_name, opp_name)
    comparison = _compare_top_two(infos, to_move_black)
    strategy = _strategy_advice(side_wr, side_sc, move_count, side_name, opp_name, board_size)

    # 五分区势力描述 + policy 直觉对比
    region_info = analyze_ownership_regions(owner_data, board_size)
    policy_note = _policy_vs_visits_note(top, to_move_black)

    if top:
        best_move = top[0].get("move", "")
        best_wr_b = top[0].get("winrate", 0.5)
        if to_move_black:
            bwr = best_wr_b
        else:
            bwr = 1.0 - best_wr_b
        summary = (
            f"轮到{to_move_name}下，AI 首选 {best_move}"
            f"（{to_move_name}视角胜率 {_winrate_pct(bwr)}%）。{position}"
        )
    else:
        summary = f"暂无候选点。轮到{to_move_name}下。"

    return {
        "summary": summary,
        "position": position,
        "candidates": [],  # 不再单独列数值（已在候选点列表卡片上显示）
        "candidate_strategies": cand_strategies,
        "comparison": comparison,
        "strategy": strategy,
        "regions": region_info,          # 新增：五分区势力（含averages/dominant/description）
        "policy_note": policy_note,      # 新增：策略网络 vs 搜索排名 对比
        "to_move": to_move_name,
        "root_winrate": root_wr,
        "to_move_view": {
            "winrate_pct": _winrate_pct(side_wr),
            "score_lead": round(side_sc, 2),
            "side_name": side_name,
            "opp_name": opp_name,
        },
    }


# ---------- 五分区势力统计（基于 ownership）----------
# 区域划分：左上 / 右上 / 左下 / 右下 / 中央
# 边界：以棋盘中线为界，中央区域取中间 (size//3) x (size//3) 的范围

def _split_regions(board_size: int):
    """返回五个区域各自的 (x_start, x_end, y_start, y_end)，含两端。"""
    mid = board_size // 2          # 中线（向下取整，便于奇数棋盘对称）
    # 中央区边长：大约 board_size//3，避免与四角重叠太多
    c_len = max(3, board_size // 3)
    c_start = (board_size - c_len) // 2
    c_end = c_start + c_len - 1

    # 四个角：以中线为界，把棋盘分成四象限
    # 左上：x ∈ [0, mid-1], y ∈ [0, mid-1]
    # 右上：x ∈ [mid, N-1], y ∈ [0, mid-1]
    # 左下：x ∈ [0, mid-1], y ∈ [mid, N-1]
    # 右下：x ∈ [mid, N-1], y ∈ [mid, N-1]
    # 中央：扣除角区的中间块
    regions = {
        "左上": (0, mid - 1, 0, mid - 1),
        "右上": (mid, board_size - 1, 0, mid - 1),
        "左下": (0, mid - 1, mid, board_size - 1),
        "右下": (mid, board_size - 1, mid, board_size - 1),
        "中央": (c_start, c_end, c_start, c_end),
    }
    return regions


def analyze_ownership_regions(ownership_grid, board_size: int) -> Dict:
    """分析五个区域的势力分布，返回结构化数据 + 自然语言一句话描述。

    Args:
        ownership_grid: KataGo ownership 数组（row-major，长 N*N，正值=黑控制 / 负值=白控制）
        board_size: 棋盘大小
    Returns:
        {
          "averages": {"左上": -0.52, "右上": 0.31, ...},   # 每个区的平均势力
          "dominant": {"左上": "白", "右上": "黑", ...},     # 每区主导方（阈值 > 0.15 才算）
          "description": "白棋在左上有厚势，黑棋在右边围成模样，中央大致两分。"
        }
    """
    result = {
        "averages": {},
        "dominant": {},
        "description": "",
    }
    if not ownership_grid or not isinstance(ownership_grid, list):
        result["description"] = "暂无势力分布数据。"
        return result
    if len(ownership_grid) != board_size * board_size:
        result["description"] = "势力数据长度与棋盘不匹配。"
        return result

    regions = _split_regions(board_size)
    THRESHOLD = 0.15   # |avg| > 此值才算某方明显掌控，否则为"两分"

    # 计算每个区的平均值（不含 0 位置，排除已落子点可能的归零影响？此处保留原值即可）
    for rname, (xs, xe, ys, ye) in regions.items():
        vals = []
        for y in range(ys, ye + 1):
            for x in range(xs, xe + 1):
                idx = y * board_size + x
                if 0 <= idx < len(ownership_grid):
                    vals.append(float(ownership_grid[idx]))
        if not vals:
            avg = 0.0
        else:
            avg = sum(vals) / len(vals)
        result["averages"][rname] = round(avg, 3)
        if avg > THRESHOLD:
            result["dominant"][rname] = "黑"
        elif avg < -THRESHOLD:
            result["dominant"][rname] = "白"
        else:
            result["dominant"][rname] = "中立"

    # 生成自然语言描述（教学口吻，分三档）
    # 1) 明显掌控的区域（按「谁在哪」汇总）
    black_regions = [r for r, d in result["dominant"].items() if d == "黑"]
    white_regions = [r for r, d in result["dominant"].items() if d == "白"]
    neutral_regions = [r for r, d in result["dominant"].items() if d == "中立"]

    parts = []
    if black_regions:
        parts.append(f"黑棋在{'、'.join(black_regions)}有明显势力")
    if white_regions:
        parts.append(f"白棋在{'、'.join(white_regions)}占据优势")
    if neutral_regions:
        # 不全部报，只在有"中央中立"时提示
        if "中央" in neutral_regions and len(neutral_regions) <= 3:
            parts.append("中央地带双方未定")
        elif len(neutral_regions) >= 4:
            parts.append("全局大部分区域仍属细棋")

    if not parts:
        result["description"] = "目前棋盘空旷，势力格局尚未形成。"
    else:
        result["description"] = "，".join(parts) + "。"

    # 补充极端情况（某方大片掌控）的定性提醒
    if len(black_regions) >= 4 and len(white_regions) <= 1:
        result["description"] += " 整体黑方模样较大，白方需要及时打入。"
    elif len(white_regions) >= 4 and len(black_regions) <= 1:
        result["description"] += " 整体白方实空较多，黑方需通过攻击追回。"
    elif abs(len(black_regions) - len(white_regions)) >= 2:
        ahead = "黑" if len(black_regions) > len(white_regions) else "白"
        result["description"] += f" {ahead}方开局稍占布局便宜。"

    return result


def _policy_vs_visits_note(top_infos: List[Dict], to_move_black: bool) -> str:
    """对比策略网络先验（policyPrior）与实际搜索次数（visits）的排名差异。

    如果 policy 首选项 与 search 首选一致 → 说明 AI 直觉和搜索一致；
    如果存在「低先验但高 visits 的选点」→ 说明搜索过程中发现了意外的好手（盲点挖掘）。
    只在出现有趣差异时返回一句话，否则返回空串。
    """
    if not top_infos:
        return ""
    # 只看前 5
    top = top_infos[:5]
    # 过滤掉 policyPrior 缺失的
    ok = [m for m in top if m.get("policyPrior") is not None]
    if len(ok) < 2:
        return ""

    # 按 policyPrior 排（先验排名）
    by_policy = sorted(ok, key=lambda m: m["policyPrior"], reverse=True)
    policy_first = by_policy[0]

    # 按 visits 排（搜索排名，即 order=0 已排好）
    by_visits = sorted(ok, key=lambda m: m.get("visits", 0), reverse=True)
    visits_first = by_visits[0]

    policy_move = policy_first.get("move", "")
    visits_move = visits_first.get("move", "")

    # 一致：说明 AI 直觉可靠
    if policy_move == visits_move:
        return (f"策略网络先验首选 {policy_move}，"
                f"与搜索结果一致，属于 AI 「一眼就觉得好」的选点。")

    # 不一致：搜索推翻了直觉，这种情况值得提醒
    # 找到 visits 首选项在 policy 排名中的位置
    vf_policy_rank = next((i + 1 for i, m in enumerate(by_policy)
                           if m.get("move") == visits_move), len(by_policy))
    # 找到 policy 首选项在 visits 排名中的位置
    pf_visits_rank = next((i + 1 for i, m in enumerate(by_visits)
                           if m.get("move") == policy_move), len(by_visits))

    return (f"搜索推翻了策略网络直觉：先验首选 {policy_move}（实际排第{pf_visits_rank}），"
            f"但实际搜索更青睐 {visits_move}（先验仅排第{vf_policy_rank}），"
            f"属于「搜索挖到的盲点」，值得细看。")

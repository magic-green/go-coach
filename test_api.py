import requests, json, random

def test_analyze(name, moves, size=19):
    next_side = "B" if len(moves) % 2 == 0 else "W"
    print("\n=== TEST:", name, "=== moves=", len(moves), "next=", next_side)
    payload = {
        'moves': moves, 'boardSize': size, 'komi': 7.5, 'rules': 'chinese', 'level': 5
    }
    r = requests.post('http://127.0.0.1:5001/api/analyze', json=payload)
    d = r.json()
    if not d.get('ok'):
        print('ERROR:', d.get('error'))
        return
    a = d['analysis']
    c = d['commentary']
    ri = a['rootInfo']
    print(f"root (BLACK view): wr={ri['winrate']*100:.1f}% score={ri['scoreLead']:+.2f}")
    tv = c['to_move_view']
    print(f"to_move_view: side={tv['side_name']} wr={tv['winrate_pct']}% score={tv['score_lead']:+.2f}")
    print("--- summary ---")
    print(" ", c['summary'])
    print("--- position ---")
    print(" ", c['position'])
    print("--- strategy ---")
    print(" ", c['strategy'])
    if c.get('comparison'):
        print("--- comparison ---")
        print(" ", c['comparison'])
    print("--- moveInfos (black view) ---")
    for m in a['moveInfos']:
        print(f"  {m['order']+1}. {m['move']} wr={m['winrate']*100:.1f}% score={m['scoreLead']:+.2f} visits={m['visits']}")

def test_ai_move(name, moves, size=19):
    next_color = 1 if len(moves) % 2 == 0 else 2
    print("\n=== AI_MOVE:", name, "=== moves=", len(moves), "next_color=", next_color)
    payload = {
        'moves': moves, 'boardSize': size, 'komi': 7.5, 'rules': 'chinese', 'level': 5
    }
    r = requests.post('http://127.0.0.1:5001/api/ai-move', json=payload)
    d = r.json()
    if not d.get('ok'):
        print('ERROR:', d.get('error'))
        return
    mv = d['move']
    print(f"  AI picks: x={mv['x']} y={mv['y']} color={mv['color']} wr={mv['winrate']*100:.1f}% score={mv['scoreLead']:+.2f}")
    return mv

# 1. 空盘 黑先
test_analyze('空盘 黑先', [])
test_ai_move('空盘 黑先 AI', [])

# 2. 黑 R16 后，轮到白 (R: x=14, y=3)
test_analyze('黑R16后 白下', [{'x': 14, 'y': 3, 'color': 1}])
test_ai_move('黑R16后 白AI', [{'x': 14, 'y': 3, 'color': 1}])

# 3. 黑R16 白D4 后黑下 (D4: x=3, y=15)
test_analyze('黑R16白D4 黑下', [{'x':14,'y':3,'color':1}, {'x':3,'y':15,'color':2}])

# 4. 中盘 10 手
random.seed(42)
mids = []
occ = set()
for i in range(10):
    while True:
        x = random.randint(3, 15)
        y = random.randint(3, 15)
        if (x, y) not in occ:
            occ.add((x, y))
            mids.append({'x': x, 'y': y, 'color': 1 if i % 2 == 0 else 2})
            break
test_analyze('中盘10手 黑下', mids)

# 5. 中盘 11 手 轮到白
mids11 = mids + [{'x': 9, 'y': 9, 'color': 1}]  # 再加一手黑
test_analyze('中盘11手 白下', mids11)

print("\n=== ALL TESTS DONE ===")

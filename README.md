# 围棋 AI 智能教练

Web 版围棋对弈 + 局势分析 + 教学解说 + 棋谱存档一体化平台。支持 9/13/19 路棋盘，KataGo 引擎驱动，本地显存空闲自动释放，可选本地 Ollama LLM 做教学口吻润色，支持子路径反向代理部署。

---

## 功能总览（三层级）

本项目按三个递进层级规划，当前**一级、二级层级已全部实现，三级层级待开发**。

### 一级 · 基础对弈  ✅ 全部实现
- 网页棋盘：9 / 13 / 19 路自适应 Canvas 渲染，木质感背景 + 星位 + 坐标
- 落子规则：黑先、提子、自杀判定、打劫、悔棋、声音合成反馈（落子/提子/非法/妙手/恶手）
- 人机 / 双人模式：`HVA` 人执黑、`AVH` AI 执黑、`HVH` 人对人、`Research` 研究模式
- AI 等级 1~20：对应入门 10 级至业余初段，通过 KataGo `maxVisits/temperature` 模拟棋力
- **双方计时器**：黑方/白方实时用时（分:秒），落子后自动切换方，当前方红色高亮
- **完整棋谱记录面板**：侧栏 1..N 行棋入口，显示手数+黑白子点+坐标，点击任一手自动进入复盘并跳转
- **逐步复盘导航**：首手 / 上一手 / 下一手 / 终局 + 复盘位置显示 `当前 / 总手数`
- **终局数子裁判**：中国规则数子，基于 KataGo ownership 数据估算黑白控制点数，含贴目 7.5 目，弹窗显示「黑胜 +X / 白胜 +X」
- **棋谱存档 & 历史棋局列表**：SQLite 自动入库（比分、用时、棋谱），弹窗列出最近 50 局，点击一键加载回放 + 自动进入复盘模式，可删除

### 二级 · 局势分析 & AI 教学  ✅ 全部实现
- **5 步优选候选点**：可切换显示/隐藏（顶栏「五点优选」按钮，绿=开 / 蓝=关），棋盘上绿色圆圈 + A~E 字母标记（纯黑粗字），侧栏列表显示坐标 + 策略思路（≤10字短标签：「右上星位」「挂角」「守角」「中腹要点」…）
- **局势分析切换按钮**：点击切到分析模式（按钮高亮），侧栏展开候选点列表 + 解说面板 + 势力图；再点返回下棋模式（下棋模式最大化棋盘，侧栏隐藏）
- **势力范围可视化**：棋盘上黑/白正方形热力点（黑势力=黑色方块，白势力=白色方块），势力越强方块越大，最大方块明显小于棋子圆形
- **10 步深度推演**：点击候选点列表，自动展开 10 步 PV（黑白数字标记 + 细体描边）。1 号候选点自动做后台预推演缓存（0s 出结果）
- **10 步推演胜率趋势折线**：推演展开后异步沿 PV 逐步重分析，侧栏绘制黑方胜率折线（含起点、50% 均势参考线、黑白占优配色），显示胜率变化 Δ
- **实时胜率条** + **全局胜率波动曲线**：棋盘底部黑/白比例条，每次分析后更新；曲线区按每一手绘制黑方胜率折线（分段绿/红着色 + 起点黄色圆点 + 最新手彩色圆点 + 50% 均势参考虚线）
- **预计目数 & 形势范围**：数子弹窗展示双方最终预计目数、贴目、目差、胜负判定；每步 KataGo `scoreLead` 贯穿 AI 等级评估
- **AI 讲解贴合围棋理论**：坐标语义识别 100% 准确（星位/小目/三三/目外/高目/守角/挂角/夹攻/拆边），混合方案：
  - 规则模板 0ms 生成大纲
  - 异步调用 Ollama `qwen2.5:7b-32k` 润色成老师口吻，相同棋谱 hash 进程内缓存 0ms

### 三级 · AI 人格氛围对话  ⏳ 规划中
- 暂定一个 AI 人格（模仿柯洁语气）
- 棋局出现明显**妙手**（绿边框）或**恶手**（红边框）或**胜率突变 ±10%** 时，棋盘右下角弹出气泡对话框
- 预置 20~30 条短评论语（夸赞/嘲讽/调侃/感慨），按形势触发时随机抽一条
- 暂无需 LLM 实时生成，纯语料库 + 条件触发即可实现

---

## 快速开始

### 1. 安装依赖

```bash
cd go-coach
pip install -r requirements.txt
```

### 2. 放入 KataGo 引擎（可选）

将以下文件放入 `katago/` 目录：

| 文件 | 说明 |
|------|------|
| `katago.exe` | KataGo analysis 模式可执行文件（推荐 CUDA 版，速度是 CPU 3~5 倍）|
| `kata1-b28c512nbt.bin.gz` | 模型权重（日常教学/工具站最合适，速度快 + Elo 14110 足够）|
| `analysis_override.cfg` | 配置模板（内部 include `default_gtp.cfg`），可设置 `backend=cuda` / `backend=eigen` |

未放入引擎时，系统自动启用 **Mock 模式**，返回模拟数据，可用于纯 UI/解说/存档流程调试。

### 3. 启动服务

```bash
# 基础启动（自动检测 KataGo 引擎；未检测到则降级 Mock 模式）
python app.py

# 强制 Mock（无需引擎，纯 UI/解说/存档调试）
$env:GO_COACH_FORCE_MOCK=1; python app.py

# 禁用 LLM 润色（无 Ollama 环境时）
$env:GO_COACH_LLM=0; python app.py
```

浏览器访问 http://127.0.0.1:5001/

---

## 硬件适配（推荐配置）

| 项目 | RTX 3060 Laptop 6GB（本机）|
|------|-------------------------|
| CPU | AMD Ryzen 9 5900HX / 同档 |
| GPU | RTX 3060 6GB |
| 内存 | 32 GB |
| KataGo 等级 maxVisits | 等级 1..20：10..310，深度推演固定 500 |
| 单步分析耗时 | 10~310 visits：0.3~2 秒；500 visits：约 5 秒 |
| 并发对局 | 建议 2–3 局（避免显存溢出）|
| KataGo 驱动要求 | 驱动最高支持 CUDA 13.1，**不要装 CUDA 13.2 的 KataGo** |

> KataGo 引擎懒启动策略：**首次操作才拉起进程，5 分钟无调用自动终止**，不浪费显存。Ollama 通过 `keep_alive=5m` 同样 5 分钟卸载模型。

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GO_COACH_FORCE_MOCK` | 空 | =1 时强制 Mock 模式（绕过引擎检测）；空/其他值自动检测文件就位后启用真实 KataGo |
| `GO_COACH_LLM` | 空 | =0 禁用 LLM 润色，强制规则解说；空/其他值自动探测 Ollama 可达性 |
| `GO_COACH_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama 服务地址 |
| `GO_COACH_OLLAMA_MODEL` | `qwen2.5:7b-32k` | LLM 模型名称 |

---

## 项目结构

```
go-coach/
├── app.py                  # Flask 主入口（注册 Blueprint / 初始化 DB）
├── config.py               # 全局配置（路径、KataGo、Ollama、LLM 缓存）
├── routes.py               # API 路由（9 个接口）
├── commentary.py           # 规则解说模板（0ms 生成，坐标语义识别引擎）
├── game_db.py              # SQLite 棋谱存档 CRUD
├── core_logger.py          # 统一日志（控制台 + 文件）、阶段计时、request_id
├── requirements.txt
├── katago/                 # KataGo 引擎目录
│   ├── katago.exe
│   ├── kata1-b28c512nbt.bin.gz
│   ├── default_gtp.cfg
│   └── analysis_override.cfg
├── engine/
│   ├── katago_engine.py    # KataGo 引擎封装（懒启动 + 空闲休眠 + Mock 降级 + 数子估算）
│   ├── level.py            # AI 等级参数（1~20 maxVisits/temperature 曲线 + 标签）
│   ├── llm_client.py       # Ollama 客户端（keep_alive=5m + 缓存 + 15s 超时 + 结构校验）
│   └── version_info.py     # KataGo 版本/后端探测
├── games.db                # 运行时生成：SQLite 棋局存档数据库
├── logs/                   # 运行时生成：日志目录
├── static/
│   ├── css/style.css       # 样式（响应式布局 + 竖屏最大化棋盘）
│   └── js/
│       ├── board.js        # 自包含 Canvas 棋盘：渲染/规则/候选点/推演/势力/手质量
│       └── app.js          # 主逻辑：落子/AI/计时器/棋谱列表/复盘/数子/存档/历史
└── templates/
    └── index.html          # 主页面（顶栏/棋盘区/侧栏/模态弹窗）
```

---

## 完整 API 列表

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 主页面（页面 + 静态资源用相对路径 `./static/...` 部署子路径）|
| `/api/health` | GET | 健康检查，返回 `engine_lifecycle`（`running`/`asleep`/`mock` + PID + 倒计时秒）|
| `/api/analyze` | POST | 分析局面，返回候选点 Top N + KataGo root 信息 + ownership 势力数据 + 规则解说大纲 + LLM hash |
| `/api/commentary-enhance` | POST | 异步 LLM 润色解说（带 hash 进程内缓存），15 秒超时前端自动回退 |
| `/api/ai-move` | POST | AI 选一步落子（高等级取 Top1，低等级 temperature 加权随机）|
| `/api/lookahead` | POST | 对指定候选点 10 步深度推演（固定 maxVisits=500），返回 PV |
| `/api/levels` | GET | 返回所有 1~20 级的参数曲线 |
| `/api/score` | POST | 终局数子（中国规则 + ownership 估算），返回比分 |
| `/api/games` | GET/POST | 获取最近 N 局棋谱列表 / 保存一局棋谱到 SQLite |
| `/api/games/<id>` | GET/DELETE | 加载单局棋谱详情（含完整 moves）/ 删除单局 |

> **子路径部署**：如挂在反向代理的 `/go/` 子路径下，前端 `detectApiBase()` 自动按 `location.pathname` 检测 API 前缀，后端配合反代规则（`/go/` → 本服务 `/`，`/api/go/` → `/api/`）即可。

---

## 日志格式

统一格式 `时间 | 级别 | pid=… | req=… | logger | 消息`，双 Handler：
- 控制台：INFO 级别彩色输出
- 文件：`logs/go-coach.log`（按大小轮转），DEBUG 级别

关键日志阶段：
- `ENGINE_ANALYZE elapsed_ms=… mode=… maxVisits=… root_wr=… scoreLead=… bestMove=…`
- `ENGINE_LOOKAHEAD elapsed_ms=… pv_len=…`
- `ENGINE_AI_MOVE pick=R16 strategy=top1 temperature=0.3`
- `ANALYZE_END total_ms=… ollama=true/false`
- `LLM_POLISH_BEGIN/END hash=… elapsed_ms=…`
- `SCORE_END winner=B diff=+33.5`
- `GAME_SAVED id=… moves=…`

---

## 部署

### 首次加载提示
- **KataGo**：懒启动，首次落子/AI/分析操作才拉起，冷启动约 1~3 秒
- **Ollama + qwen2.5:7b-32k**：首次 LLM 请求加载模型可能 20~60 秒，后续命中缓存即 0ms；5 分钟无调用自动卸载释放显存

### 子路径反向代理部署（可选）

支持挂在 Nginx / Caddy / Cloudflare Tunnel 等任意反向代理的子路径下（如 `/go/`），无需改代码：

```text
公网 /go/     → 后端 /          （页面与静态资源）
公网 /api/go/ → 后端 /api/      （接口前缀）
```

前端 `detectApiBase()` 按 `location.pathname` 自动探测 API 前缀，后端无子路径感知，直接对外暴露 `127.0.0.1:5001` 即可。

---

## 开发路线

| 层级 | 模块 | 状态 |
|------|------|------|
| 一级 | 棋盘 + 人机模式 + 计时器 + 棋谱面板 | ✅ 完成 |
| 一级 | 复盘导航 + 终局数子裁判 | ✅ 完成 |
| 一级 | 棋谱存档 + 历史棋局列表 & 加载回放 | ✅ 完成 |
| 二级 | 5 步优选候选点 + 短标签解释 | ✅ 完成 |
| 二级 | 局势分析切换 + 势力图 + 10 步推演 | ✅ 完成 |
| 二级 | 胜率条 + 预计目数（数子弹窗）+ AI 解说 | ✅ 完成 |
| 二级 | 10 步推演胜率趋势图（折线） | ✅ 完成 |
| 三级 | AI 人格：柯洁式语料库 20~30 条 | 🔲 规划 |
| 三级 | 气泡触发条件（妙手/恶手/大逆转）+ 气泡 UI | 🔲 规划 |
| 三级 | 评论语随机抽取 + 避免重复 | 🔲 规划 |

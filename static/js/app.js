/* app.js — 主逻辑：落子分析、AI 对手、解说渲染、手质量评价、复盘导航、音效 */
(function () {
  "use strict";

  /* ========== 音效模块（Web Audio API 合成，无需外部文件） ========== */
  var Sfx = (function () {
    var ctx = null;
    var enabled = true;

    function ensure() {
      if (!enabled) return null;
      if (!ctx) {
        try { ctx = new (window.AudioContext || window.webkitAudioContext)(); }
        catch (e) { enabled = false; return null; }
      }
      if (ctx.state === "suspended") ctx.resume();
      return ctx;
    }

    function tone(freq, dur, type, vol, delay) {
      var c = ensure(); if (!c) return;
      var t0 = c.currentTime + (delay || 0);
      var osc = c.createOscillator();
      var g = c.createGain();
      osc.type = type || "sine";
      osc.frequency.setValueAtTime(freq, t0);
      g.gain.setValueAtTime(0, t0);
      g.gain.linearRampToValueAtTime(vol || 0.25, t0 + 0.008);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
      osc.connect(g); g.connect(c.destination);
      osc.start(t0); osc.stop(t0 + dur);
    }

    function sweep(f1, f2, dur, type, vol) {
      var c = ensure(); if (!c) return;
      var t0 = c.currentTime;
      var osc = c.createOscillator();
      var g = c.createGain();
      osc.type = type || "sine";
      osc.frequency.setValueAtTime(f1, t0);
      osc.frequency.exponentialRampToValueAtTime(f2, t0 + dur);
      g.gain.setValueAtTime(0, t0);
      g.gain.linearRampToValueAtTime(vol || 0.2, t0 + 0.01);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
      osc.connect(g); g.connect(c.destination);
      osc.start(t0); osc.stop(t0 + dur);
    }

    // 首次点击解锁 AudioContext（浏览器策略）
    document.addEventListener("click", function unlock() {
      ensure();
      document.removeEventListener("click", unlock);
    }, { once: true });

    return {
      place:      function () { tone(240, 0.07, "triangle", 0.22); },                          // 人落子：木质感 tock
      placeAI:    function () { tone(190, 0.09, "triangle", 0.22); },                          // AI落子：稍低沉
      capture:    function () { sweep(450, 120, 0.14, "sawtooth", 0.18); },                     // 提子：快速下滑
      illegal:    function () { tone(110, 0.12, "square", 0.12); },                             // 非法：低 buzz
      thinking:   function () { tone(660, 0.18, "sine", 0.06); },                               // AI思考：轻音
      newGame:    function () { tone(440, 0.08, "sine", 0.12); tone(660, 0.08, "sine", 0.12, 0.07); tone(880, 0.12, "sine", 0.12, 0.14); }, // 新局：上行和弦
      undo:       function () { tone(660, 0.06, "sine", 0.10); tone(440, 0.10, "sine", 0.10, 0.06); }, // 悔棋：下行
      candidateClick: function () { tone(1200, 0.025, "sine", 0.08); },                         // 候选点点击：轻 tick
      review:     function () { tone(900, 0.03, "sine", 0.08); },                               // 复盘导航：tick
      excellent:  function () { tone(880, 0.07, "sine", 0.14); tone(1320, 0.10, "sine", 0.14, 0.06); }, // 妙手：亮上行
      mediocre:   function () { tone(440, 0.08, "sine", 0.10); },                               // 俗手：中音
      bad:        function () { sweep(300, 140, 0.18, "sawtooth", 0.12); },                     // 恶手：下行 buzz
    };
  })();

  // 暴露给 board.js 使用
  window.Sfx = Sfx;

  function detectApiBase() {
    if (typeof window.__GO_API_BASE__ === "string" && window.__GO_API_BASE__) {
      return window.__GO_API_BASE__.replace(/\/+$/, "");
    }
    var p = (location && location.pathname) || "/";
    if (p === "/go" || p.indexOf("/go/") === 0) {
      return "/api/go";
    }
    return "/api";
  }
  var API_BASE = detectApiBase();

  var state = {
    boardSize: 19,
    komi: 7.5,
    rules: "chinese",
    aiLevel: 5,
    maxVisits: 300,          // KataGo 计算步数（0=自动按等级，>0=手动覆盖）
    playerColor: 1,
    mode: "hva",
    aiSide: 2,
    analyzing: false,
    aiThinking: false,
    aiQueued: false,
    // 手质量评价（黑白双方各自保留最新一条）
    prevRootWinrate: null,   // 落子前的黑方胜率
    lastMoveQuality: null,   // {label, color, delta_pct, description}
    lastBlackQuality: null,  // 黑方最新手质量
    lastWhiteQuality: null,  // 白方最新手质量
    // 全局胜率波动曲线：索引=第N手（落子1对应索引1，表示下完第一手的黑胜率）
    // 开局前 (index 0) = 50% (komi 已反映在首次分析后校正)
    winrateCurve: [0.5],
    // 手质量圈渲染：把"本手"中性色映射交给 app 层（黑棋→白圈，白棋→黑圈）
    // 复盘模式
    reviewMode: false,
    reviewIndex: -1,
    // 上一次分析结果（用于复盘时缓存）
    lastAnalysis: null,
    // 分析模式（点击局势分析按钮切换；竖屏下决定是否展开侧栏）
    analyzeMode: false,
    // 5步优选候选点显示开关（下棋模式也能切换）
    showCandidates: true,
    // 预推演缓存：{ candidateGtp: { pv, ready } }
    prefetchedLookahead: {},
    // 是否正在后台预推演
    prefetching: false,
    // 当前轮到的"是人的回合"（非AI）时，默默对1号点做深度推演缓存
    // 计时器
    timerBlack: 0,      // 黑方累计秒数
    timerWhite: 0,      // 白方累计秒数
    timerActive: false, // 计时是否运行中
    timerSide: 1,       // 当前计时方 1=黑 2=白
    timerInterval: null,
    timerStartTs: 0,    // 本轮开始时刻
    // 数子结果（存着供保存棋谱用）
    lastScoreResult: null,
  };

  var board = null;
  var dom = {};
  var trialReviewIndex = -1; // 复盘试下模式：>=0 表示在试下状态，记录进入时的 reviewIndex

  function init() {
    cacheDom();
    bindControls();
    initBoard();
    drawGlobalTrend();  // 开局即绘制空网格
    checkHealth();
    // 每 30 秒刷新引擎状态徽章（显示休眠/运行/剩余时间）
    setInterval(checkHealth, 30000);
  }

  function cacheDom() {
    dom.canvas = document.getElementById("board-canvas");
    dom.commentary = document.getElementById("commentary");
    dom.candidates = document.getElementById("candidates");
    dom.statusBar = document.getElementById("status-bar");
    dom.engineBadge = document.getElementById("engine-badge");
    dom.katagoVersion = document.getElementById("katago-version");
    dom.appVersion = document.getElementById("app-version");
    dom.btnNew = document.getElementById("btn-new");
    dom.btnUndo = document.getElementById("btn-undo");
    dom.btnAi = document.getElementById("btn-ai-move");
    dom.btnAnalyze = document.getElementById("btn-analyze");
    dom.btnCandidates = document.getElementById("btn-candidates");
    dom.selSize = document.getElementById("sel-size");
    dom.selLevel = document.getElementById("sel-level");
    dom.selMode = document.getElementById("sel-mode");
    dom.selVisits = document.getElementById("sel-visits");
    dom.moveCount = document.getElementById("move-count");
    dom.toMove = document.getElementById("to-move");
    dom.loading = document.getElementById("loading-overlay");
    // 复盘导航
    dom.btnReview = document.getElementById("btn-review");
    dom.btnFirst = document.getElementById("btn-first");
    dom.btnPrev = document.getElementById("btn-prev");
    dom.btnNext = document.getElementById("btn-next");
    dom.btnLast = document.getElementById("btn-last");
    dom.reviewBar = document.getElementById("review-bar");
    dom.reviewPos = document.querySelector(".review-pos");
    dom.trialBtn = document.getElementById("btn-trial");
    dom.trialReturnBtn = document.getElementById("btn-trial-return");
    dom.moveQuality = null; // 原本手妙手徽章已移除，兼容保留避免报错
    dom.globalTrendCanvas = document.getElementById("global-trend-canvas");
    dom.globalTrendHint = document.getElementById("global-trend-hint");
    dom.globalTrend = document.getElementById("global-trend");
    dom.wrBlack = document.getElementById("wr-black");
    dom.wrWhite = document.getElementById("wr-white");
    dom.wrBarFill = document.getElementById("wr-bar-fill");
    dom.wrBarFillWhite = document.getElementById("wr-bar-fill-white");
    // 胜率曲线旁目数显示
    dom.gtScoreB = document.getElementById("gt-score-b");
    dom.gtScoreW = document.getElementById("gt-score-w");
    dom.gtScoreDiff = document.getElementById("gt-score-diff");
    // 计时器
    dom.timerBlack = document.getElementById("timer-black");
    dom.timerWhite = document.getElementById("timer-white");
    // 棋谱记录
    dom.movesList = document.getElementById("moves-list");
    // 数子 & 历史
    dom.btnScore = document.getElementById("btn-score");
    dom.scoreModal = document.getElementById("score-modal");
    dom.scoreTitle = document.getElementById("score-title");
    dom.scoreBody = document.getElementById("score-body");
    dom.btnSaveGame = document.getElementById("btn-save-game");
    dom.btnCloseScore = document.getElementById("btn-close-score");
    dom.historyModal = document.getElementById("history-modal");
    dom.historyList = document.getElementById("history-list");
    dom.btnCloseHistory = document.getElementById("btn-close-history");
    dom.btnHelp = document.getElementById("btn-help");
  }

  function bindControls() {
    dom.btnNew.addEventListener("click", newGame);
    dom.btnUndo.addEventListener("click", undo);
    dom.btnAi.addEventListener("click", function () {
      state.aiQueued = false;
      aiMove();
    });
    dom.btnAnalyze.addEventListener("click", function () { toggleAnalyzeMode(); });
    dom.btnCandidates.addEventListener("click", function () { toggleCandidates(); });
    dom.selSize.addEventListener("change", function () {
      state.boardSize = parseInt(dom.selSize.value, 10);
      newGame();
    });
    dom.selLevel.addEventListener("change", function () {
      state.aiLevel = parseInt(dom.selLevel.value, 10);
    });
    if (dom.selVisits) dom.selVisits.addEventListener("change", function () {
      state.maxVisits = parseInt(dom.selVisits.value, 10);
    });
    dom.selMode.addEventListener("change", function () {
      var m = dom.selMode.value;
      state.mode = m;
      if (m === "hva") { state.playerColor = 1; state.aiSide = 2; }
      else if (m === "avh") { state.playerColor = 2; state.aiSide = 1; }
      else { state.playerColor = 0; state.aiSide = 0; } // hvh 两人对下 / research 研究模式 都没有 AI 自动下
      newGame();
    });
    window.addEventListener("resize", function () { resizeCanvas(); drawGlobalTrend(); });
    // 复盘/棋谱：合并按钮 — 有棋局时切换复盘模式，无棋局时打开历史列表
    if (dom.btnReview) dom.btnReview.addEventListener("click", function () {
      if (board && board.history.length > 0) {
        toggleReview();
      } else {
        showHistory();
      }
    });
    if (dom.btnFirst) dom.btnFirst.addEventListener("click", function () { reviewGoto(0); });
    if (dom.btnPrev) dom.btnPrev.addEventListener("click", function () { reviewGoto(state.reviewIndex - 1); });
    if (dom.btnNext) dom.btnNext.addEventListener("click", function () { reviewGoto(state.reviewIndex + 1); });
    if (dom.btnLast) dom.btnLast.addEventListener("click", function () { reviewGoto(board.history.length - 1); });
    // 复盘试下/返回
    if (dom.trialBtn) dom.trialBtn.addEventListener("click", enterTrialMode);
    if (dom.trialReturnBtn) dom.trialReturnBtn.addEventListener("click", exitTrialMode);
    // 数子
    if (dom.btnScore) dom.btnScore.addEventListener("click", doScore);
    if (dom.btnSaveGame) dom.btnSaveGame.addEventListener("click", saveCurrentGame);
    if (dom.btnCloseScore) dom.btnCloseScore.addEventListener("click", function () { dom.scoreModal.style.display = "none"; });
    // 历史棋局弹窗关闭
    if (dom.btnCloseHistory) dom.btnCloseHistory.addEventListener("click", function () { dom.historyModal.style.display = "none"; });
    // 使用说明 → 新标签页打开 /help
    if (dom.btnHelp) dom.btnHelp.addEventListener("click", function () {
      window.open("./help", "_blank", "noopener,noreferrer");
    });
    // 侧栏 panel 折叠：点击 .panel-header 切换对应 panel 的 collapsed 类
    var headers = document.querySelectorAll(".panel.collapsible > h3.panel-header");
    for (var hi = 0; hi < headers.length; hi++) {
      headers[hi].addEventListener("click", (function (hdr) {
        return function () {
          var targetId = hdr.getAttribute("data-target");
          var panel = targetId ? document.getElementById(targetId) : hdr.parentElement;
          if (!panel) return;
          var collapsed = panel.classList.toggle("collapsed");
          // 保存折叠状态到 localStorage（按 panel id 记忆，可选）
          try {
            if (panel.id) {
              localStorage.setItem("panel_collapsed_" + panel.id, collapsed ? "1" : "0");
            }
          } catch (e) { /* ignore */ }
        };
      })(headers[hi]));
    }
    // 恢复上次的折叠状态
    try {
      var allPanels = document.querySelectorAll(".panel.collapsible[id]");
      for (var pi = 0; pi < allPanels.length; pi++) {
        var p = allPanels[pi];
        if (localStorage.getItem("panel_collapsed_" + p.id) === "1") {
          p.classList.add("collapsed");
        }
      }
    } catch (e) { /* ignore */ }
  }

  function initBoard() {
    resizeCanvas();
    board = new GoBoard(dom.canvas, {
      size: state.boardSize,
      onMove: function (color, x, y, captured) {
        // 切换计时方（落子后，计时切到对方）
        timerSwitch(color);
        updateStatus();
        updateMovesList();
        // 落子后：只有「分析模式」开着才显示候选点和解说面板
        // 无论如何都做手质量评价（如果 analyze 开关没开就不显示解说）
        var doRender = state.analyzeMode;
        analyze(doRender, { x: x, y: y, color: color });
        // 复盘试下模式：跳过 AI 逻辑和预推演
        var isTrial = (trialReviewIndex >= 0);
        if (!isTrial && state.mode !== "hvh" && state.aiSide !== 0) {
          var next = board.history.length % 2 === 0 ? 1 : 2;
          if (next === state.aiSide && !state.aiQueued) {
            state.aiQueued = true;
            setTimeout(aiMove, 400);
          }
        }
        // 如果现在轮到的是「人」下的回合，默默后台预推演 1 号点（不显示，只缓存）
        if (!isTrial) {
          schedulePrefetchLookahead();
        }
      },
      onIllegal: function (msg) {
        flash(msg);
      },
    });
    updateStatus();
    // 若 AI 执黑 (avh 模式)，新局直接让 AI 下第一步
    if (state.mode === "avh" && board.history.length === 0 && !state.aiQueued) {
      state.aiQueued = true;
      setTimeout(aiMove, 300);
    } else if (board.history.length === 0) {
      // 新局且人先下：预推演 1 号点
      schedulePrefetchLookahead();
    }
  }

  function resizeCanvas() {
    var section = dom.canvas.closest(".board-section");
    if (!section) return;

    // 判断是否为手机/竖屏布局：棋盘是否可滚动（主布局变单列，overflow:visible）
    var isMobile = window.matchMedia("(max-width: 820px), (orientation: portrait) and (max-width: 1024px)").matches;

    var size;
    if (isMobile) {
      // 手机端：棋盘以屏幕宽度为基准，正方形最大化
      // 留少量 padding 给左右各 4~6px 的边距
      var screenW = Math.min(window.innerWidth, section.clientWidth || window.innerWidth);
      size = Math.floor(screenW - 12);
      // 防止极端窄屏导致棋盘过小
      size = Math.max(260, size);
    } else {
      // 电脑/横屏：维持在一页以内，取可用宽高的较小值
      var availW = section.clientWidth - 12;
      var infoH = 0;
      var info = section.querySelector(".board-info");
      if (info) infoH = info.offsetHeight + 8;
      var availH = section.clientHeight - infoH - 4;
      size = Math.min(availW, availH);
      size = Math.max(280, size);
    }

    // 高清 DPR 适配：实际像素 *= devicePixelRatio，CSS 尺寸保持 size
    var dpr = window.devicePixelRatio || 1;
    // 限制最大 DPR，避免超大屏/桌面缩放导致 canvas 过大（>2048 对老浏览器不友好）
    dpr = Math.min(dpr, 3);
    var pxSize = Math.round(size * dpr);
    dom.canvas.width = pxSize;
    dom.canvas.height = pxSize;
    dom.canvas.style.width = size + "px";
    dom.canvas.style.height = size + "px";

    // board.js 使用 canvas.width 做渲染，点击事件按 rect.width/canvas.width 换算，
    // 两者比例恰好为 dpr，所以渲染和点击都自动正确，无需额外修改 board.js
    if (board) board.render();
  }

  // ---- 分析模式切换：点击「局势分析」按钮进入/退出 ----
  function toggleAnalyzeMode() {
    // 强制复位分析锁（防止 analyze() 之前的请求卡住导致状态残留，按钮点击无效）
    state.analyzing = false;
    showLoading(false);
    // 如果当前在复盘模式，先退出复盘模式（确保棋盘可点击）
    if (state.reviewMode) {
      exitReview();
    }
    state.analyzeMode = !state.analyzeMode;
    document.body.classList.toggle("analyze-mode", state.analyzeMode);
    dom.btnAnalyze.classList.toggle("analyze-active", state.analyzeMode);
    if (state.analyzeMode) {
      // 进入分析模式：立即分析并显示候选点/解说
      analyze(true);
    } else {
      // 退出分析模式：清除候选点、解说面板、推演标记、势力图
      if (board) {
        board.setCandidateMarks([]);
        board.clearLookaheadMarks();
        board.clearOwnerData();
      }
      clearTrend();
      dom.candidates.innerHTML = '<p class="hint">落子后显示 AI 推荐选点</p>';
      dom.commentary.innerHTML = '<p class="hint">落子后将自动分析局面并给出解说。</p>';
    }
    // 竖屏下布局变化，需要重新计算棋盘大小 + 全局曲线 canvas 尺寸
    setTimeout(function () { resizeCanvas(); drawGlobalTrend(); }, 60);
  }

  // ---- 五点优选候选点显示/隐藏切换 ----
  function toggleCandidates() {
    state.showCandidates = !state.showCandidates;
    var btn = dom.btnCandidates;
    if (!btn) return;
    if (state.showCandidates) {
      btn.classList.remove("toggle-off");
      btn.classList.add("toggle-on");
      // 立即恢复显示候选点（用缓存的分析数据）
      if (state.lastAnalysis && state.lastAnalysis.moveInfos && board.history.length > 0) {
        var marks = state.lastAnalysis.moveInfos.slice(0, 5).map(function (m, i) {
          var xy = gtpToXY(m.move, state.boardSize);
          return { x: xy.x, y: xy.y, order: i, winrate: m.winrate };
        });
        board.setCandidateMarks(marks);
      }
    } else {
      btn.classList.remove("toggle-on");
      btn.classList.add("toggle-off");
      board.setCandidateMarks([]);
    }
  }

  // ---- 后台预推演：默默对 1 号点做深度推演（不显示，只存到缓存） ----
  var prefetchToken = 0;
  function schedulePrefetchLookahead() {
    // 取消上一次未完成的预推演请求（因为局面已变）
    prefetchToken++;
    state.prefetchedLookahead = {};
    if (state.prefetching) return; // 等当前的完成再排下一个
    var thisToken = prefetchToken;
    // 延迟 200ms 再发，避免连走两手时多次请求
    setTimeout(function () {
      if (thisToken !== prefetchToken) return;
      doPrefetchLookahead(thisToken);
    }, 200);
  }

  function doPrefetchLookahead(token) {
    // 不是「人」的回合就不需要（AI 回合用户不需要看推演）
    var toMove = board.history.length % 2 === 0 ? 1 : 2; // 1=黑 2=白
    var humanTurn = false;
    if (state.mode === "hva") humanTurn = (toMove === state.playerColor);
    else if (state.mode === "avh") humanTurn = (toMove === state.playerColor);
    else if (state.mode === "hvh") humanTurn = true;
    else humanTurn = true; // research
    if (!humanTurn) return;

    // 找 1 号点：先用上一次分析结果里的首选
    var candidateGtp = null;
    if (state.lastAnalysis && state.lastAnalysis.moveInfos && state.lastAnalysis.moveInfos[0]) {
      candidateGtp = state.lastAnalysis.moveInfos[0].move;
    }
    if (!candidateGtp) return; // 没有分析结果就先不算

    state.prefetching = true;
    var payload = {
      moves: board.getMoves(),
      boardSize: state.boardSize,
      komi: state.komi,
      rules: state.rules,
      level: state.aiLevel,
      candidate: candidateGtp,
    };
    fetch(API_BASE + "/lookahead", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (token !== prefetchToken) return; // 被新的排程取代
        if (data.ok && data.pv && data.pv.length > 0) {
          state.prefetchedLookahead[candidateGtp.toUpperCase()] = {
            pv: data.pv.slice(),
            ready: true,
          };
        }
      })
      .catch(function () { /* 静默失败 */ })
      .finally(function () {
        if (token === prefetchToken) {
          state.prefetching = false;
          // 局面可能又变了，再检查一次要不要排新的
          if (!_isCacheMatch()) setTimeout(schedulePrefetchLookahead, 0);
        }
      });
  }

  function _isCacheMatch() {
    // 如果缓存里有对应 1 号点的结果，说明当前局面的预推演已就绪
    var cand = state.lastAnalysis && state.lastAnalysis.moveInfos
      ? state.lastAnalysis.moveInfos[0] && state.lastAnalysis.moveInfos[0].move
      : null;
    if (!cand) return false;
    return !!state.prefetchedLookahead[cand.toUpperCase()];
  }

  function newGame() {
    Sfx.newGame();
    state.aiQueued = false;
    state.prevRootWinrate = null;
    state.lastMoveQuality = null;
    state.lastBlackQuality = null;
    state.lastWhiteQuality = null;
    state.reviewMode = false;
    state.reviewIndex = -1;
    trialReviewIndex = -1; // 清理试下模式
    state.lastAnalysis = null;
    state.prefetchedLookahead = {};
    state.lastScoreResult = null;
    prefetchToken++;
    // 新局默认退出分析模式（下棋模式最大化棋盘）
    state.analyzeMode = false;
    document.body.classList.remove("analyze-mode");
    dom.btnAnalyze.classList.remove("analyze-active");
    // 重置5步优选按钮状态为显示
    state.showCandidates = true;
    if (dom.btnCandidates) {
      dom.btnCandidates.classList.remove("toggle-off");
      dom.btnCandidates.classList.add("toggle-on");
    }
    if (dom.reviewBar) dom.reviewBar.style.display = "none";
    // 清理试下按钮状态
    if (dom.trialBtn) dom.trialBtn.style.display = "inline-block";
    if (dom.trialReturnBtn) dom.trialReturnBtn.style.display = "none";
    if (dom.btnFirst) dom.btnFirst.disabled = false;
    if (dom.btnPrev) dom.btnPrev.disabled = false;
    if (dom.btnNext) dom.btnNext.disabled = false;
    if (dom.btnLast) dom.btnLast.disabled = false;
    resetWinrate();
    resetGlobalTrend();
    // 重置计时器
    timerReset();
    if (board) {
      board.setReviewMode(false);
      board.clear();
      if (board.size !== state.boardSize) board.resize(state.boardSize);
    }
    dom.commentary.innerHTML = '<p class="hint">落子后将自动分析局面并给出解说。</p>';
    dom.candidates.innerHTML = '<p class="hint">落子后显示 AI 推荐选点</p>';
    if (dom.movesList) dom.movesList.innerHTML = '<p class="hint">尚未落子</p>';
    clearTrend();
    updateStatus();
    // 新局开始计时（黑方）
    timerStart(1);
    if (state.mode === "avh" && !state.aiQueued) {
      state.aiQueued = true;
      setTimeout(aiMove, 300);
    }
  }

  function undo() {
    Sfx.undo();
    state.aiQueued = false;
    state.prevRootWinrate = null;
    state.prefetchedLookahead = {};
    prefetchToken++;
    // 复盘试下模式：先退出试下
    if (trialReviewIndex >= 0) exitTrialMode();
    if (state.reviewMode) exitReview();
    if (state.mode === "hva" || state.mode === "avh") {
      // 人机模式：退人+AI 两步
      board.undo();
      board.undo();
    } else {
      // 双人 / 研究模式：退一步
      board.undo();
    }
    board.clearStoneBorders();
    board.clearLookaheadMarks();
    board.clearOwnerData();
    clearTrend();
    dom.candidates.innerHTML = "";
    // 悔棋后：全局胜率曲线把最后一个点（对应下完被悔那手）弹出，然后重画
    if (board && state.winrateCurve.length > 1) {
      state.winrateCurve.length = Math.min(state.winrateCurve.length - 1, board.history.length);
      if (state.winrateCurve.length === 0) state.winrateCurve = [0.5];
    }
    drawGlobalTrend();
    updateStatus();
    updateMovesList();
    if (board.history.length > 0) analyze(state.analyzeMode);
  }

  function updateStatus() {
    var count = board ? board.history.length : 0;
    dom.moveCount.textContent = count;
    var nextColor = count % 2 === 0 ? "黑" : "白";
    dom.toMove.textContent = nextColor;
  }

  // ---- 更新黑白胜率条 + 目数显示（rootInfo.winrate 是黑方视角） ----
  function updateWinrate(rootInfo) {
    if (!rootInfo || typeof rootInfo.winrate !== "number") return;
    var blackWr = rootInfo.winrate;
    var whiteWr = 1 - blackWr;
    var blackPct = (blackWr * 100).toFixed(1);
    var whitePct = (whiteWr * 100).toFixed(1);
    if (dom.wrBlack) dom.wrBlack.textContent = blackPct + "%";
    if (dom.wrWhite) dom.wrWhite.textContent = whitePct + "%";
    if (dom.wrBarFill) dom.wrBarFill.style.width = blackPct + "%";
    if (dom.wrBarFillWhite) dom.wrBarFillWhite.style.width = whitePct + "%";

    // 实时更新目数显示（scoreLead 来自 KataGo 黑方视角的目数领先）
    var scoreLead = rootInfo.scoreLead;
    if (typeof scoreLead === "number" && dom.gtScoreB && dom.gtScoreW) {
      // scoreLead > 0 = 黑优，scoreLead < 0 = 白优
      var bScore = scoreLead >= 0 ? scoreLead : 0;
      var wScore = scoreLead < 0 ? -scoreLead : 0;
      dom.gtScoreB.textContent = bScore.toFixed(1);
      dom.gtScoreW.textContent = wScore.toFixed(1);
      if (dom.gtScoreDiff) {
        var diffText = scoreLead >= 0
          ? "黑优 " + scoreLead.toFixed(1) + " 目"
          : "白优 " + (-scoreLead).toFixed(1) + " 目";
        dom.gtScoreDiff.textContent = diffText;
      }
    } else {
      if (dom.gtScoreB) dom.gtScoreB.textContent = "--";
      if (dom.gtScoreW) dom.gtScoreW.textContent = "--";
      if (dom.gtScoreDiff) dom.gtScoreDiff.textContent = "";
    }
  }

  function resetWinrate() {
    if (dom.wrBlack) dom.wrBlack.textContent = "50.0%";
    if (dom.wrWhite) dom.wrWhite.textContent = "50.0%";
    if (dom.wrBarFill) dom.wrBarFill.style.width = "50%";
    if (dom.wrBarFillWhite) dom.wrBarFillWhite.style.width = "50%";
  }

  // ---- 手质量评价（前端逻辑，对比前后两手的黑方胜率） ----
  function evaluateMoveQuality(prevWr, currWr, moverColor) {
    var moverBlack = (moverColor === 1);
    var delta;
    if (moverBlack) {
      delta = currWr - prevWr;  // 黑下，黑胜率应升
    } else {
      delta = prevWr - currWr;  // 白下，黑胜率应降
    }
    var deltaPct = Math.round(delta * 1000) / 10;

    if (deltaPct >= -0.5) {
      return { label: "妙手", color: "#00CC00", delta_pct: deltaPct,
               description: "胜率变化 " + deltaPct.toFixed(1) + "%，与 AI 最优解几乎一致！" };
    }
    if (deltaPct >= -3.0) {
      return { label: "本手", color: null, delta_pct: deltaPct,
               description: "胜率变化 " + deltaPct.toFixed(1) + "%，稳健正着，接近 AI 推荐。" };
    }
    if (deltaPct >= -8.0) {
      return { label: "俗手", color: "#FFCC00", delta_pct: deltaPct,
               description: "胜率变化 " + deltaPct.toFixed(1) + "%，略有损失，AI 有更优选点。" };
    }
    return { label: "恶手", color: "#FF4444", delta_pct: deltaPct,
             description: "胜率变化 " + deltaPct.toFixed(1) + "%，损失较大，建议考虑其他选点。" };
  }

  // ---- 调用后端分析 ----
  // render=true 时会在棋盘上画候选点并填充解说面板
  // render=false 时只更新 lastAnalysis 用于后台手质量评价和胜率条，不显示任何东西
  // reviewIndex: 复盘时指定分析到第几手（仅发送到该位置，解决复盘形式判断不更新 BUG）
  function analyze(render, moveInfo, reviewIndex) {
    if (state.analyzing) return;
    if (!board) return;
    if (!render && board.history.length === 0) return;
    state.analyzing = true;
    // 只有 render=true 时才显示转圈加载动画（下棋模式不要转圈干扰用户）
    if (render) showLoading(true);

    // 复盘模式：只发送到当前复盘位置，解决形势判断不更新的 BUG
    var analyzeMoves;
    if (typeof reviewIndex === "number" && reviewIndex >= 0) {
      analyzeMoves = _getMovesUpTo(reviewIndex);
    } else {
      analyzeMoves = board.getMoves();
    }

    var payload = {
      moves: analyzeMoves,
      boardSize: state.boardSize,
      komi: state.komi,
      rules: state.rules,
      level: state.aiLevel,
      maxVisits: state.maxVisits,
    };

    // 保存落子前的黑方胜率（用于手质量评价）
    var prevWr = null;
    if (state.lastAnalysis && state.lastAnalysis.rootInfo) {
      prevWr = state.lastAnalysis.rootInfo.winrate;
    }

    fetch(API_BASE + "/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.analyzing = false;
        if (render) showLoading(false);
        if (data.ok) {
          // 手质量评价：有上一次分析 + 当前是落子后分析
          if (prevWr !== null && moveInfo && data.analysis.rootInfo) {
            var currWr = data.analysis.rootInfo.winrate;
            var quality = evaluateMoveQuality(prevWr, currWr, moveInfo.color);
            state.lastMoveQuality = quality;
            // 黑白双方各自保留最新一条手质量
            if (moveInfo.color === 1) {
              state.lastBlackQuality = { quality: quality, moveInfo: moveInfo };
            } else {
              state.lastWhiteQuality = { quality: quality, moveInfo: moveInfo };
            }
            // 手质量音效
            if (quality.label === "妙手") Sfx.excellent();
            else if (quality.label === "俗手") Sfx.mediocre();
            else if (quality.label === "恶手") Sfx.bad();
            // 决定棋子圈颜色：妙手/俗手/恶手 = 彩色；本手 = 中性色（黑棋→白圈，白棋→黑圈）
            var circleColor = quality.color; // 可能 null（本手）
            if (!circleColor) {
              circleColor = (moveInfo.color === 1) ? "#ffffff" : "#000000";
            }
            if (circleColor) {
              board.setStoneBorder(moveInfo.x, moveInfo.y, circleColor, moveInfo.color);
            }
            renderMoveQuality(); // 兼容 no-op
          }
          state.lastAnalysis = data.analysis;
          // 胜率条：始终更新（即使没打开分析模式也显示）
          if (data.analysis.rootInfo) {
            updateWinrate(data.analysis.rootInfo);
            // 记录全局胜率曲线上的一个点 + 重画
            recordWinratePoint(data.analysis.rootInfo.winrate);
            drawGlobalTrend();
          }
          // 5 步优选候选点：受 showCandidates 开关控制
          if (state.showCandidates && board.history.length > 0 && data.analysis.moveInfos) {
            var marks = data.analysis.moveInfos.slice(0, 5).map(function (m, i) {
              var xy = gtpToXY(m.move, state.boardSize);
              return { x: xy.x, y: xy.y, order: i, winrate: m.winrate };
            });
            board.setCandidateMarks(marks);
          } else if (!state.showCandidates || board.history.length === 0) {
            board.setCandidateMarks([]);
          }
          if (render) {
            // 渲染解说面板 + 势力图 + 候选点列表侧栏（分析模式专属）
            var extra = {};
            if (data.ollama_ready && data.commentary_hash) {
              extra.enhancing = true;
            }
            renderCommentary(data.commentary, data.analysis, extra);
            // 势力分布图
            if (data.analysis.ownerData) {
              board.setOwnerData(data.analysis.ownerData);
            } else {
              board.clearOwnerData();
            }
            // 异步请求 LLM 润色（不阻塞用户）
            if (data.ollama_ready && data.commentary_hash) {
              setTimeout(function () {
                enhanceCommentary(
                  data.commentary_hash,
                  data.commentary,
                  payload.moves,
                  payload.boardSize,
                  payload.level
                );
              }, 60);
            }
          } else {
            // 下棋模式：势力图不显示（只在分析模式打开）
            board.clearOwnerData();
          }
        } else {
          if (render) flash("分析失败: " + (data.error || "未知错误"));
        }
        // 分析完成后，如果是人回合，触发预推演 1 号点（后台静默，不显示）
        schedulePrefetchLookahead();
      })
      .catch(function (err) {
        state.analyzing = false;
        if (render) showLoading(false);
        if (render) flash("网络错误: " + err.message);
      });
  }

  // ---- 请求 AI 落子 ----
  function aiMove() {
    if (state.aiThinking) return;
    if (!board) return;
    if (state.mode !== "hvh" && state.aiSide !== 0) {
      var should = board.history.length % 2 === 0 ? 1 : 2;
      if (should !== state.aiSide) {
        flash("当前还没轮到 AI 落子");
        state.aiQueued = false;
        return;
      }
    }
    state.aiThinking = true;
    dom.btnAi.disabled = true;
    dom.btnAi.textContent = "AI 思考中...";
    Sfx.thinking();

    // 保存落子前的黑方胜率
    var prevWr = null;
    if (state.lastAnalysis && state.lastAnalysis.rootInfo) {
      prevWr = state.lastAnalysis.rootInfo.winrate;
    }

    var payload = {
      moves: board.getMoves(),
      boardSize: state.boardSize,
      komi: state.komi,
      rules: state.rules,
      level: state.aiLevel,
      maxVisits: state.maxVisits,
    };

    fetch(API_BASE + "/ai-move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.aiThinking = false;
        state.aiQueued = false;
        dom.btnAi.disabled = false;
        dom.btnAi.textContent = "AI 落子";
        if (data.ok && data.move) {
          var mv = data.move;
          board.placeExternal(mv.x, mv.y, mv.color);
          Sfx.placeAI();
          // AI 落子后：切换计时 + 更新棋谱 + 分析
          timerSwitch(mv.color);
          updateStatus();
          updateMovesList();
          analyze(true, { x: mv.x, y: mv.y, color: mv.color });
        } else {
          flash("AI 落子失败: " + (data.error || "未知错误"));
        }
      })
      .catch(function (err) {
        state.aiThinking = false;
        state.aiQueued = false;
        dom.btnAi.disabled = false;
        dom.btnAi.textContent = "AI 落子";
        flash("网络错误: " + err.message);
      });
  }

  // ---- 渲染全局胜率波动曲线（下完每一手的黑方胜率折线）----
  function recordWinratePoint(wr) {
    if (typeof wr !== "number") return;
    var targetIdx = board ? board.history.length : 0;
    state.winrateCurve[targetIdx] = wr;
    // 稀疏填充中间的洞（如某些手未重新分析则复用前一个值）
    for (var i = 0; i < targetIdx; i++) {
      if (typeof state.winrateCurve[i] !== "number") {
        state.winrateCurve[i] = (i > 0 ? state.winrateCurve[i - 1] : 0.5);
      }
    }
  }
  function drawGlobalTrend() {
    var cv = dom.globalTrendCanvas;
    if (!cv || !cv.getContext) return;
    var pts = state.winrateCurve.slice();
    // 最少 2 点才能画线段；开局前显示空网格
    if (pts.length < 2) {
      var startWr = pts[0] || 0.5;
      pts = [startWr, startWr];
    }
    // DPR 适配
    var cssW = cv.clientWidth || 320;
    var cssH = cv.clientHeight || 80;
    var dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(cssW * dpr) || cv.height !== Math.round(cssH * dpr)) {
      cv.width = Math.round(cssW * dpr);
      cv.height = Math.round(cssH * dpr);
    }
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var padL = 28, padR = 6, padT = 6, padB = 14;
    var plotW = cssW - padL - padR;
    var plotH = cssH - padT - padB;
    // 背景网格 0 / 50 / 100
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    ctx.font = "10px 'Inter','PingFang SC',Arial,sans-serif";
    ctx.fillStyle = "#6a7a9a";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    [0, 50, 100].forEach(function (pct) {
      var y = padT + plotH * (1 - pct / 100);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(pct + "%", padL - 3, y);
    });
    // 50% 参考虚线
    ctx.strokeStyle = "rgba(233,69,96,0.25)";
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    var y50 = padT + plotH * 0.5;
    ctx.moveTo(padL, y50);
    ctx.lineTo(padL + plotW, y50);
    ctx.stroke();
    ctx.setLineDash([]);

    var n = pts.length - 1; // 手数：0 ~ n
    var xStep = n > 0 ? plotW / n : plotW;
    function ptX(i) { return padL + i * xStep; }
    function ptY(v) { return padT + plotH * (1 - Math.max(0, Math.min(1, v))); }

    // 分段颜色：黑段绿(0.5+)/白段红(<0.5)
    for (var si = 0; si < n; si++) {
      var v0 = pts[si], v1 = pts[si + 1];
      var mid = (v0 + v1) / 2;
      var segColor = mid >= 0.5 ? "#2ed573" : "#e94560";
      ctx.strokeStyle = segColor;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(ptX(si), ptY(v0));
      ctx.lineTo(ptX(si + 1), ptY(v1));
      ctx.stroke();
    }
    // 起点 + 最新点 圆点
    var lastV = pts[pts.length - 1];
    ctx.fillStyle = lastV >= 0.5 ? "#2ed573" : "#e94560";
    ctx.beginPath();
    ctx.arc(ptX(pts.length - 1), ptY(lastV), 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#e9c46a";
    ctx.beginPath();
    ctx.arc(ptX(0), ptY(pts[0]), 2, 0, Math.PI * 2);
    ctx.fill();

    // 复盘模式：高亮当前手位置（竖线 + 亮点）
    if (state.reviewMode && state.reviewIndex >= 0 && state.reviewIndex < pts.length) {
      var hi = state.reviewIndex;
      var hx = ptX(hi);
      var hy = ptY(pts[hi]);
      // 竖线
      ctx.strokeStyle = "#e9c46a";
      ctx.lineWidth = 2;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      // 大亮点
      ctx.fillStyle = "#f0d060";
      ctx.shadowColor = "#f0d060";
      ctx.shadowBlur = 8;
      ctx.beginPath();
      ctx.arc(hx, hy, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
      // 手数标注
      ctx.fillStyle = "#f0d060";
      ctx.font = "bold 10px -apple-system, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillText("第" + (hi + 1) + "手", hx, padT - 2);
    }
    // Hint
    if (dom.globalTrendHint) {
      var last = (lastV * 100).toFixed(1);
      var start = (pts[0] * 100).toFixed(1);
      dom.globalTrendHint.textContent = (board && board.history.length ? board.history.length : 0)
        + " 手 · 黑 " + start + "% → " + last + "%";
    }
  }
  function resetGlobalTrend() {
    state.winrateCurve = [0.5];
    drawGlobalTrend();
    if (dom.globalTrendHint) dom.globalTrendHint.textContent = "开局后显示";
  }
  // 兼容旧函数名（其他地方若有调用 renderMoveQuality 不会报错）
  function renderMoveQuality() { /* no-op；本手妙手窗口已替换为全局胜率曲线 */ }

  // ---- 渲染解说面板 + 候选点 ----
  function renderCommentary(commentary, analysis, extraFlags) {
    extraFlags = extraFlags || {};
    // 候选点标记到棋盘（受 showCandidates 开关控制）
    if (state.showCandidates) {
      var marks = (analysis.moveInfos || []).slice(0, 5).map(function (m, i) {
        var xy = gtpToXY(m.move, state.boardSize);
        return { x: xy.x, y: xy.y, order: i, winrate: m.winrate };
      });
      board.setCandidateMarks(marks);
    }

    // 候选点列表（带策略思路）— 序号 A~E 与棋盘候选点字母一致
    var html = "";
    var infos = (analysis.moveInfos || []).slice(0, 5);
    var strategies = commentary.candidate_strategies || [];
    var candColors = ["#006400", "#228B22", "#3CB371", "#66CDAA", "#90EE90"];
    for (var i = 0; i < infos.length; i++) {
      var m = infos[i];
      var wr = (m.winrate * 100).toFixed(1);
      var sc = (m.scoreLead >= 0 ? "+" : "") + m.scoreLead.toFixed(1);
      var cc = candColors[Math.min(i, candColors.length - 1)];
      var letter = String.fromCharCode(65 + i); // A / B / C / D / E
      var strat = strategies[i] || "";
      html += '<div class="candidate" style="border-left-color:' + cc + '" data-rank="' + i + '">' +
        '<span class="rank" style="background:' + cc + '; padding: 0 7px;">' + letter + '</span>' +
        '<span class="coord">' + m.move + '</span>' +
        '<span class="stat">胜率 ' + wr + '%</span>' +
        '<span class="stat">目数 ' + sc + '</span>' +
        '<span class="visits">' + m.visits + '次</span>' +
        (strat ? '<div class="cand-strategy">' + strat + '</div>' : '') +
        "</div>";
    }
    dom.candidates.innerHTML = html || '<p class="hint">暂无候选点</p>';

    // 候选点列表点击事件 — 进入 10 步推演视图
    var candEls = dom.candidates.querySelectorAll(".candidate");
    for (var ci = 0; ci < candEls.length; ci++) {
      candEls[ci].addEventListener("click", function (rank) {
        return function () { showLookahead(rank, marks[rank]); };
      }(ci));
    }

    // 解说文本（只保留真正有用的内容：总结 + 策略 + 选点对比定性）
    var c = commentary;
    var text = "";
    // 正在润色中 + 来源徽章
    var badges = "";
    if (extraFlags.enhancing) {
      badges += '<span class="badge ldr enhancing">🧠 正在润色教学解说…</span>';
    }
    if (extraFlags.polished) {
      badges += '<span class="badge ldr polished">🧠 AI 教练解说</span>';
    } else {
      badges += '<span class="badge ldr rule">⚙️ 规则解说</span>';
    }
    text += '<div class="commentary-header">' + badges + '</div>';
    text += '<div class="commentary-summary">' + c.summary + "</div>";
    if (c.comparison) {
      text += '<div class="commentary-section"><h4>选点判断</h4><p>' + c.comparison + "</p></div>";
    }
    // 五分区势力描述
    if (c.regions && c.regions.description) {
      text += '<div class="commentary-section"><h4>势力格局</h4><p>' + c.regions.description + "</p></div>";
    }
    // policy 直觉 vs 搜索 对比
    if (c.policy_note) {
      text += '<div class="commentary-section"><h4>AI 思考方式</h4><p>' + c.policy_note + "</p></div>";
    }
    text += '<div class="commentary-section"><h4>策略建议</h4><p>' + c.strategy + "</p></div>";
    if (analysis.mode === "mock") {
      text += '<div class="mock-note">⚠ Mock 模式数据，放入 KataGo 后启用真实分析</div>';
    }
    dom.commentary.innerHTML = text;
  }

  // ---- 请求 LLM 异步润色解说 ----
  var llmEnhanceReqId = 0;
  var LLM_ENHANCE_TIMEOUT = 15000; // 15 秒超时

  function enhanceCommentary(commHash, commentary, movesList, size, level) {
    var reqId = ++llmEnhanceReqId;
    // 超时保护：15 秒后自动移除"正在润色"提示
    var timeoutId = setTimeout(function () {
      if (reqId !== llmEnhanceReqId) return;
      var latest = state.lastAnalysis;
      if (latest) {
        renderCommentary(commentary, latest, {});
      }
    }, LLM_ENHANCE_TIMEOUT);

    var payload = {
      commentary_hash: commHash,
      moves: movesList,
      boardSize: size,
      level: level,
      commentary: commentary,
    };
    fetch(API_BASE + "/commentary-enhance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        clearTimeout(timeoutId);
        if (reqId !== llmEnhanceReqId) return;
        if (d && d.ok && d.polished) {
          // 用润色版替换当前解说（保留候选点结构）
          var latest = state.lastAnalysis;
          if (!latest) return;
          // 合并：用润色的文字 + 原 commentary 的额外字段（如 to_move_view）
          for (var k in commentary) {
            if (!(k in d.polished)) d.polished[k] = commentary[k];
          }
          renderCommentary(d.polished, latest, { polished: true });
        } else {
          // 润色失败，移除"正在润色"提示，保留规则解说
          var latest = state.lastAnalysis;
          if (latest) {
            renderCommentary(commentary, latest, {});
          }
        }
      })
      .catch(function () {
        clearTimeout(timeoutId);
        if (reqId !== llmEnhanceReqId) return;
        var latest = state.lastAnalysis;
        if (latest) {
          renderCommentary(commentary, latest, {});
        }
      });
  }

  // ---- 候选点列表点击：调用后端深度推演，展示 10 步 pv（不改变棋局状态） ----
  var lookaheadReqId = 0;

  function showLookahead(rank, mark) {
    // 没开分析模式不允许推演（候选点列表在非分析模式时其实也不可见，多一层保险）
    if (!state.analyzeMode) return;
    Sfx.candidateClick();
    var candEls = dom.candidates.querySelectorAll(".candidate");
    var togglingOff = candEls[rank] && candEls[rank].classList.contains("selected");
    for (var i = 0; i < candEls.length; i++) {
      candEls[i].classList.toggle("selected", i === rank && !togglingOff);
    }
    if (togglingOff) {
      board.clearLookaheadMarks();
      clearTrend();
      return;
    }

    var col = "ABCDEFGHJKLMNOPQRST";
    var candidateGtp = mark.move || (col[mark.x] + (state.boardSize - mark.y));
    var moveCount = board.history.length;
    var firstColor = moveCount % 2 === 0 ? 1 : 2;

    // 优先用后台预推演缓存（如果命中 = 瞬间弹出，零等待）
    var cached = state.prefetchedLookahead[candidateGtp.toUpperCase()];
    if (cached && cached.ready && cached.pv && cached.pv.length > 0) {
      _renderLookaheadMarks(cached.pv, firstColor);
      flash("推演（缓存）：" + candidateGtp + " 起手，共 " + cached.pv.length + " 步");
      return;
    }

    // 没命中缓存：先显示普通分析里的短 pv（几秒钟也行），再请求深度推演
    var infos = state.lastAnalysis && state.lastAnalysis.moveInfos
      ? state.lastAnalysis.moveInfos : [];
    var info = infos[rank];
    var shortPv = (info && info.pv) ? info.pv.slice(0, 10) : [];
    if (shortPv.length > 0) {
      _renderLookaheadMarks(shortPv, firstColor);
    }

    var reqId = ++lookaheadReqId;
    showLoading(true);

    var payload = {
      moves: board.getMoves(),
      boardSize: state.boardSize,
      komi: state.komi,
      rules: state.rules,
      level: state.aiLevel,
      candidate: candidateGtp,
    };

    fetch(API_BASE + "/lookahead", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (reqId !== lookaheadReqId) return; // 已被新请求取代
        showLoading(false);
        if (data.ok && data.pv && data.pv.length > 0) {
          _renderLookaheadMarks(data.pv, firstColor);
          flash("推演：" + candidateGtp + " 起手，共 " + data.pv.length + " 步");
          // 顺便存入缓存，避免下次再等
          state.prefetchedLookahead[candidateGtp.toUpperCase()] = { pv: data.pv.slice(), ready: true };
        } else {
          flash("推演失败: " + (data.error || "无数据"));
        }
      })
      .catch(function (err) {
        if (reqId !== lookaheadReqId) return;
        showLoading(false);
        flash("推演请求失败: " + err.message);
      });
  }

  // 将 pv 字符串数组转为棋盘推演标记
  function _renderLookaheadMarks(pv, firstColor) {
    var marks = [];
    for (var i = 0; i < pv.length; i++) {
      var mv = pv[i];
      if (!mv || mv === "pass") continue;
      var xy = gtpToXY(mv, state.boardSize);
      if (xy.x < 0) continue;
      marks.push({
        x: xy.x,
        y: xy.y,
        number: i + 1,
        color: (i % 2 === 0) ? firstColor : (firstColor === 1 ? 2 : 1),
      });
    }
    board.setLookaheadMarks(marks);
  }

  // ---- 推演胜率趋势折线（已移除，保留 clearTrend 为 no-op 避免报错） ----
  var trendReqId = 0;
  function clearTrend() { /* 已移除推演趋势面板，no-op */ }
  function _trendRootWr() { return 0.5; }

  // ---- 复盘模式 ----
  function toggleReview() {
    // 如果在试下模式，先退出试下
    if (trialReviewIndex >= 0) {
      exitTrialMode();
    }
    if (state.reviewMode) {
      exitReview();
    } else {
      // 进入复盘模式前，先退出分析模式（避免互相干扰）
      if (state.analyzeMode) {
        toggleAnalyzeMode();
      }
      enterReview();
    }
  }

  function enterReview() {
    if (!board || board.history.length === 0) {
      flash("没有可复盘的棋谱");
      return;
    }
    state.reviewMode = true;
    state.reviewIndex = board.history.length - 1;
    board.setReviewMode(true);
    board.clearStoneBorders();
    board.clearLookaheadMarks();
    clearTrend();
    if (dom.reviewBar) dom.reviewBar.style.display = "flex";
    if (dom.btnReview) dom.btnReview.textContent = "退出复盘";
    reviewGoto(state.reviewIndex);
  }

  function exitReview() {
    // 如果处于试下模式，先清理
    if (trialReviewIndex >= 0) {
      trialReviewIndex = -1;
      if (dom.trialBtn) dom.trialBtn.style.display = "inline-block";
      if (dom.trialReturnBtn) dom.trialReturnBtn.style.display = "none";
      if (dom.btnFirst) dom.btnFirst.disabled = false;
      if (dom.btnPrev) dom.btnPrev.disabled = false;
      if (dom.btnNext) dom.btnNext.disabled = false;
      if (dom.btnLast) dom.btnLast.disabled = false;
    }
    state.reviewMode = false;
    state.reviewIndex = -1;
    board.setReviewMode(false);
    board.clearLookaheadMarks();
    clearTrend();
    // 恢复到最终局面
    board.gotoMove(board.history.length - 1);
    if (dom.reviewBar) dom.reviewBar.style.display = "none";
    if (dom.btnReview) dom.btnReview.textContent = "复盘";
    // 退出复盘重新画全局曲线（保持）
    drawGlobalTrend();
    // 重新分析最终局面
    if (board.history.length > 0) analyze(true);
  }

  // ---- 复盘试下模式：从当前复盘位置试着落一子 ----
  function enterTrialMode() {
    if (!state.reviewMode || !board || trialReviewIndex >= 0) return;
    // 保存当前复盘位置索引
    trialReviewIndex = state.reviewIndex;
    // 退出复盘模式（棋盘可点击）
    state.reviewMode = false;
    board.setReviewMode(false);
    board.clearLookaheadMarks();
    clearTrend();
    // 保持棋盘在当前位置（不改动历史）
    board.gotoMove(trialReviewIndex);
    // 更新 UI：显示"返回"按钮，隐藏"试下"
    if (dom.trialBtn) dom.trialBtn.style.display = "none";
    if (dom.trialReturnBtn) dom.trialReturnBtn.style.display = "inline-block";
    // 禁用复盘导航按钮
    if (dom.btnFirst) dom.btnFirst.disabled = true;
    if (dom.btnPrev) dom.btnPrev.disabled = true;
    if (dom.btnNext) dom.btnNext.disabled = true;
    if (dom.btnLast) dom.btnLast.disabled = true;
    // 进入分析模式（如果还没开），确保试下后能看到五点优选和形势判断
    if (!state.analyzeMode) {
      // 直接 toggle 会触发 exitReview，所以手动设置
      state.analyzeMode = true;
      document.body.classList.add("analyze-mode");
      if (dom.btnAnalyze) dom.btnAnalyze.classList.add("analyze-active");
    }
    // 分析当前局面
    analyze(true, null, trialReviewIndex);
    flash("请点击棋盘落子试下");
  }

  function exitTrialMode() {
    if (trialReviewIndex < 0 || !board) return;
    // 撤销所有试下过程中下的棋
    while (board.history.length > trialReviewIndex + 1) {
      board.undo();
    }
    // 清除分析痕迹
    board.setCandidateMarks([]);
    board.clearLookaheadMarks();
    board.clearOwnerData();
    clearTrend();
    // 重新进入复盘模式
    trialReviewIndex = -1;
    state.reviewMode = true;
    board.setReviewMode(true);
    // 回到复盘位置
    reviewGoto(state.reviewIndex);
    // 恢复 UI
    if (dom.trialBtn) dom.trialBtn.style.display = "inline-block";
    if (dom.trialReturnBtn) dom.trialReturnBtn.style.display = "none";
    if (dom.btnFirst) dom.btnFirst.disabled = false;
    if (dom.btnPrev) dom.btnPrev.disabled = false;
    if (dom.btnNext) dom.btnNext.disabled = false;
    if (dom.btnLast) dom.btnLast.disabled = false;
  }

  // 获取指定手数之前的落子列表（供复盘分析使用，只发到当前复盘位置）
  function _getMovesUpTo(index) {
    if (!board || index < 0) return [];
    return board.history.slice(0, index + 1).map(function (h) {
      return { x: h.x, y: h.y, color: h.color };
    });
  }

  function reviewGoto(index) {
    if (!board || !state.reviewMode) return;
    Sfx.review();
    index = Math.max(-1, Math.min(index, board.history.length - 1));
    state.reviewIndex = index;
    board.gotoMove(index);
    updateReviewUI();
    updateMovesList();
    // 自动分析当前复盘位置（仅发送到当前手，解决复盘时形势判断不更新的 BUG）
    if (index >= 0) {
      analyze(true, null, index);
    } else {
      // 空盘位置：清除候选点、解说、势力图
      board.setCandidateMarks([]);
      board.clearLookaheadMarks();
      board.clearOwnerData();
      dom.candidates.innerHTML = '<p class="hint">落子后显示 AI 推荐选点</p>';
      dom.commentary.innerHTML = '<p class="hint">空盘</p>';
    }
  }

  function updateReviewUI() {
    var total = board ? board.history.length : 0;
    var idx = state.reviewIndex;
    if (dom.reviewBar) {
      var label = dom.reviewBar.querySelector(".review-pos");
      if (label) label.textContent = (idx + 1) + " / " + total;
    }
    if (dom.btnFirst) dom.btnFirst.disabled = (idx <= -1);
    if (dom.btnPrev) dom.btnPrev.disabled = (idx <= -1);
    if (dom.btnNext) dom.btnNext.disabled = (idx >= total - 1);
    if (dom.btnLast) dom.btnLast.disabled = (idx >= total - 1);
  }

  // ---- 其他 ----
  function checkHealth() {
    fetch(API_BASE + "/health")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // 应用版本号（顶部标题旁小字）
        if (dom.appVersion && data.app && data.app.version) {
          dom.appVersion.textContent = "v" + data.app.version;
        }
        // 引擎状态徽章（根据 engine_lifecycle 区分运行/休眠/Mock）
        var lc = data.engine_lifecycle || {};
        if (data.engine && data.engine.mode === "mock") {
          dom.engineBadge.textContent = "Mock 模式";
          dom.engineBadge.className = "badge mock";
        } else if (lc.state === "asleep") {
          dom.engineBadge.textContent = "KataGo 休眠";
          dom.engineBadge.className = "badge ready";
          dom.engineBadge.title = "首次操作自动拉起（" + (lc.idle_timeout_sec ? (lc.idle_timeout_sec / 60) + " 分钟空闲自动休眠" : "按需启动") + "）";
        } else if (lc.state === "running") {
          var remain = lc.remain_sec != null ? lc.remain_sec : 0;
          var remainText = remain > 60 ? Math.floor(remain / 60) + " 分" : remain + " 秒";
          dom.engineBadge.textContent = "KataGo GPU · 余 " + remainText;
          dom.engineBadge.className = "badge ready";
          dom.engineBadge.title = "PID " + lc.pid + " · 空闲 " + (lc.idle_sec != null ? lc.idle_sec : 0) + " 秒";
        } else {
          dom.engineBadge.textContent = "KataGo GPU";
          dom.engineBadge.className = "badge ready";
        }
        // KataGo 版本信息（新徽章）
        if (dom.katagoVersion) {
          var kt = data.katago || {};
          if (kt.version) {
            var label = "KataGo v" + kt.version;
            if (kt.backend) label += " · " + kt.backend;
            if (kt.cuda_build) label += " · CUDA " + kt.cuda_build;
            dom.katagoVersion.textContent = label;
            dom.katagoVersion.className = "badge engine-ver on";
          } else if (data.engine && data.engine.backend) {
            // mock 或不可用时退回 engine.backend 描述
            dom.katagoVersion.textContent = data.engine.backend || "无可用引擎";
            dom.katagoVersion.className = "badge engine-ver";
          } else {
            dom.katagoVersion.textContent = "-";
            dom.katagoVersion.className = "badge engine-ver";
          }
        }
      })
      .catch(function () {
        // 单次健康检查失败不立即标记"离线"，避免刷新慢或公网网络抖动导致误导。
        // 只有徽章当前仍是初始"检测中"时才改成"重连中…"，否则保留最后一次真实状态。
        var prevBadge = dom.engineBadge ? dom.engineBadge.textContent : "";
        if (!prevBadge || /检测中/.test(prevBadge)) {
          dom.engineBadge.textContent = "重连中…";
          dom.engineBadge.className = "badge offline";
        }
        if (dom.katagoVersion) {
          // 只在原本还没有版本信息时才显示"重连中"，不要覆盖已有版本
          var prevVer = dom.katagoVersion.textContent || "";
          if (!prevVer || prevVer === "-" || /连接失败|检测中/.test(prevVer)) {
            dom.katagoVersion.textContent = "重连中…";
            dom.katagoVersion.className = "badge engine-ver";
          }
        }
      });
  }

  function showLoading(show) {
    dom.loading.classList.toggle("show", show);
  }

  function flash(msg) {
    var el = document.createElement("div");
    el.className = "flash";
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.classList.add("show"); }, 10);
    setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () { document.body.removeChild(el); }, 300);
    }, 2000);
  }

  function gtpToXY(move, size) {
    if (!move || move === "pass") return { x: -1, y: -1 };
    var cols = "ABCDEFGHJKLMNOPQRST";
    var x = cols.indexOf(move[0].toUpperCase());
    var y = size - parseInt(move.slice(1), 10);
    return { x: x, y: y };
  }

  /* ========== 计时器 ========== */
  function fmtTime(sec) {
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  function timerStart(side) {
    state.timerSide = side;
    state.timerActive = true;
    state.timerStartTs = Date.now();
    if (state.timerInterval) clearInterval(state.timerInterval);
    state.timerInterval = setInterval(timerTick, 1000);
    timerUpdateActive();
  }

  function timerStop() {
    if (!state.timerActive) return;
    // 把已过时间累加到当前方
    var elapsed = Math.floor((Date.now() - state.timerStartTs) / 1000);
    if (state.timerSide === 1) state.timerBlack += elapsed;
    else state.timerWhite += elapsed;
    state.timerActive = false;
    if (state.timerInterval) { clearInterval(state.timerInterval); state.timerInterval = null; }
    timerUpdateDisplay();
    timerUpdateActive();
  }

  function timerSwitch(justMovedColor) {
    // justMovedColor 刚落子的一方 -> 计时切到对方
    timerStop();
    var next = justMovedColor === 1 ? 2 : 1;
    timerStart(next);
  }

  function timerReset() {
    timerStop();
    state.timerBlack = 0;
    state.timerWhite = 0;
    timerUpdateDisplay();
  }

  function timerTick() {
    // 不累加到 state，只在显示上加实时增量
    timerUpdateDisplay();
  }

  function timerUpdateDisplay() {
    var bSec = state.timerBlack;
    var wSec = state.timerWhite;
    if (state.timerActive) {
      var live = Math.floor((Date.now() - state.timerStartTs) / 1000);
      if (state.timerSide === 1) bSec += live;
      else wSec += live;
    }
    if (dom.timerBlack) dom.timerBlack.textContent = fmtTime(bSec);
    if (dom.timerWhite) dom.timerWhite.textContent = fmtTime(wSec);
  }

  function timerUpdateActive() {
    if (dom.timerBlack) dom.timerBlack.classList.toggle("active", state.timerActive && state.timerSide === 1);
    if (dom.timerWhite) dom.timerWhite.classList.toggle("active", state.timerActive && state.timerSide === 2);
  }

  /* ========== 棋谱记录面板 ========== */
  function updateMovesList() {
    if (!dom.movesList || !board) return;
    var history = board.history;
    if (history.length === 0) {
      dom.movesList.innerHTML = '<p class="hint">尚未落子</p>';
      return;
    }
    var cols = "ABCDEFGHJKLMNOPQRST";
    var html = "";
    for (var i = 0; i < history.length; i++) {
      var h = history[i];
      var colorCls = h.color === 1 ? "b" : "w";
      var coord = cols[h.x] + (state.boardSize - h.y);
      var isCurrent = (state.reviewMode && i === state.reviewIndex) ||
                      (!state.reviewMode && i === history.length - 1);
      html += '<span class="move-entry' + (isCurrent ? " current" : "") + '" data-idx="' + i + '">' +
        '<span class="move-num">' + (i + 1) + '</span>' +
        '<span class="stone-dot ' + colorCls + '"></span>' +
        '<span class="move-coord">' + coord + '</span>' +
        '</span>';
    }
    dom.movesList.innerHTML = html;
    // 点击跳转
    var entries = dom.movesList.querySelectorAll(".move-entry");
    for (var j = 0; j < entries.length; j++) {
      entries[j].addEventListener("click", function (idx) {
        return function () {
          if (!state.reviewMode) enterReview();
          reviewGoto(idx);
        };
      }(parseInt(entries[j].dataset.idx, 10)));
    }
    // 自动滚到最新
    dom.movesList.scrollTop = dom.movesList.scrollHeight;
  }

  /* ========== 终局数子 ========== */
  function doScore() {
    if (!board || board.history.length === 0) {
      flash("没有棋谱可数子");
      return;
    }
    timerStop();
    showLoading(true);
    var payload = {
      moves: board.getMoves(),
      boardSize: state.boardSize,
      komi: state.komi,
      rules: state.rules,
    };
    fetch(API_BASE + "/score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        showLoading(false);
        if (data.ok) {
          state.lastScoreResult = data;
          renderScoreModal(data);
        } else {
          flash("数子失败: " + (data.error || "未知错误"));
        }
      })
      .catch(function (err) {
        showLoading(false);
        flash("数子请求失败: " + err.message);
      });
  }

  function renderScoreModal(data) {
    var winnerText = data.winner === "B" ? "黑胜" : "白胜";
    var diffText = data.winner === "B"
      ? "黑 +" + data.diff
      : "白 +" + Math.abs(data.diff);
    var html = '<div class="score-result ' + (data.winner === "B" ? "win-b" : "win-w") + '">' +
      winnerText + ' ' + diffText + '</div>';
    html += '<div class="score-detail">';
    html += '<div class="sd-block"><div class="sd-label">黑方</div><div class="sd-val b">' + data.black + '</div></div>';
    html += '<div class="sd-block"><div class="sd-label">白方</div><div class="sd-val w">' + data.white + '</div></div>';
    html += '</div>';
    html += '<div class="score-komi">含贴目 ' + data.komi + ' 目 · ' +
      (data.mode === "mock" ? "估算（Mock）" : "KataGo 估算") + '</div>';
    if (data.elapsed_ms) {
      html += '<div class="score-komi">耗时 ' + data.elapsed_ms + ' ms</div>';
    }
    dom.scoreBody.innerHTML = html;
    dom.scoreModal.style.display = "flex";
  }

  /* ========== 保存棋谱 ========== */
  function saveCurrentGame() {
    if (!board || board.history.length === 0) {
      flash("没有棋谱可保存");
      return;
    }
    var scoreData = state.lastScoreResult || {};
    var resultStr = "";
    if (scoreData.winner) {
      resultStr = scoreData.winner === "B"
        ? "黑+" + scoreData.diff
        : "白+" + Math.abs(scoreData.diff);
    }
    var payload = {
      moves: board.getMoves(),
      boardSize: state.boardSize,
      komi: state.komi,
      rules: state.rules,
      mode: state.mode,
      level: state.aiLevel,
      result: resultStr,
      blackScore: scoreData.black || null,
      whiteScore: scoreData.white || null,
      winner: scoreData.winner || null,
      metadata: {
        blackTime: state.timerBlack,
        whiteTime: state.timerWhite,
      },
    };
    fetch(API_BASE + "/games", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          flash("棋谱已保存（ID: " + data.id + "）");
          dom.scoreModal.style.display = "none";
        } else {
          flash("保存失败: " + (data.error || "未知错误"));
        }
      })
      .catch(function (err) {
        flash("保存请求失败: " + err.message);
      });
  }

  /* ========== 历史棋局 ========== */
  function showHistory() {
    dom.historyModal.style.display = "flex";
    dom.historyList.innerHTML = '<p class="hint">加载中...</p>';
    fetch(API_BASE + "/games?limit=50")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok || !data.games || data.games.length === 0) {
          dom.historyList.innerHTML = '<p class="hint">暂无历史棋局</p>';
          return;
        }
        var html = "";
        for (var i = 0; i < data.games.length; i++) {
          var g = data.games[i];
          var d = new Date(g.created_at * 1000);
          var dateStr = (d.getMonth() + 1) + "/" + d.getDate() + " " +
            (d.getHours() < 10 ? "0" : "") + d.getHours() + ":" +
            (d.getMinutes() < 10 ? "0" : "") + d.getMinutes();
          var resultCls = g.winner === "B" ? "b-win" : "w-win";
          var resultText = g.result || "-";
          html += '<div class="history-item" data-id="' + g.id + '">' +
            '<span class="hi-id">#' + g.id + '</span>' +
            '<span class="hi-date">' + dateStr + '</span>' +
            '<span class="hi-size">' + g.board_size + '路</span>' +
            '<span class="hi-result ' + resultCls + '">' + resultText + '</span>' +
            '<span class="hi-moves">' + (g.moves_count || "") + '</span>' +
            '<span class="hi-del" data-del="' + g.id + '">删</span>' +
            '</div>';
        }
        dom.historyList.innerHTML = html;
        // 绑定点击加载
        var items = dom.historyList.querySelectorAll(".history-item");
        for (var j = 0; j < items.length; j++) {
          items[j].addEventListener("click", function (gid) {
            return function (e) {
              if (e.target.classList.contains("hi-del")) return;
              loadGame(gid);
            };
          }(parseInt(items[j].dataset.id, 10)));
        }
        // 绑定删除
        var dels = dom.historyList.querySelectorAll(".hi-del");
        for (var k = 0; k < dels.length; k++) {
          dels[k].addEventListener("click", function (e) {
            e.stopPropagation();
            var gid = parseInt(e.target.dataset.del, 10);
            if (!confirm("确认删除棋局 #" + gid + "?")) return;
            fetch(API_BASE + "/games/" + gid, { method: "DELETE" })
              .then(function (r) { return r.json(); })
              .then(function () { showHistory(); });
          });
        }
      })
      .catch(function (err) {
        dom.historyList.innerHTML = '<p class="hint">加载失败: ' + err.message + '</p>';
      });
  }

  function loadGame(gid) {
    fetch(API_BASE + "/games/" + gid)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok || !data.game) {
          flash("加载棋谱失败");
          return;
        }
        var g = data.game;
        dom.historyModal.style.display = "none";
        // 设置棋盘参数
        state.boardSize = g.board_size;
        state.komi = g.komi;
        state.rules = g.rules;
        dom.selSize.value = g.board_size;
        // 清空棋盘
        if (board) {
          board.setReviewMode(false);
          board.clear();
          if (board.size !== g.board_size) board.resize(g.board_size);
        }
        // 逐手回放（临时禁用 onMove 回调，避免触发 analyze/AI 逻辑导致颜色错乱）
        var savedOnMove = board.onMove;
        board.onMove = null;
        var moves = g.moves || [];
        for (var i = 0; i < moves.length; i++) {
          var m = moves[i];
          board.placeExternal(m.x, m.y, m.color);
        }
        // 恢复 onMove 回调
        board.onMove = savedOnMove;
        updateStatus();
        updateMovesList();
        // 进入复盘模式
        if (moves.length > 0) enterReview();
        flash("已加载棋局 #" + gid + "（" + moves.length + "手）");
      })
      .catch(function (err) {
        flash("加载失败: " + err.message);
      });
  }

  document.addEventListener("DOMContentLoaded", init);
})();

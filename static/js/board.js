/* board.js — 自包含围棋棋盘（Canvas 渲染 + 规则判定）
 * 功能：渲染棋盘/星位/坐标/棋子，处理点击落子，判定提子/自杀/打劫。
 * 不依赖外部库，适配 9/13/19 路。
 */
(function (global) {
  "use strict";

  var GTP_COLUMNS = "ABCDEFGHJKLMNOPQRST";

  function GoBoard(canvas, options) {
    options = options || {};
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.size = options.size || 19;
    this.onMove = options.onMove || null; // 回调(color, x, y, captured)
    this.onIllegal = options.onIllegal || null;

    // 棋盘状态：0=空, 1=黑, 2=白
    this.board = [];
    this.history = []; // 落子历史 {x,y,color,captured}
    this.lastMove = null;
    this.previousBoardHash = null; // 用于打劫判定
    this.candidateMarks = []; // 候选点标记 [{x,y,order,winrate}]
    this.stoneBorders = {};   // 棋子边框着色 "x,y" -> color (手质量评价)
    this.lastBorderBlack = null; // {key:"x,y"}  最近一手黑棋的质量圈
    this.lastBorderWhite = null; // {key:"x,y"}  最近一手白棋的质量圈
    this.lookaheadMarks = []; // 推演标记 [{x,y,number,color}]
    this.ownerData = null;    // 势力分布数据（扁平数组, row-major, 0=黑势~1=白势）
    this.hoverXY = null;
    this.reviewMode = false;  // 复盘模式：禁止落子

    this._initBoard();
    this._bindEvents();
    this.render();
  }

  GoBoard.prototype._initBoard = function () {
    this.board = [];
    for (var i = 0; i < this.size; i++) {
      this.board.push(new Array(this.size).fill(0));
    }
  };

  // 计算单元格像素尺寸
  GoBoard.prototype._metrics = function () {
    var w = this.canvas.width;
    var h = this.canvas.height;
    // 小棋盘需要更大 padding 防止棋子超出画布
    var padding;
    if (this.size <= 9) {
      padding = Math.round(w * 0.07);
    } else if (this.size <= 13) {
      padding = Math.round(w * 0.05);
    } else {
      padding = Math.round(w * 0.04);
    }
    var usable = w - padding * 2;
    var cell = usable / (this.size - 1);
    return { w: w, h: h, padding: padding, cell: cell };
  };

  // 棋盘坐标 -> 画布像素
  GoBoard.prototype._toPixel = function (x, y) {
    var m = this._metrics();
    return { px: m.padding + x * m.cell, py: m.padding + y * m.cell };
  };

  // 画布像素 -> 棋盘坐标
  GoBoard.prototype._toBoard = function (px, py) {
    var m = this._metrics();
    var x = Math.round((px - m.padding) / m.cell);
    var y = Math.round((py - m.padding) / m.cell);
    if (x < 0 || x >= this.size || y < 0 || y >= this.size) return null;
    // 容差判断：离交叉点太远不算
    var p = this._toPixel(x, y);
    if (Math.abs(px - p.px) > m.cell * 0.45 || Math.abs(py - p.py) > m.cell * 0.45) return null;
    return { x: x, y: y };
  };

  GoBoard.prototype._bindEvents = function () {
    var self = this;
    this.canvas.addEventListener("click", function (e) {
      var rect = self.canvas.getBoundingClientRect();
      var scaleX = self.canvas.width / rect.width;
      var scaleY = self.canvas.height / rect.height;
      var pos = self._toBoard((e.clientX - rect.left) * scaleX, (e.clientY - rect.top) * scaleY);
      if (!pos) return;
      // 棋盘点击：始终正常落子（候选点仅作视觉参考，不拦截点击）
      self._tryPlace(pos.x, pos.y);
    });
    this.canvas.addEventListener("mousemove", function (e) {
      var rect = self.canvas.getBoundingClientRect();
      var scaleX = self.canvas.width / rect.width;
      var scaleY = self.canvas.height / rect.height;
      var pos = self._toBoard((e.clientX - rect.left) * scaleX, (e.clientY - rect.top) * scaleY);
      self.hoverXY = pos;
      self.render();
    });
    this.canvas.addEventListener("mouseleave", function () {
      self.hoverXY = null;
      self.render();
    });
  };

  // 落子尝试（含规则判定）
  GoBoard.prototype._tryPlace = function (x, y) {
    if (this.reviewMode) return false; // 复盘模式禁止落子
    if (this.board[x][y] !== 0) {
      if (typeof window.Sfx !== "undefined") window.Sfx.illegal();
      if (this.onIllegal) this.onIllegal("此处已有棋子");
      return false;
    }
    var color = this.history.length % 2 === 0 ? 1 : 2; // 黑先
    var opp = color === 1 ? 2 : 1;

    // 临时落子
    this.board[x][y] = color;

    // 检查并提取对方无气的棋串
    var captured = this._captureGroups(x, y, opp);

    // 检查己方是否自杀（无气且未提子）
    if (captured.length === 0) {
      var myGroup = this._getGroup(x, y);
      if (this._liberties(myGroup) === 0) {
        this.board[x][y] = 0; // 还原
        if (this.onIllegal) this.onIllegal("禁着点（自杀）");
        return false;
      }
    }

    // 打劫判定：禁止重现上一手前的局面
    var newHash = this._boardHash();
    if (this.previousBoardHash !== null && newHash === this.previousBoardHash) {
      // 还原这步
      this._restoreCaptured(captured, opp);
      this.board[x][y] = 0;
      if (this.onIllegal) this.onIllegal("打劫禁着（不可立即提回）");
      return false;
    }

    // 记录历史
    this.history.push({ x: x, y: y, color: color, captured: captured });
    this.previousBoardHash = this._boardHashBefore(x, y, captured);
    this.lastMove = { x: x, y: y, color: color };
    this.candidateMarks = [];
    this.lookaheadMarks = [];
    this.ownerData = null; // 落子后清除旧势力数据，避免分析延迟时显示错位
    this.render();
    // 音效：落子（有提子时用提子音）
    if (typeof window.Sfx !== "undefined") {
      if (captured.length > 0) window.Sfx.capture();
      else window.Sfx.place();
    }
    if (this.onMove) this.onMove(color, x, y, captured);
    return true;
  };

  // 外部直接落子（AI 走棋用，跳过规则判定但走同一套记录）
  GoBoard.prototype.placeExternal = function (x, y, color) {
    if (x < 0 || y < 0) return false;
    if (this.reviewMode) return false; // 复盘模式禁止落子
    if (this.board[x][y] !== 0) return false;
    var opp = color === 1 ? 2 : 1;
    this.board[x][y] = color;
    var captured = this._captureGroups(x, y, opp);
    this.history.push({ x: x, y: y, color: color, captured: captured });
    this.previousBoardHash = this._boardHash();
    this.lastMove = { x: x, y: y, color: color };
    this.candidateMarks = [];
    this.lookaheadMarks = [];
    this.ownerData = null; // 落子后清除旧势力数据，避免分析延迟时显示错位
    this.render();
    if (this.onMove) this.onMove(color, x, y, captured);
    return true;
  };

  // 获取连通棋串
  GoBoard.prototype._getGroup = function (x, y) {
    var color = this.board[x][y];
    if (color === 0) return [];
    var visited = {};
    var stack = [[x, y]];
    var group = [];
    while (stack.length) {
      var p = stack.pop();
      var key = p[0] + "," + p[1];
      if (visited[key]) continue;
      visited[key] = true;
      if (this.board[p[0]][p[1]] !== color) continue;
      group.push([p[0], p[1]]);
      var nbrs = this._neighbors(p[0], p[1]);
      for (var i = 0; i < nbrs.length; i++) {
        stack.push(nbrs[i]);
      }
    }
    return group;
  };

  // 计算棋串的气
  GoBoard.prototype._liberties = function (group) {
    var libs = {};
    for (var i = 0; i < group.length; i++) {
      var nbrs = this._neighbors(group[i][0], group[i][1]);
      for (var j = 0; j < nbrs.length; j++) {
        var nx = nbrs[j][0], ny = nbrs[j][1];
        if (this.board[nx][ny] === 0) libs[nx + "," + ny] = true;
      }
    }
    return Object.keys(libs).length;
  };

  // 提取指定颜色的无气棋串（落子后调用）
  GoBoard.prototype._captureGroups = function (x, y, oppColor) {
    var captured = [];
    var checked = {};
    var nbrs = this._neighbors(x, y);
    for (var i = 0; i < nbrs.length; i++) {
      var nx = nbrs[i][0], ny = nbrs[i][1];
      var key = nx + "," + ny;
      if (checked[key] || this.board[nx][ny] !== oppColor) continue;
      var group = this._getGroup(nx, ny);
      for (var j = 0; j < group.length; j++) checked[group[j][0] + "," + group[j][1]] = true;
      if (this._liberties(group) === 0) {
        for (var k = 0; k < group.length; k++) {
          this.board[group[k][0]][group[k][1]] = 0;
          captured.push([group[k][0], group[k][1]]);
        }
      }
    }
    return captured;
  };

  GoBoard.prototype._restoreCaptured = function (captured, color) {
    for (var i = 0; i < captured.length; i++) {
      this.board[captured[i][0]][captured[i][1]] = color;
    }
  };

  GoBoard.prototype._neighbors = function (x, y) {
    var r = [];
    if (x > 0) r.push([x - 1, y]);
    if (x < this.size - 1) r.push([x + 1, y]);
    if (y > 0) r.push([x, y - 1]);
    if (y < this.size - 1) r.push([x, y + 1]);
    return r;
  };

  // 棋盘状态哈希（用于打劫）
  GoBoard.prototype._boardHash = function () {
    var s = "";
    for (var i = 0; i < this.size; i++) s += this.board[i].join("");
    return s;
  };

  // 计算这步落子前的局面哈希（用于下一手的打劫判定）
  GoBoard.prototype._boardHashBefore = function (x, y, captured) {
    // 临时还原
    var color = this.board[x][y];
    this.board[x][y] = 0;
    var opp = color === 1 ? 2 : 1;
    this._restoreCaptured(captured, opp);
    var h = this._boardHash();
    // 恢复
    this.board[x][y] = color;
    this._captureGroups(x, y, opp);
    return h;
  };

  // ---------------- 渲染 ----------------
  GoBoard.prototype.render = function () {
    var ctx = this.ctx;
    var m = this._metrics();
    // 木色背景
    ctx.fillStyle = "#e8b96b";
    ctx.fillRect(0, 0, m.w, m.h);
    // 木纹渐变
    var grad = ctx.createLinearGradient(0, 0, m.w, m.h);
    grad.addColorStop(0, "rgba(180,130,60,0.15)");
    grad.addColorStop(1, "rgba(220,170,90,0.1)");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, m.w, m.h);

    // 网格线
    ctx.strokeStyle = "#2a1a08";
    ctx.lineWidth = 1;
    for (var i = 0; i < this.size; i++) {
      var p1 = this._toPixel(0, i);
      var p2 = this._toPixel(this.size - 1, i);
      ctx.beginPath();
      ctx.moveTo(p1.px, p1.py);
      ctx.lineTo(p2.px, p2.py);
      ctx.stroke();
      var p3 = this._toPixel(i, 0);
      var p4 = this._toPixel(i, this.size - 1);
      ctx.beginPath();
      ctx.moveTo(p3.px, p3.py);
      ctx.lineTo(p4.px, p4.py);
      ctx.stroke();
    }
    // 外框加粗
    ctx.lineWidth = 2;
    var tl = this._toPixel(0, 0);
    var br = this._toPixel(this.size - 1, this.size - 1);
    ctx.strokeRect(tl.px, tl.py, br.px - tl.px, br.py - tl.py);

    // 星位
    var stars = this._starPoints();
    ctx.fillStyle = "#2a1a08";
    for (var s = 0; s < stars.length; s++) {
      var sp = this._toPixel(stars[s][0], stars[s][1]);
      ctx.beginPath();
      ctx.arc(sp.px, sp.py, Math.max(2, m.cell * 0.08), 0, Math.PI * 2);
      ctx.fill();
    }

    // 势力分布图（正方形，最大边长=cell*0.4，即半边长=cell*0.2）
    if (this.ownerData && this.ownerData.length === this.size * this.size) {
      var maxR = m.cell * 0.20; // 最大方形半边长 = cell*0.2（边长=2/5格，原尺寸×2）
      var minR = m.cell * 0.06; // 最小可见尺寸（同步翻倍）
      for (var oy = 0; oy < this.size; oy++) {
        for (var ox = 0; ox < this.size; ox++) {
          // 有棋子的点不显示势力
          if (this.board[ox][oy] !== 0) continue;
          var val = this.ownerData[oy * this.size + ox];
          // KataGo ownership (reportAnalysisWinratesAs=BLACK): 正值=黑控制, 负值=白控制
          var strength = Math.abs(val); // 0~1
          if (strength < 0.15) continue; // 中立区域不显示
          var half = minR + (maxR - minR) * strength;
          var op = this._toPixel(ox, oy);
          var sqX = op.px - half;
          var sqY = op.py - half;
          var sqSize = half * 2;
          // 黑势力=黑色描边 + 半透明黑填充；白势力=白色描边 + 半透明白填充
          // （木色背景上反差明显，避免白色方块上画黑边看起来像"黑方框"）
          if (val > 0) {
            ctx.fillStyle = "rgba(0,0,0,0.55)";
            ctx.fillRect(sqX, sqY, sqSize, sqSize);
            ctx.strokeStyle = "rgba(0,0,0,0.9)";
            ctx.lineWidth = 1;
            ctx.strokeRect(sqX, sqY, sqSize, sqSize);
          } else {
            ctx.fillStyle = "rgba(255,255,255,0.55)";
            ctx.fillRect(sqX, sqY, sqSize, sqSize);
            ctx.strokeStyle = "rgba(255,255,255,0.95)";
            ctx.lineWidth = 1;
            ctx.strokeRect(sqX, sqY, sqSize, sqSize);
          }
        }
      }
    }

    // 坐标标签
    ctx.fillStyle = "#5a3a18";
    ctx.font = Math.round(m.cell * 0.35) + "px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (var c = 0; c < this.size; c++) {
      var cx = this._toPixel(c, 0);
      ctx.fillText(GTP_COLUMNS[c], cx.px, m.padding * 0.4);
      var cx2 = this._toPixel(c, this.size - 1);
      ctx.fillText(GTP_COLUMNS[c], cx2.px, m.h - m.padding * 0.4);
    }
    for (var r = 0; r < this.size; r++) {
      var ry = this._toPixel(0, r);
      ctx.fillText(String(this.size - r), m.padding * 0.4, ry.py);
      var ry2 = this._toPixel(this.size - 1, r);
      ctx.fillText(String(this.size - r), m.w - m.padding * 0.4, ry2.py);
    }

    // 棋子（带边框着色）
    for (var xi = 0; xi < this.size; xi++) {
      for (var yi = 0; yi < this.size; yi++) {
        if (this.board[xi][yi] !== 0) {
          var bc = this.stoneBorders[xi + "," + yi] || null;
          this._drawStone(xi, yi, this.board[xi][yi], m.cell, bc);
        }
      }
    }

    // 最后一手标记
    if (this.lastMove) {
      var lp = this._toPixel(this.lastMove.x, this.lastMove.y);
      ctx.strokeStyle = this.lastMove.color === 1 ? "#fff" : "#000";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(lp.px, lp.py, m.cell * 0.18, 0, Math.PI * 2);
      ctx.stroke();
    }

    // 候选点标记 — 绿色渐变（深绿=最推荐 → 浅绿=一般推荐）
    var candColors = ["#006400", "#228B22", "#3CB371", "#66CDAA", "#90EE90"];
    for (var ci = 0; ci < this.candidateMarks.length; ci++) {
      var mk = this.candidateMarks[ci];
      var mp = this._toPixel(mk.x, mk.y);
      var cc = candColors[Math.min(ci, candColors.length - 1)];
      // 外圈半透明填充（放大一点，便于识别）
      ctx.fillStyle = cc;
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.arc(mp.px, mp.py, m.cell * 0.30, 0, Math.PI * 2);
      ctx.fill();
      // 内圈实心
      ctx.globalAlpha = 0.9;
      ctx.beginPath();
      ctx.arc(mp.px, mp.py, m.cell * 0.23, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
      // 序号：ABCDE 字母 — 纯黑粗字（去掉白色描边，保证清晰）
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      var numFontSize = Math.max(14, Math.round(m.cell * 0.34));
      var num = String.fromCharCode(65 + ci); // 0→A, 1→B, 2→C, 3→D, 4→E
      ctx.font = "700 " + numFontSize + "px 'Helvetica Neue', 'Inter', 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif";
      // 直接黑色实心字，加 0.5px 的外发光阴影（很弱的一圈保证浅色背景上也清晰）
      ctx.shadowColor = "rgba(255,255,255,0.5)";
      ctx.shadowBlur = Math.max(1, m.cell * 0.04);
      ctx.fillStyle = "#000";
      ctx.fillText(num, mp.px, mp.py);
      ctx.shadowBlur = 0;
    }

    // 推演标记 — 黑白数字标记未来步数（棋子+描边大字）
    for (var li = 0; li < this.lookaheadMarks.length; li++) {
      var lk = this.lookaheadMarks[li];
      var lp2 = this._toPixel(lk.x, lk.y);
      // 半透明棋子底（放大到接近正式棋子尺寸 0.46）
      ctx.globalAlpha = 0.72;
      var lkR = m.cell * 0.45;
      var lkGrad = ctx.createRadialGradient(
        lp2.px - lkR * 0.28, lp2.py - lkR * 0.28, lkR * 0.1,
        lp2.px, lp2.py, lkR);
      if (lk.color === 1) {
        lkGrad.addColorStop(0, "#555");
        lkGrad.addColorStop(1, "#000");
      } else {
        lkGrad.addColorStop(0, "#fff");
        lkGrad.addColorStop(1, "#c8c8c8");
      }
      ctx.fillStyle = lkGrad;
      ctx.beginPath();
      ctx.arc(lp2.px, lp2.py, lkR, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
      // 数字：细字体 Helvetica Neue Ultra-Light，字重 100
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      var lkNum = String(lk.number);
      var lkFontSize = Math.max(14, Math.round(m.cell * 0.38));
      ctx.font = "100 " + lkFontSize + "px 'Helvetica Neue UltraLight', 'HelveticaNeue-UltraLight', 'Inter Thin', 'Segoe UI Light', Arial, sans-serif";
      var isBlack = lk.color === 1;
      // 描边：黑子用白描边，白子用黑描边，2px 起（调细）
      var lkStrokeW = Math.max(2, Math.round(m.cell * 0.05));
      ctx.lineWidth = lkStrokeW;
      ctx.strokeStyle = isBlack ? "rgba(255,255,255,0.95)" : "rgba(0,0,0,0.85)";
      ctx.strokeText(lkNum, lp2.px, lp2.py);
      ctx.fillStyle = isBlack ? "#fff" : "#000";
      ctx.fillText(lkNum, lp2.px, lp2.py);
    }

    // 悬停预览（复盘模式下不显示）
    if (this.hoverXY && !this.reviewMode && this.board[this.hoverXY.x][this.hoverXY.y] === 0) {
      var hp = this._toPixel(this.hoverXY.x, this.hoverXY.y);
      ctx.globalAlpha = 0.4;
      this._drawStone(this.hoverXY.x, this.hoverXY.y,
        this.history.length % 2 === 0 ? 1 : 2, m.cell);
      ctx.globalAlpha = 1;
    }
  };

  GoBoard.prototype._drawStone = function (x, y, color, cell, borderColor) {
    var ctx = this.ctx;
    var p = this._toPixel(x, y);
    var r = cell * 0.46;
    // 阴影
    ctx.beginPath();
    ctx.arc(p.px + 1, p.py + 2, r, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(0,0,0,0.3)";
    ctx.fill();
    // 棋子本体
    var grad = ctx.createRadialGradient(p.px - r * 0.3, p.py - r * 0.3, r * 0.1, p.px, p.py, r);
    if (color === 1) {
      grad.addColorStop(0, "#555");
      grad.addColorStop(1, "#000");
    } else {
      grad.addColorStop(0, "#fff");
      grad.addColorStop(1, "#c8c8c8");
    }
    ctx.beginPath();
    ctx.arc(p.px, p.py, r, 0, Math.PI * 2);
    ctx.fillStyle = grad;
    ctx.fill();
    // 边框着色（手质量评价 / 推演高亮）
    if (borderColor) {
      ctx.strokeStyle = borderColor;
      ctx.lineWidth = Math.max(2, cell * 0.06);
      ctx.beginPath();
      ctx.arc(p.px, p.py, r, 0, Math.PI * 2);
      ctx.stroke();
    }
  };

  GoBoard.prototype._starPoints = function () {
    var n = this.size;
    if (n === 19) {
      return [[3, 3], [9, 3], [15, 3], [3, 9], [9, 9], [15, 9], [3, 15], [9, 15], [15, 15]];
    }
    if (n === 13) {
      return [[3, 3], [9, 3], [6, 6], [3, 9], [9, 9]];
    }
    if (n === 9) {
      return [[2, 2], [6, 2], [4, 4], [2, 6], [6, 6]];
    }
    return [];
  };

  // ---------------- 对外方法 ----------------
  GoBoard.prototype.getMoves = function () {
    return this.history.map(function (h) {
      return { x: h.x, y: h.y, color: h.color };
    });
  };

  GoBoard.prototype.setCandidateMarks = function (marks) {
    this.candidateMarks = marks || [];
    this.lookaheadMarks = [];
    this.render();
  };

  GoBoard.prototype.undo = function () {
    if (this.history.length === 0) return false;
    var last = this.history.pop();
    this.board[last.x][last.y] = 0;
    var opp = last.color === 1 ? 2 : 1;
    for (var i = 0; i < last.captured.length; i++) {
      this.board[last.captured[i][0]][last.captured[i][1]] = opp;
    }
    this.lastMove = this.history.length > 0
      ? { x: this.history[this.history.length - 1].x, y: this.history[this.history.length - 1].y, color: this.history[this.history.length - 1].color }
      : null;
    this.previousBoardHash = null;
    this.candidateMarks = [];
    this.ownerData = null;
    this.render();
    return true;
  };

  GoBoard.prototype.clear = function () {
    this._initBoard();
    this.history = [];
    this.lastMove = null;
    this.previousBoardHash = null;
    this.candidateMarks = [];
    this.lookaheadMarks = [];
    this.ownerData = null;
    this.render();
  };

  GoBoard.prototype.resize = function (size) {
    this.size = size;
    this.clear();
  };

  // ---- 手质量边框 ----
  // 策略：永远保留"最近一手黑棋 + 最近一手白棋"各一个质量圈
  // 调用方式：setStoneBorder(x, y, borderColor, moverColor)
  //   - borderColor: 彩圈颜色（妙手绿、俗手黄、恶手红）；对于"本手"请传中性圈色
  //     (调用方负责本手中性色：黑棋→#fff白圈，白棋→#000黑圈)
  //   - moverColor: 1=黑棋 / 2=白棋
  GoBoard.prototype.setStoneBorder = function (x, y, color, moverColor) {
    // 先删除对应颜色上一手的圈
    if (moverColor === 1 && this.lastBorderBlack && this.stoneBorders[this.lastBorderBlack]) {
      delete this.stoneBorders[this.lastBorderBlack];
    }
    if (moverColor === 2 && this.lastBorderWhite && this.stoneBorders[this.lastBorderWhite]) {
      delete this.stoneBorders[this.lastBorderWhite];
    }
    var key = x + "," + y;
    if (x >= 0 && y >= 0 && color) {
      this.stoneBorders[key] = color;
      if (moverColor === 1) this.lastBorderBlack = key;
      else if (moverColor === 2) this.lastBorderWhite = key;
    } else {
      if (moverColor === 1) this.lastBorderBlack = null;
      else if (moverColor === 2) this.lastBorderWhite = null;
    }
    this.render();
  };
  GoBoard.prototype.clearStoneBorders = function () {
    this.stoneBorders = {};
    this.lastBorderBlack = null;
    this.lastBorderWhite = null;
    this.render();
  };

  // ---- 推演标记 ----
  GoBoard.prototype.setLookaheadMarks = function (marks) {
    this.lookaheadMarks = marks || [];
    this.render();
  };
  GoBoard.prototype.clearLookaheadMarks = function () {
    this.lookaheadMarks = [];
    this.render();
  };

  // ---- 势力分布 ----
  GoBoard.prototype.setOwnerData = function (data) {
    this.ownerData = data || null;
    this.render();
  };
  GoBoard.prototype.clearOwnerData = function () {
    this.ownerData = null;
    this.render();
  };

  // ---- 复盘导航 ----
  GoBoard.prototype.gotoMove = function (index) {
    // index = -1 表示空盘
    index = Math.max(-1, Math.min(index, this.history.length - 1));
    this._initBoard();
    this.previousBoardHash = null;
    this.stoneBorders = {};
    this.candidateMarks = [];
    this.lookaheadMarks = [];
    this.ownerData = null;
    for (var i = 0; i <= index; i++) {
      var h = this.history[i];
      this.board[h.x][h.y] = h.color;
      // 恢复提子
      var opp = h.color === 1 ? 2 : 1;
      if (h.captured) {
        for (var j = 0; j < h.captured.length; j++) {
          this.board[h.captured[j][0]][h.captured[j][1]] = 0;
        }
      }
    }
    if (index >= 0) {
      var last = this.history[index];
      this.lastMove = { x: last.x, y: last.y, color: last.color };
    } else {
      this.lastMove = null;
    }
    this.render();
  };

  // 复盘模式开关
  GoBoard.prototype.setReviewMode = function (on) {
    this.reviewMode = !!on;
    if (on) { this.hoverXY = null; }
    this.render();
  };

  // 导出坐标转换供外部使用
  GoBoard.xyToGtp = function (x, y, size) {
    if (x < 0 || x >= size) return "pass";
    return GTP_COLUMNS[x] + (size - y);
  };

  global.GoBoard = GoBoard;
})(window);

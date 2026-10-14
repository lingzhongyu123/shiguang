"use strict";
var SECTIONS = ["长期背景", "当前目标", "长期偏好", "进行中的项目", "经多次观察得到的模式", "近期变化"];

var state = { entries: [], pending: [], todos: [], goals: [], events: [], profile: { sections: {}, updated: "" } };

/* ---- 基础 ---- */
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
/* 轻量 markdown 渲染:先转义再替换,只认标题/加粗/斜体/行内代码/列表符 */
function md(s) {
  return esc(s)
    .replace(/^#{1,6}\s+(.+)$/gm, "<b>$1</b>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\*([^*\n]+)\*/g, "<i>$1</i>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/^\s*[-•]\s+/gm, "· ");
}
function toast(msg) {
  var t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(function () { t.classList.remove("show"); }, 2400);
}
function api(method, path, body) {
  var opt = { method: method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  return fetch(path, opt).then(function (r) {
    if (r.status === 401) {
      showLogin();
      throw new Error("未登录");
    }
    return r.json().then(function (data) {
      if (!r.ok) throw new Error(data.error || data.detail || ("请求失败 " + r.status));
      return data;
    });
  });
}

/* ---- 登录 ---- */
function showLogin() {
  $("app").hidden = true;
  $("navTabs").hidden = true;
  $("loginPane").hidden = false;
}
function showApp() {
  $("loginPane").hidden = true;
  $("app").hidden = false;
  $("navTabs").hidden = false;
}
$("btnLogin").onclick = function () {
  $("btnLogin").disabled = true;
  api("POST", "/api/login", { password: $("pwd").value }).then(function () {
    showApp();
    refreshAll();
  }).catch(function (e) {
    var err = $("loginErr");
    err.textContent = e.message === "未登录" ? "密码不对" : e.message;
    err.hidden = false;
  }).finally(function () { $("btnLogin").disabled = false; });
};
$("pwd").addEventListener("keydown", function (e) {
  if (e.key === "Enter") $("btnLogin").click();
});
$("btnLogout").onclick = function () {
  api("POST", "/api/logout").then(function () { location.reload(); });
};

/* ---- 刷新 ---- */
function refreshAll() {
  api("GET", "/api/entries").then(function (es) { state.entries = es; renderLog(); }).catch(function () {});
  api("GET", "/api/pending").then(function (ps) { state.pending = ps; renderPending(); }).catch(function () {});
  api("GET", "/api/todos").then(function (ts) { state.todos = ts; renderTodos(); }).catch(function () {});
  api("GET", "/api/profile").then(function (p) { state.profile = p; renderProfile(); }).catch(function () {});
  api("GET", "/api/goals").then(function (gs) { state.goals = gs; renderGoals(); }).catch(function () {});
  api("GET", "/api/events").then(function (es) { state.events = es; renderEvents(); }).catch(function () {});
}

/* ---- 页签 ---- */
function switchPane(name) {
  document.querySelectorAll("nav.tabs button").forEach(function (x) { x.classList.remove("active"); });
  document.querySelector('nav.tabs button[data-pane="' + name + '"]').classList.add("active");
  ["record", "log", "mem", "profile"].forEach(function (id) {
    $("pane-" + id).hidden = id !== name;
  });
  window.scrollTo(0, 0);
  // 重触发切入动画(切 tab 时轻滑入,不是硬切)
  var p = $("pane-" + name);
  p.style.animation = "none";
  void p.offsetWidth;  // 强制回流以重启动画
  p.style.animation = "";
}
document.querySelectorAll("nav.tabs button").forEach(function (b) {
  b.onclick = function () { switchPane(b.dataset.pane); };
});

/* ---- 提交处理 ---- */
function greet() {
  var now = new Date();
  var wd = ["日", "一", "二", "三", "四", "五", "六"][now.getDay()];
  var h = now.getHours();
  var g = h < 5 ? "夜深了" : h < 11 ? "早上好" : h < 14 ? "中午好" : h < 18 ? "下午好" : "晚上好";
  $("greet").innerHTML = g + ",今天 · <b>" + (now.getMonth() + 1) + "月" + now.getDate() + "日 星期" + wd + "</b>";
}
/* 任务轮询:提交与页面重开后的恢复共用。
   btn 传 null 表示后台恢复(不锁按钮)。
   全局只允许一个轮询器;轮询 16 分钟强制超时(服务端 15 分钟收割僵尸任务,
   前端兜底防止极端情况下无限轮询)。 */
var _poller = null;

/* 失败原文暂存(多任务叠加时拼接,不互相覆盖) */
function stashFailedRaw(raw) {
  try {
    var old = localStorage.getItem("sg_failed_raw");
    if (old && old !== raw) raw = old + "\n\n————\n\n" + raw;
    localStorage.setItem("sg_failed_raw", raw);
  } catch (e) {}
}

function pollJob(jid, btn) {
  if (_poller) {
    // 覆盖旧轮询器前,先把旧任务的终态查一次:失败的原文暂存、完成的提示归档,
    // 不让被顶掉的任务静默消失(典型:启动续接的任务被新提交覆盖)
    var oldJid = _poller.jid;
    clearInterval(_poller.ival);
    _poller = null;
    api("GET", "/api/jobs/" + oldJid).then(function (j) {
      if (j.status === "done") { refreshAll(); toast("上一个任务已归档"); }
      else if (j.status === "failed" && j.raw) {
        stashFailedRaw(j.raw);
        toast("上一个任务处理失败,原文已暂存(清空输入框后刷新可找回)");
      }
    }).catch(function () {});
  }
  var t0 = Date.now();
  try { localStorage.setItem("sg_job", JSON.stringify({ id: jid, t: t0 })); } catch (e) {}
  var errs = 0;  // 网络闪断容忍:连续 3 次失败才放弃
  var p = { jid: jid, ival: null };
  function finish(job, errMsg) {
    clearInterval(p.ival);
    if (_poller === p) _poller = null;
    // job 为 null = 没拿到终态(网络断/登录过期/墙钟超时):保留 sg_job 供刷新后续接,
    // 原文还在服务端 jobs 表里,不能因为前端断连就当任务没了
    var known = !!job;
    _pendingNew = null;  // 骨架卡片撤下(完成→_jumpToNew 填充;失败→toast 提示)
    renderLog();
    if (known) {
      try {
        var j = JSON.parse(localStorage.getItem("sg_job") || "null");
        if (!j || j.id === jid) localStorage.removeItem("sg_job");
      } catch (e) {}
    }
    if (btn) {
      btn.disabled = false;
      btn.textContent = "提交并处理";
    }
    $("procHint").textContent = "提交后由服务端的 GLM 自动整理,约需十几秒。";
    if (job && job.status === "missing") {
      toast("任务记录已不存在(可能恢复过备份),无法续接");
    } else if (job && job.status === "done") {
      _jumpToNew = true;  // 新日志在日志页自动展开定位
      refreshAll();
      switchPane("log");
      toast("已归档");
    } else {
      var raw = job ? job.raw : "";
      if (!known) {
        toast((errMsg || "连接中断") + " —— 任务仍在服务端,刷新页面可续接查看结果");
      } else if (raw) {
        if (!$("inRaw").value.trim()) {
          $("inRaw").value = raw;  // 处理失败时把口述原文找回来,不丢内容
          toast("处理失败,原文已恢复到输入框:" + (errMsg || "未知错误"));
        } else {
          // 输入框已有新内容,原文暂存,下次清空输入框打开页面时找回
          stashFailedRaw(raw);
          toast("处理失败,原文已暂存(清空输入框后刷新可找回):" + (errMsg || "未知错误"));
        }
      } else {
        toast(errMsg || job && job.error || "处理失败");
      }
    }
  }
  p.ival = setInterval(function () {
    if (Date.now() - t0 > 16 * 60 * 1000) {  // 墙钟兜底
      finish(null, "任务超时(服务可能重启过),请重新提交");
      return;
    }
    api("GET", "/api/jobs/" + jid).then(function (job) {
      if (job.status === "queued" || job.status === "running") {
        errs = 0;
        var stages = { embedding: "正在检索相关记忆…", generating: "GLM 正在整理与提炼…", saving: "正在归档…" };
        var sec = Math.round((Date.now() - t0) / 1000);
        $("procHint").textContent = (btn ? "" : "后台任务:") + (stages[job.stage] || "排队/处理中…") + "(" + sec + "s)";
        var sk = $("pendStage");
        if (sk && _pendingNew && _pendingNew.t0 === t0) sk.textContent = (stages[job.stage] || "排队/处理中…") + "(" + sec + "s)";
        return;
      }
      finish(job, job.error);
    }).catch(function (e) {
      if (++errs < 3 && e.message !== "未登录" && e.message.indexOf("不存在") < 0) return;
      // 404(任务不存在,如数据库重置后恢复备份)是确定终态,不能当"未知状态"保留续接
      finish(e.message.indexOf("不存在") >= 0 ? { status: "missing" } : null, e.message);
    });
  }, 1500);
  _poller = p;
}

/* 草稿自动保存:刷新/闪退不丢已输入的口述 */
$("inRaw").addEventListener("input", function () {
  try { localStorage.setItem("sg_draft", $("inRaw").value); } catch (e) {}
});
function restoreDraft() {
  try {
    var d = localStorage.getItem("sg_draft");
    if (d && !$("inRaw").value.trim()) $("inRaw").value = d;
  } catch (e) {}
}

$("btnProcess").onclick = function () {
  var raw = $("inRaw").value.trim();
  var mode = $("inMode").value;
  if (!raw && !mode) { toast("先输入口述日志,或选择一个模式"); return; }
  if (!raw && !state.entries.length &&
      (mode === "/周总结" || mode === "/月总结" || mode === "/查看画像")) {
    toast("还没有归档日志,先记录几条再总结"); return;
  }
  var btn = $("btnProcess");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>正在整理…';
  $("procHint").textContent = "GLM 正在后台整理,可以去别的页看,好了会提示(约 10–60 秒)。";
  api("POST", "/api/process", { raw: raw, mode: mode }).then(function (r) {
    $("inRaw").value = "";
    try { localStorage.removeItem("sg_draft"); } catch (e) {}
    _pendingNew = { t0: Date.now() };  // 日志页置顶骨架卡片,边等边看得到进度
    renderLog();
    switchPane("log");
    pollJob(r.jobId, btn);
  }).catch(function (e) {
    btn.disabled = false;
    btn.textContent = "提交并处理";
    $("procHint").textContent = "提交后由服务端的 GLM 自动整理,约需十几秒。";
    toast(e.message);
  });
};

/* ---- 日志(按日期分组的线性表,点开日期再看单条) ---- */
var _openDates = null;   // 展开的日期集合;null 表示首次渲染只展开最新一天
var _openEntries = {};   // 展开的日志 id → true
var _jumpToNew = false;  // 刚归档的新日志:自动展开其日期和正文
var _activeTab = {};     // 日志 id → 当前页签(整理/复盘/记忆/画像),重渲染不弹回
var _pendingNew = null;  // 提交后正在处理的新日志:置顶骨架卡片,完成即填充

function todayKey() {
  var d = new Date();
  return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
}

function fmtDate(d) {
  var m = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(d || "");
  if (!m) return d || "未注明日期";
  var wd = ["日", "一", "二", "三", "四", "五", "六"][new Date(+m[1], +m[2] - 1, +m[3]).getDay()];
  var s = +m[2] + "月" + +m[3] + "日 星期" + wd;
  if (+m[1] !== new Date().getFullYear()) s += " " + m[1];
  return s;
}

function entryTitle(e) {  // 折叠态的单行预览
  var t = (e.polished || "").split("\n")[0].replace(/^#+\s*/, "");
  if (!t) t = (e.summary || "").split("\n")[0];
  return t || "(无正文)";
}

function renderLog() {
  var box = $("logList");
  if (!state.entries.length) {
    box.innerHTML = '<div class="card empty">还没有归档的日志。<br>回「记录」页,说第一段吧。</div>';
    return;
  }
  // 按日期分组(entries 已按 id 倒序 → 日期天然从新到旧)
  var groups = [];
  var byDate = {};
  state.entries.forEach(function (e) {
    var k = (e.date || "").slice(0, 10) || "未注明日期";
    if (!byDate[k]) { byDate[k] = []; groups.push(k); }
    byDate[k].push(e);
  });
  if (_openDates === null) _openDates = {};
  if (_jumpToNew && state.entries.length) {
    var ne = state.entries[0];  // entries 按 id 倒序,最新在前
    _openDates[(ne.date || "").slice(0, 10) || "未注明日期"] = true;
    _openEntries[ne.id] = true;
    _jumpToNew = false;
  }
  var h = groups.map(function (k) {
    var open = !!_openDates[k];
    var hs = '<div class="card dgroup' + (open ? " open" : "") + '" data-date="' + esc(k) + '">' +
      '<div class="dhead" data-date="' + esc(k) + '"><span class="darrow">' + (open ? "▾" : "▸") +
      '</span><b>' + esc(/^未注明/.test(k) ? k : fmtDate(k)) + '</b>' +
      '<span class="dcount">' + byDate[k].length + ' 条</span></div>' +
      '<div class="dbody"' + (open ? "" : " hidden") + ">";
    hs += byDate[k].map(function (e) {
      var eo = _openEntries[e.id];
      var card = '<div class="ecard' + (eo ? "" : " collapsed") + '" data-eid="' + e.id + '">' +
        '<div class="ehead">' + esc(entryTitle(e)) +
        (e.mode ? ' <span class="chip">' + esc(e.mode) + "</span>" : "") +
        '<span class="darrow">' + (eo ? "▾" : "▸") + "</span></div>" +
        '<div class="efull"' + (eo ? "" : " hidden") + ">";
      var tab = _activeTab[e.id] || "整理";
      card += '<div class="etabs">' +
        '<button' + (tab === "整理" ? ' class="active"' : "") + ' data-et="整理">整理</button>' +
        '<button' + (tab === "复盘" ? ' class="active"' : "") + ' data-et="复盘">复盘</button>' +
        '<button' + (tab === "记忆" ? ' class="active"' : "") + ' data-et="记忆">记忆</button>' +
        '<button' + (tab === "画像" ? ' class="active"' : "") + ' data-et="画像">画像</button></div>';
      card += '<div class="ebody" data-eb="整理"' + (tab === "整理" ? "" : " hidden") + ">" + md(e.polished || "(无正文)") + "</div>";
      card += '<div class="ebody" data-eb="复盘"' + (tab === "复盘" ? "" : " hidden") + ">";
      if (e.understanding) card += '<div class="hint" style="color:var(--fg)">理解与回应</div><div>' + md(e.understanding) + "</div>";
      if (e.growth) card += '<div class="hint" style="margin-top:8px;color:var(--fg)">成长观察</div><div>' + md(e.growth) + "</div>";
      if (e.summary) card += '<div class="hint" style="margin-top:8px;color:var(--fg)">内容提炼</div><div>' + md(e.summary) + "</div>";
      if (e.advice && e.advice.length) {
        card += '<div class="hint" style="margin-top:8px">观察与建议:</div><ul class="plain">';
        e.advice.forEach(function (a, i) {
          // 观察/建议/思考/目的分行展示;旧数据是分号分隔的,展示时同样拆行
          if (a.indexOf("\n") < 0 && /观察:[^;]+;/.test(a)) a = a.replace(/;/g, "\n");
          var html = esc(a).replace(/\n/g, "<br>")
            .replace(/(^|<br>)(观察|建议|思考|目的):/g, '$1<b>$2:</b>');
          card += '<li><div class="tx">' + html + '</div><button class="btn small ghost" data-todo="' + e.id + ":" + i + '">转为待办</button></li>';
        });
        card += "</ul>";
      }
      if (e.takeaway) card += '<div class="takeaway">“' + esc(e.takeaway) + "”</div>";
      if (!e.understanding && !e.growth && !e.summary && !e.takeaway && !(e.advice && e.advice.length)) card += '<span class="hint">(本次无复盘内容)</span>';
      card += "</div>";
      card += '<div class="ebody" data-eb="记忆"' + (tab === "记忆" ? "" : " hidden") + ">";
      if (e.mempack && e.mempack.length) {
        card += '<ul class="plain">';
        e.mempack.forEach(function (m) { card += "<li>" + esc(typeof m === "string" ? m : m.text) + "</li>"; });
        card += "</ul>";
      } else card += '<span class="hint">(本次无记忆更新)</span>';
      card += "</div>";
      card += '<div class="ebody" data-eb="画像"' + (tab === "画像" ? "" : " hidden") + ">";
      if (e.profileDelta) card += esc(e.profileDelta) + (e.evi ? '<div><span class="chip acc">证据 ' + esc(e.evi) + "</span></div>" : "");
      else card += '<span class="hint">(本次画像不变)</span>';
      card += "</div>";
      if (e.events && e.events.length) {
        card += '<div class="hint" style="margin-top:8px">本次事件:</div><ul class="plain">';
        e.events.forEach(function (ev) {
          card += '<li><span class="chip">' + esc(ev.type) + '</span> <b>' + esc(ev.title) + "</b>" +
               (ev.description ? " — " + esc(ev.description) : "") + "</li>";
        });
        card += "</ul>";
      }
      if (e.raw) card += '<details style="margin-top:8px"><summary>原始口述</summary><div class="hint" style="white-space:pre-wrap">' + esc(e.raw) + "</div></details>";
      card += '<div class="row" style="margin-top:8px"><button class="btn danger small" data-del="' + e.id + '">删除</button></div>';
      return card + "</div></div>";  // 关 efull + ecard
    }).join("");
    return hs + "</div></div>";  // 关 dbody + dgroup
  }).join("");
  if (_pendingNew) {
    // 置顶骨架卡片:提交后立刻可见"正在整理",完成由 _jumpToNew 填充真卡片
    h = '<div class="card" style="padding:12px 14px;display:flex;align-items:center;gap:10px">' +
        '<span class="spin2"></span><div><b>正在整理刚才提交的日志</b>' +
        '<div class="hint" id="pendStage">排队/处理中…</div></div></div>' + h;
  }
  box.innerHTML = h;

  box.querySelectorAll(".dhead").forEach(function (b) {
    b.onclick = function () {
      var k = b.dataset.date;
      if (_openDates[k]) delete _openDates[k]; else _openDates[k] = true;
      renderLog();
      var el = box.querySelector('.dhead[data-date="' + k + '"]');
      if (el) el.scrollIntoView({ block: "start" });  // 展开后把该日期滚回视野
    };
  });
  box.querySelectorAll(".ehead").forEach(function (b) {
    b.onclick = function () {
      var id = +b.closest(".ecard").dataset.eid;
      if (_openEntries[id]) delete _openEntries[id]; else _openEntries[id] = true;
      renderLog();
      var el = box.querySelector('.ecard[data-eid="' + id + '"]');
      if (el) el.scrollIntoView({ block: "nearest" });
    };
  });
  box.querySelectorAll(".etabs button").forEach(function (b) {
    b.onclick = function (e) {
      e.stopPropagation();
      var card = b.closest(".ecard");
      _activeTab[+card.dataset.eid] = b.dataset.et;  // 记住页签,重渲染不弹回
      card.querySelectorAll(".etabs button").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      card.querySelectorAll(".ebody").forEach(function (x) { x.hidden = x.dataset.eb !== b.dataset.et; });
    };
  });
  box.querySelectorAll("[data-todo]").forEach(function (b) {
    b.onclick = function () {
      var parts = b.dataset.todo.split(":");
      var e = state.entries.filter(function (x) { return String(x.id) === parts[0]; })[0];
      if (!e || !e.advice[+parts[1]]) return;
      // 优先取"建议:"那行作为待办;没有则用整条
      var lines = e.advice[+parts[1]].split("\n");
      var sug = lines.filter(function (l) { return l.indexOf("建议:") === 0; })[0] || lines[0];
      api("POST", "/api/todos", { text: sug.replace(/^建议:\s*/, ""), src_entry: e.id }).then(function () {
        refreshAll();
        toast("已加入行动事项");
        switchPane("mem");
      }).catch(function (err) { toast(err.message); });
    };
  });
  box.querySelectorAll("[data-del]").forEach(function (b) {
    b.onclick = function () {
      if (!confirm("删除这条日志?已确认的记忆和待办会保留。")) return;
      api("DELETE", "/api/entries/" + b.dataset.del).then(function () {
        refreshAll();
        toast("已删除");
      }).catch(function (err) { toast(err.message); });
    };
  });
}

/* ---- 待审核 ---- */
function renderBadge() {
  var btn = document.querySelector('nav.tabs button[data-pane="mem"]');
  var old = btn.querySelector(".dot");
  if (old) old.remove();
  if (state.pending.length) {
    var d = document.createElement("span");
    d.className = "dot";
    d.textContent = state.pending.length;
    btn.appendChild(d);
  }
}
function renderPending() {
  var box = $("pendList");
  renderBadge();
  if (!state.pending.length) {
    box.innerHTML = '<div class="empty" style="padding:16px 6px">没有待审核的记忆。</div>';
    return;
  }
  box.innerHTML = state.pending.map(function (p) {
    var h = '<div class="card pend" style="margin-bottom:10px;padding:12px 14px"><div>' + esc(p.text) + "</div>";
    if (p.evi) h += '<span class="chip">来源证据:' + esc(p.evi) + "</span>";
    if (p.confidence !== null && p.confidence !== undefined) h += '<span class="chip">AI 置信度 ' + Math.round(p.confidence * 100) + "%</span>";
    if (p.note) h += '<span class="chip">' + esc(p.note) + "</span>";
    h += '<div class="row" style="margin-top:8px"><label class="hint" style="margin:0">归入</label><select data-pid="' + p.id + '">';
    SECTIONS.forEach(function (s) {
      h += '<option value="' + s + '"' + (s === p.cat ? " selected" : "") + ">" + s + "</option>";
    });
    h += "</select>";
    h += '<button class="btn small" data-ok="' + p.id + '">确认并入</button>' +
      '<button class="btn small ghost" data-skip="' + p.id + '">暂不收录</button></div></div>';
    return h;
  }).join("");
  box.querySelectorAll("[data-ok]").forEach(function (b) {
    b.onclick = function () {
      var p = state.pending.filter(function (x) { return String(x.id) === b.dataset.ok; })[0];
      var sel = box.querySelector('select[data-pid="' + b.dataset.ok + '"]');
      var cat = sel ? sel.value : (p ? p.cat : "长期背景");
      api("POST", "/api/pending/" + b.dataset.ok + "/confirm", { cat: cat }).then(function () {
        refreshAll();
        toast("已并入「" + cat + "」");
      }).catch(function (err) { toast(err.message); });
    };
  });
  box.querySelectorAll("[data-skip]").forEach(function (b) {
    b.onclick = function () {
      api("POST", "/api/pending/" + b.dataset.skip + "/skip").then(function () {
        refreshAll();
        toast("已跳过");
      }).catch(function (err) { toast(err.message); });
    };
  });
}

/* ---- 待办 ---- */
function renderTodos() {
  var box = $("todoList");
  if (!state.todos.length) {
    box.innerHTML = '<div class="empty" style="padding:14px 6px">暂无待办。日志里的建议可以一键转为待办。</div>';
    return;
  }
  box.innerHTML = state.todos.map(function (t) {
    var h = '<div class="todo' + (t.done ? " done" : "") + '"><input type="checkbox" data-td="' + t.id + '"' + (t.done ? " checked" : "") + ">";
    h += '<div class="tx">' + esc(t.text) + (t.due ? ' <span class="due">截止 ' + esc(t.due) + "</span>" : "") + "</div>";
    h += '<button class="btn small ghost" data-tdel="' + t.id + '">✕</button></div>';
    return h;
  }).join("");
  box.querySelectorAll("[data-td]").forEach(function (c) {
    c.onchange = function () {
      api("PATCH", "/api/todos/" + c.dataset.td, { done: c.checked }).then(function () {
        refreshAll();
      }).catch(function (err) { toast(err.message); });
    };
  });
  box.querySelectorAll("[data-tdel]").forEach(function (b) {
    b.onclick = function () {
      api("DELETE", "/api/todos/" + b.dataset.tdel).then(function () { refreshAll(); })
        .catch(function (err) { toast(err.message); });
    };
  });
}
$("btnTodoAdd").onclick = function () {
  var t = $("todoNew").value.trim();
  if (!t) { toast("先填写待办内容"); return; }
  api("POST", "/api/todos", { text: t, due: $("todoDue").value }).then(function () {
    $("todoNew").value = "";
    refreshAll();
  }).catch(function (err) { toast(err.message); });
};
$("todoNew").addEventListener("keydown", function (e) {
  if (e.key === "Enter") $("btnTodoAdd").click();
});

/* ---- 目标 ---- */
var GOAL_STATUS = { active: "进行中", paused: "暂停", done: "已完成", abandoned: "放弃" };
function renderGoals() {
  var box = $("goalList");
  if (!state.goals.length) {
    box.innerHTML = '<div class="empty" style="padding:14px 6px">还没有目标。添加后,AI 处理日志时会把内容和目标关联。</div>';
    return;
  }
  box.innerHTML = state.goals.map(function (g) {
    var h = '<div class="todo' + (g.status !== "active" ? " done" : "") + '">';
    h += '<div class="tx"><b>' + esc(g.title) + "</b>";
    if (g.deadline) h += ' <span class="due">截止 ' + esc(g.deadline) + "</span>";
    if (g.status !== "active") h += ' <span class="chip">' + esc(GOAL_STATUS[g.status] || g.status) + "</span>";
    if (g.status === "active" && g.daysIdle !== null && g.daysIdle >= 7)
      h += '<div class="hint" style="color:#b4552d">⚠ 最近 ' + g.daysIdle + " 天没有与它相关的日志</div>";
    h += "</div>";
    if (g.status === "active")
      h += '<button class="btn small ghost" data-gdone="' + g.id + '">完成</button>';
    h += '<button class="btn small ghost" data-gdel="' + g.id + '">✕</button></div>';
    return h;
  }).join("");
  box.querySelectorAll("[data-gdone]").forEach(function (b) {
    b.onclick = function () {
      api("PATCH", "/api/goals/" + b.dataset.gdone, { status: "done" }).then(function () { refreshAll(); })
        .catch(function (err) { toast(err.message); });
    };
  });
  box.querySelectorAll("[data-gdel]").forEach(function (b) {
    b.onclick = function () {
      if (!confirm("删除这个目标?")) return;
      api("DELETE", "/api/goals/" + b.dataset.gdel).then(function () { refreshAll(); })
        .catch(function (err) { toast(err.message); });
    };
  });
}
$("btnGoalAdd").onclick = function () {
  var t = $("goalNew").value.trim();
  if (!t) { toast("先填写目标内容"); return; }
  api("POST", "/api/goals", { title: t, deadline: $("goalDue").value }).then(function () {
    $("goalNew").value = "";
    refreshAll();
  }).catch(function (err) { toast(err.message); });
};
$("goalNew").addEventListener("keydown", function (e) {
  if (e.key === "Enter") $("btnGoalAdd").click();
});

/* ---- 事件时间线 ---- */
var EV_LABEL = { research: "科研", study: "学习", life: "生活", decision: "决定", relation: "人际" };
function renderEvents() {
  var box = $("eventList");
  if (!state.events.length) {
    box.innerHTML = '<div class="empty" style="padding:14px 6px">还没有提取到事件。记录几条日志后,重要事件会出现在这里。</div>';
    return;
  }
  box.innerHTML = state.events.map(function (ev) {
    var stars = "★".repeat(Math.min(5, ev.importance || 1));
    var h = '<div class="todo"><div class="tx">' +
      '<span class="hint">' + esc(ev.date || "") + "</span> ";
    h += '<span class="chip">' + esc(EV_LABEL[ev.type] || ev.type) + "</span> <b>" + esc(ev.title) + "</b>";
    if (ev.importance >= 4) h += ' <span style="color:#c9962e;font-size:12px">' + stars + "</span>";
    if (ev.description) h += '<div class="hint" style="white-space:normal">' + esc(ev.description) + "</div>";
    h += "</div>";
    h += '<button class="btn small ghost" data-edel="' + ev.id + '">✕</button></div>';
    return h;
  }).join("");
  box.querySelectorAll("[data-edel]").forEach(function (b) {
    b.onclick = function () {
      api("DELETE", "/api/events/" + b.dataset.edel).then(function () { refreshAll(); })
        .catch(function (err) { toast(err.message); });
    };
  });
}

/* ---- 画像 ---- */
function renderProfile() {
  var view = $("profileView");
  var p = state.profile;
  var secs = p.sections || {};
  var items = p.items || [];
  var n = p.updated ? ' <span class="hint">更新于 ' + esc(p.updated) + "</span>" : "";
  var h = "<h2>个人核心记忆档案" + n + "</h2>";
  var any = false;
  SECTIONS.forEach(function (s) {
    h += '<div class="profile-block"><h3>' + s + "</h3>";
    var secItems = items.filter(function (i) { return i.section === s; });
    if (secItems.length) {
      any = true;
      secItems.forEach(function (i) {
        if (i.status === "deprecated") {
          h += '<p style="text-decoration:line-through;opacity:.55">' + esc(i.text) +
               ' <button class="btn small ghost" data-aret="' + i.id + '">恢复</button></p>';
        } else {
          h += "<p>" + esc(i.text) +
               (i.lastVerified ? ' <span class="hint">核证 ' + esc(i.lastVerified) + "</span>" : "") +
               ' <button class="btn small ghost" data-adep="' + i.id + '">停用</button></p>';
          var ev = i.evidence || [];
          if (ev.length) {
            h += '<details style="margin:-4px 0 6px"><summary class="hint">来源 ' + ev.length + " 篇日志</summary><ul class=\"plain\">";
            ev.forEach(function (e2) {
              h += '<li><span class="hint">' + esc(e2.date || "") + "</span> " + esc(e2.title) + "</li>";
            });
            h += "</ul></details>";
          }
        }
      });
    } else h += '<p class="hint">暂无记录</p>';
    h += "</div>";
  });
  if (!any) h += '<p class="hint">在「记忆」页确认待审核记忆后,档案会逐渐丰满起来。</p>';
  view.innerHTML = h;
  view.querySelectorAll("[data-adep]").forEach(function (b) {
    b.onclick = function () {
      if (!confirm("停用这条记忆?它会从 AI 的档案里移除,但保留在历史中(可恢复)。")) return;
      api("POST", "/api/archive/" + b.dataset.adep + "/status", { status: "deprecated" })
        .then(function () { refreshAll(); toast("已停用"); })
        .catch(function (err) { toast(err.message); });
    };
  });
  view.querySelectorAll("[data-aret]").forEach(function (b) {
    b.onclick = function () {
      api("POST", "/api/archive/" + b.dataset.aret + "/status", { status: "active" })
        .then(function () { refreshAll(); toast("已恢复"); })
        .catch(function (err) { toast(err.message); });
    };
  });
  $("profileEdit").value = SECTIONS.map(function (s) {
    return "## " + s + "\n" + ((secs[s] || []).join("\n") || "(暂无)");
  }).join("\n\n");
}
$("btnProfileSave").onclick = function () {
  api("PUT", "/api/profile", { text: $("profileEdit").value }).then(function () {
    refreshAll();
    toast("档案已保存");
  }).catch(function (err) { toast(err.message); });
};

/* ---- 导入 ---- */
$("btnImport").onclick = function () { $("fileImport").click(); };
$("fileImport").onchange = function (e) {
  var f = e.target.files[0];
  if (!f) return;
  var r = new FileReader();
  r.onload = function () {
    fetch("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: r.result })
      .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, d: d }; }); })
      .then(function (r2) {
        if (!r2.ok) throw new Error(r2.d.error || r2.d.detail || "导入失败");
        refreshAll();
        toast("已导入 " + r2.d.entries + " 条日志、" + r2.d.pending + " 条记忆、" + r2.d.todos + " 条待办");
      })
      .catch(function (err) { toast(err.message); });
  };
  r.readAsText(f);
};

/* ---- 启动 ---- */
greet();
fetch("/api/profile").then(function (r) {
  if (r.status === 401) showLogin();
  else {
    showApp();
    refreshAll();
    // 恢复上次未完成的任务(30 分钟内):页面关闭/闪断后继续等结果
    try {
      var j = JSON.parse(localStorage.getItem("sg_job") || "null");
      if (j && j.id && Date.now() - j.t < 30 * 60 * 1000) {
        $("procHint").textContent = "发现上次未完成的任务,正在续接…";
        pollJob(j.id, null);
      } else if (j) localStorage.removeItem("sg_job");
    } catch (e) {}
    // 找回上次处理失败时暂存的原文(输入框为空才填,避免覆盖新内容);失败暂存优先于普通草稿
    try {
      var fr = localStorage.getItem("sg_failed_raw");
      if (fr && !$("inRaw").value.trim()) {
        $("inRaw").value = fr;
        toast("已找回上次处理失败的口述原文,请检查后重新提交");
      }
      localStorage.removeItem("sg_failed_raw");
    } catch (e) {}
    restoreDraft();
  }
});

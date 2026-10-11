"use strict";
var SECTIONS = ["长期背景", "当前目标", "长期偏好", "进行中的项目", "经多次观察得到的模式", "近期变化"];

var state = { entries: [], pending: [], todos: [], profile: { sections: {}, updated: "" } };

/* ---- 基础 ---- */
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
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
}

/* ---- 页签 ---- */
function switchPane(name) {
  document.querySelectorAll("nav.tabs button").forEach(function (x) { x.classList.remove("active"); });
  document.querySelector('nav.tabs button[data-pane="' + name + '"]').classList.add("active");
  ["record", "log", "mem", "profile"].forEach(function (id) {
    $("pane-" + id).hidden = id !== name;
  });
  window.scrollTo(0, 0);
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
  $("procHint").textContent = "GLM 正在整理、提炼并生成记忆更新,请稍候(约 10–30 秒)。";
  api("POST", "/api/process", { raw: raw, mode: mode }).then(function () {
    $("inRaw").value = "";
    refreshAll();
    switchPane("log");
    toast("已归档");
  }).catch(function (e) {
    toast(e.message);
  }).finally(function () {
    btn.disabled = false;
    btn.textContent = "提交并处理";
    $("procHint").textContent = "提交后由服务端的 GLM 自动整理,约需十几秒。";
  });
};

/* ---- 日志 ---- */
function renderLog() {
  var box = $("logList");
  if (!state.entries.length) {
    box.innerHTML = '<div class="card empty">还没有归档的日志。<br>回「记录」页,说第一段吧。</div>';
    return;
  }
  box.innerHTML = state.entries.map(function (e) {
    var h = '<div class="card"><div class="edate">' + esc(e.date || "未注明日期") +
      (e.mode ? " · " + esc(e.mode) : "") + "</div>";
    h += '<div class="etabs">' +
      '<button class="active" data-et="整理">整理</button>' +
      '<button data-et="复盘">复盘</button>' +
      '<button data-et="记忆">记忆</button>' +
      '<button data-et="画像">画像</button></div>';
    h += '<div class="ebody" data-eb="整理">' + esc(e.polished || "(无正文)") + "</div>";
    h += '<div class="ebody" data-eb="复盘" hidden>';
    if (e.summary) h += '<div class="hint" style="color:var(--fg)">内容提炼</div><div>' + esc(e.summary) + "</div>";
    if (e.advice && e.advice.length) {
      h += '<div class="hint" style="margin-top:8px">观察与建议:</div><ul class="plain">';
      e.advice.forEach(function (a, i) {
        h += '<li><div class="tx">' + esc(a) + '</div><button class="btn small ghost" data-todo="' + e.id + ":" + i + '">转为待办</button></li>';
      });
      h += "</ul>";
    }
    if (!e.summary && !(e.advice && e.advice.length)) h += '<span class="hint">(本次无复盘内容)</span>';
    h += "</div>";
    h += '<div class="ebody" data-eb="记忆" hidden>';
    if (e.mempack && e.mempack.length) {
      h += '<ul class="plain">';
      e.mempack.forEach(function (m) { h += "<li>" + esc(typeof m === "string" ? m : m.text) + "</li>"; });
      h += "</ul>";
    } else h += '<span class="hint">(本次无记忆更新)</span>';
    h += "</div>";
    h += '<div class="ebody" data-eb="画像" hidden>';
    if (e.profileDelta) h += esc(e.profileDelta) + (e.evi ? '<div><span class="chip acc">证据 ' + esc(e.evi) + "</span></div>" : "");
    else h += '<span class="hint">(本次画像不变)</span>';
    h += "</div>";
    if (e.raw) h += '<details style="margin-top:8px"><summary>原始口述</summary><div class="hint" style="white-space:pre-wrap">' + esc(e.raw) + "</div></details>";
    h += '<div class="row" style="margin-top:8px"><button class="btn danger small" data-del="' + e.id + '">删除</button></div></div>';
    return h;
  }).join("");

  box.querySelectorAll(".etabs button").forEach(function (b) {
    b.onclick = function () {
      var card = b.closest(".card");
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
      api("POST", "/api/todos", { text: e.advice[+parts[1]], src_entry: e.id }).then(function () {
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

/* ---- 画像 ---- */
function renderProfile() {
  var view = $("profileView");
  var p = state.profile;
  var secs = p.sections || {};
  var n = p.updated ? ' <span class="hint">更新于 ' + esc(p.updated) + "</span>" : "";
  var h = "<h2>个人核心记忆档案" + n + "</h2>";
  var any = false;
  SECTIONS.forEach(function (s) {
    h += '<div class="profile-block"><h3>' + s + "</h3>";
    if (secs[s] && secs[s].length) { any = true; h += "<p>" + secs[s].map(esc).join("<br>") + "</p>"; }
    else h += '<p class="hint">暂无记录</p>';
    h += "</div>";
  });
  if (!any) h += '<p class="hint">在「记忆」页确认待审核记忆后,档案会逐渐丰满起来。</p>';
  view.innerHTML = h;
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
  else { showApp(); refreshAll(); }
});

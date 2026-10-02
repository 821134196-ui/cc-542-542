/* 影城放映机灯泡管理 — 前端逻辑（无依赖，原生 fetch） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  if (opts.body) {
    opts.method = opts.method || "POST";
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isErr ? "err" : "";
  t.style.display = "block";
  clearTimeout(t._tm);
  t._tm = setTimeout(() => (t.style.display = "none"), 3600);
}

async function run(fn, okMsg) {
  try {
    await fn();
    if (okMsg) toast(okMsg);
    await render();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------- 常量 ---------------- */
const STEP_NAMES = { 1: "断电", 2: "冷却", 3: "安装", 4: "对焦", 5: "试映" };
const WO_STATUS = {
  in_progress: ["进行中", "blue"], pending_release: ["待放行", "gold"],
  released: ["已放行", "green"], cancelled: ["已取消", ""],
};
const PROJ_STATUS = {
  idle: ["空闲", "green"], running: ["放映中", "blue"],
  maintenance: ["维修中", "gold"], down: ["停机", "red"],
};
const INCIDENT_TYPES = {
  lamp_failure: "异常熄灯", timer_reset_failed: "计时器重置失败",
  spare_mismatch: "备件型号不符", cancelled_midway: "换灯中途取消",
  insufficient_brightness: "试映亮度不足", show_delay: "场次延误",
};
const COVERAGE = {
  ok: ["可覆盖", "green"], tight: ["临界", "gold"],
  insufficient: ["无法覆盖", "red"], over_threshold: ["超阈值·需主管批准", "red"],
  override: ["超阈值·已批准临时使用", "gold"], no_lamp: ["无在用灯泡", "red"],
};
const TABS = [
  ["overview", "影厅总览"], ["ledger", "设备台账·寿命预测"],
  ["workorders", "换灯工单"], ["screenings", "试映记录"],
  ["incidents", "异常·场次影响"], ["approvals", "放行审批"],
];
let currentTab = "overview";
let usersCache = [];

/* ---------------- 骨架 ---------------- */
function renderTabs() {
  $("#tabs").innerHTML = TABS.map(([k, label]) =>
    `<button class="${k === currentTab ? "active" : ""}"
      onclick="switchTab('${k}')">${label}</button>`).join("");
}
window.switchTab = (k) => { currentTab = k; render(); };

async function render() {
  renderTabs();
  const v = $("#view");
  v.innerHTML = "<p class='muted'>加载中…</p>";
  try {
    if (currentTab === "overview") v.innerHTML = await viewOverview();
    else if (currentTab === "ledger") v.innerHTML = await viewLedger();
    else if (currentTab === "workorders") v.innerHTML = await viewWorkOrders();
    else if (currentTab === "screenings") v.innerHTML = await viewScreenings();
    else if (currentTab === "incidents") v.innerHTML = await viewIncidents();
    else if (currentTab === "approvals") v.innerHTML = await viewApprovals();
  } catch (e) {
    v.innerHTML = `<p class="red">${esc(e.message)}</p>`;
  }
}

function badge(text, cls = "") { return `<span class="badge ${cls}">${esc(text)}</span>`; }
function projStatusBadge(s) {
  const [t, c] = PROJ_STATUS[s] || [s, ""];
  return badge(t, c);
}
function lifeBar(lamp) {
  const pct = Math.min(100, (100 * lamp.cumulative_hours / lamp.threshold_hours));
  const cls = pct >= 100 ? "over" : pct >= 90 ? "warn" : "";
  return `<div class="bar ${cls}"><i style="width:${pct}%"></i></div>`;
}
function userOptions(role) {
  return usersCache.filter((u) => !role || u.role === role)
    .map((u) => `<option value="${u.id}">${esc(u.name)}（${u.role === "supervisor" ? "放映主管" : "技术员"}）</option>`).join("");
}

/* ---------------- 影厅总览：下一场开始时间 ∥ 剩余寿命 ---------------- */
async function viewOverview() {
  const [ov, users] = await Promise.all([api("/api/overview"), api("/api/users")]);
  usersCache = users;
  const cards = ov.map((e) => {
    const lamp = e.lamp, p = e.projector, ns = e.next_show;
    const critical = lamp && e.life_pct >= 90 && e.life_pct < 100;
    const over = lamp && e.life_pct >= 100;
    const cov = e.coverage ? COVERAGE[e.coverage.verdict] || [e.coverage.label, ""] : null;
    return `<div class="card ${critical ? "critical" : ""} ${over ? "alarm" : ""}">
      <h3>${esc(e.hall.name)} ${projStatusBadge(p.status)}</h3>
      <div class="muted">${esc(p.model)} ｜ 灯 ${lamp ? esc(lamp.serial) : "—"}</div>
      <div class="pair">
        <div><div class="muted">下一场开始</div>
          <div class="big">${ns ? esc(ns.start_time.slice(5, 16)) : "—"}</div>
          <div class="muted">${ns ? esc(ns.title) + " · " + ns.duration_min + " 分钟" : "无排期"}
            ${ns && ns.status === "delayed" ? badge("已延误 " + ns.delay_min + "分", "gold") : ""}</div>
        </div>
        <div><div class="muted">灯泡剩余寿命</div>
          <div class="big ${over ? "red" : critical ? "gold" : "green"}">
            ${lamp ? e.remaining_hours + " h" : "—"}</div>
          <div class="muted">${lamp ? lamp.cumulative_hours + " / " + lamp.threshold_hours + " h" : ""}</div>
        </div>
      </div>
      ${lamp ? lifeBar(lamp) : ""}
      <div class="row" style="margin-top:8px">
        <span>${cov ? badge(cov[0], cov[1]) : ""}</span>
        <span>
          ${over && !e.override ? `<button class="act gold"
            onclick="grantOverride(${p.id})">主管批准临时使用</button>` : ""}
          ${ns ? `<button class="act" onclick="completeShow(${ns.id})">完成本场·累计时长</button>` : ""}
          <button class="act danger" onclick="lampFailure(${p.id})">模拟异常熄灯</button>
        </span>
      </div>
    </div>`;
  });
  return `<h2>影厅总览<span class="en">下一场开始时间 ∥ 灯泡剩余寿命</span></h2>
    <div class="grid">${cards.join("")}</div>`;
}

window.completeShow = (id) =>
  run(() => api(`/api/shows/${id}/complete`, { method: "POST" }), "已累计本场点亮时长");
window.lampFailure = (pid) => {
  const detail = prompt("异常熄灯说明", "放映中灯泡异常熄灭");
  if (detail === null) return;
  run(() => api("/api/lamp_failure", { body: { projector_id: pid, detail } }),
    "已登记异常熄灯，临近场次标记受影响");
};
window.grantOverride = (pid) => {
  const sid = Number(prompt("批准人（放映主管）编号：3=王正纲 4=赵敏", "3"));
  if (!sid) return;
  const reason = prompt("临时使用理由", "黄金场次，次日换灯") || "";
  run(() => api("/api/overrides",
    { body: { projector_id: pid, supervisor_id: sid, reason } }), "已批准临时使用");
};

/* ---------------- 设备台账 · 寿命预测 ---------------- */
async function viewLedger() {
  const led = await api("/api/ledger");
  const rowsHtml = led.map((e) => {
    const p = e.projector, l = e.lamp, pr = e.prediction;
    return `<tr>
      <td>${esc(p.hall_name)}</td><td>${esc(p.model)}</td>
      <td>${l ? esc(l.serial) : "—"}</td>
      <td>${l ? `${l.cumulative_hours} / ${l.threshold_hours} h` : "—"}</td>
      <td>${projStatusBadge(p.status)}</td>
      <td class="${pr.remaining_hours !== null && pr.remaining_hours < 20 ? "gold" : ""}">
        ${pr.remaining_hours !== null ? pr.remaining_hours + " h" : "—"}</td>
      <td>${pr.shows_left} 场</td>
      <td>${pr.eta ? esc(pr.eta.slice(0, 16)) : "—"}</td>
    </tr>`;
  }).join("");
  const spares = led.map((e) =>
    `<div class="row"><span class="k">${esc(e.projector.hall_name)}（${esc(e.projector.lamp_model)}）</span>
     <span>${e.spares.map((s) => esc(s.serial)).join("、") || "无备件"}</span></div>`).join("");
  return `<h2>设备台账<span class="en">放映机 · 灯泡 · 累计点亮 · 厂家阈值</span></h2>
    <table><tr><th>影厅</th><th>放映机型号</th><th>灯泡序列号</th><th>累计/阈值</th>
      <th>状态</th><th>剩余寿命</th><th>可放场次</th><th>预计触阈</th></tr>${rowsHtml}</table>
    <h2>备件库存<span class="en">按机型匹配</span></h2>
    <div class="card">${spares}</div>`;
}

/* ---------------- 换灯工单：固定编号步骤 ---------------- */
async function viewWorkOrders() {
  const [wos, users, ledger] = await Promise.all([
    api("/api/work_orders"), api("/api/users"), api("/api/ledger")]);
  usersCache = users;
  const projOpts = ledger.map((e) =>
    `<option value="${e.projector.id}">${esc(e.projector.hall_name)} · ${esc(e.projector.model)}</option>`).join("");
  const create = `<form class="inline" onsubmit="return createWO(event)">
    <b>新建换灯工单</b>
    <select id="wo-proj">${projOpts}</select>
    <select id="wo-tech">${userOptions()}</select>
    <input id="wo-reason" placeholder="原因（如：临近阈值 / 异常熄灯）" size="28">
    <button class="act gold">开工单（占用维修窗口）</button>
  </form>`;
  const cards = [];
  for (const w of wos) {
    cards.push(await woCard(w.id));
  }
  return `<h2>换灯工单<span class="en">步骤固定编号 1-5，按序执行避免遗漏</span></h2>
    ${create}${cards.join("") || "<p class='muted'>暂无工单</p>"}`;
}

async function woCard(id) {
  const w = await api(`/api/work_orders?id=${id}`);
  const [st, sc] = WO_STATUS[w.status] || [w.status, ""];
  const nextNo = (w.steps.find((s) => !s.done) || {}).step_no;
  const steps = w.steps.map((s) => {
    const cls = s.done ? "done" : s.step_no === nextNo && w.status === "in_progress" ? "next" : "";
    return `<div class="step ${cls}">
      <span class="no">${s.step_no}</span><span class="nm">${esc(s.name)}</span>
      <div class="t">${s.done ? "✓ " + esc(s.done_at).slice(5, 16) : "待执行"}</div>
    </div>`;
  }).join("");
  let action = "";
  if (w.status === "in_progress" && nextNo) {
    if (nextNo === 3) {
      const spares = await api(`/api/spares`);
      const opts = spares.map((s) =>
        `<option value="${s.id}">${esc(s.serial)}（${esc(s.model)}）</option>`).join("");
      action = `<select id="spare-${w.id}">${opts}</select>
        <button class="act gold" onclick="doStep(${w.id},3)">执行 3 安装</button>`;
    } else {
      action = `<button class="act gold"
        onclick="doStep(${w.id},${nextNo})">执行 ${nextNo} ${STEP_NAMES[nextNo]}</button>`;
    }
    const failed = w.screenings.length && !w.screenings.some((s) => s.result === "pass")
      && w.steps[4].done;
    if (failed) action += ` <button class="act" onclick="rescreen(${w.id})">重新对焦·复测试映</button>`;
    action += ` <button class="act danger" onclick="cancelWO(${w.id})">中途取消</button>`;
  }
  let release = "";
  if (w.status === "pending_release") {
    release = `<div class="row" style="margin-top:8px"><span class="k">最终放行（换灯人不得签署）</span>
      <span><select id="appr-${w.id}">${userOptions("supervisor")}</select>
      <button class="act gold" onclick="release(${w.id},'approved')">批准放行</button>
      <button class="act danger" onclick="release(${w.id},'rejected')">驳回</button></span></div>`;
  }
  const scr = w.screenings.map((s) =>
    `<div class="muted">试映 #${s.id}：${s.brightness_before} fL → ${s.brightness_after} fL
     ${badge(s.result === "pass" ? "合格" : "亮度不足", s.result === "pass" ? "green" : "red")}</div>`).join("");
  const inc = w.open_incidents.map((i) =>
    `<div class="red">⚠ ${INCIDENT_TYPES[i.type]}：${esc(i.detail)}</div>`).join("");
  return `<div class="card section">
    <h3>工单 #${w.id} · ${esc(w.technician.name)} ${badge(st, sc)}</h3>
    <div class="muted">放映机 #${w.projector_id} ｜ ${esc(w.reason || "")} ｜ 开单 ${esc(w.created_at)}
      ${w.cancel_reason ? "｜ 取消原因：" + esc(w.cancel_reason) : ""}</div>
    <div class="steps">${steps}</div>
    <div>${action}</div>${inc}${scr}${release}
  </div>`;
}

window.createWO = (ev) => {
  ev.preventDefault();
  const body = {
    projector_id: Number($("#wo-proj").value),
    technician_id: Number($("#wo-tech").value),
    reason: $("#wo-reason").value,
  };
  run(() => api("/api/work_orders", { body }), "工单已创建，维修窗口已占用");
  return false;
};
window.doStep = (id, no) => {
  const body = {};
  if (no === 3) body.new_lamp_id = Number($(`#spare-${id}`).value);
  run(() => api(`/api/work_orders/${id}/steps/${no}`, { body }),
    `步骤 ${no}（${STEP_NAMES[no]}）已完成`);
};
window.cancelWO = (id) => {
  const reason = prompt("中途取消原因（旧灯已拆下不得复用）", "放映计划调整");
  if (reason === null) return;
  run(() => api(`/api/work_orders/${id}/cancel`, { body: { reason } }), "工单已取消并登记异常");
};
window.rescreen = (id) =>
  run(() => api(`/api/work_orders/${id}/rescreen`, { method: "POST" }), "复检完成");
window.release = (id, decision) => {
  const approver_id = Number($(`#appr-${id}`).value);
  run(() => api(`/api/work_orders/${id}/release`,
    { body: { approver_id, decision, note: "" } }),
    decision === "approved" ? "已放行，维修窗口已释放" : "已驳回，退回整改");
};

/* ---------------- 试映记录：前后亮度成对 ---------------- */
async function viewScreenings() {
  const list = await api("/api/screenings");
  if (!list.length) return "<h2>试映记录</h2><p class='muted'>暂无试映记录</p>";
  const cards = list.map((s) => `<div class="card">
    <h3>试映 #${s.id} · 工单 #${s.work_order_id}
      ${badge(s.result === "pass" ? "合格" : "亮度不足", s.result === "pass" ? "green" : "red")}</h3>
    <div class="muted">${esc(s.created_at)} ｜ 合格线 ≥ 14.0 fL</div>
    <div class="vs">
      <div class="side"><img src="/${esc(s.photo_before)}" alt="换灯前">
        <div class="fl ${s.brightness_before < 14 ? "red" : ""}">${s.brightness_before} fL</div>
        <div class="muted">换灯前</div></div>
      <div class="arrow">→</div>
      <div class="side"><img src="/${esc(s.photo_after)}" alt="换灯后">
        <div class="fl ${s.brightness_after < 14 ? "red" : "green"}">${s.brightness_after} fL</div>
        <div class="muted">换灯后</div></div>
    </div>
  </div>`);
  return `<h2>试映记录<span class="en">前后亮度成对展示</span></h2>
    <div class="grid">${cards.join("")}</div>`;
}

/* ---------------- 异常 · 场次影响 ---------------- */
async function viewIncidents() {
  const [inc, impacts, shows] = await Promise.all([
    api("/api/incidents"), api("/api/impacts"), api("/api/shows")]);
  const incRows = inc.map((i) => `<tr>
    <td>#${i.id}</td><td>${badge(INCIDENT_TYPES[i.type] || i.type,
      i.status === "open" ? "red" : "")}</td>
    <td>${esc(i.detail || "")}</td>
    <td>${i.status === "open" ? badge("待处置", "gold") : badge("已处置", "green")}</td>
    <td>${i.status === "open"
      ? (i.type === "timer_reset_failed"
        ? `<button class="act gold" onclick="retryReset(${i.id})">重试重置</button> ` : "")
      + `<button class="act" onclick="resolveInc(${i.id})">登记处置</button>`
      : esc(i.resolution || "")}</td>
  </tr>`).join("");
  const impRows = impacts.map((m) => `<tr>
    <td>${esc(m.hall_name)}</td><td>《${esc(m.title)}》${esc(m.start_time.slice(5, 16))}</td>
    <td>${badge({ at_risk: "受影响", delayed: "已延误", cancelled: "已取消" }[m.impact],
      m.impact === "at_risk" ? "gold" : "red")}</td>
    <td>${m.delay_min ? m.delay_min + " 分钟" : "—"}</td>
    <td class="muted">${esc(m.cause)}</td>
  </tr>`).join("");
  const showRows = shows.filter((s) => s.status !== "completed").map((s) => `<tr>
    <td>${esc(s.hall_name)}</td><td>《${esc(s.title)}》</td>
    <td>${esc(s.start_time.slice(5, 16))}</td><td>${s.duration_min} 分钟</td>
    <td>${badge({ scheduled: "待映", delayed: "已延误", cancelled: "已取消" }[s.status],
      s.status === "delayed" ? "gold" : "")}</td>
    <td>${s.status !== "cancelled"
      ? `<button class="act" onclick="delayShow(${s.id})">登记延误</button>` : ""}</td>
  </tr>`).join("");
  return `<h2>异常处置<span class="en">六类异常闭环</span></h2>
    <table><tr><th>#</th><th>类型</th><th>说明</th><th>状态</th><th>处置</th></tr>${incRows}</table>
    <h2>场次影响</h2>
    <table><tr><th>影厅</th><th>场次</th><th>影响</th><th>延误</th><th>来源</th></tr>${impRows}</table>
    <h2>排期<span class="en">可登记场次延误</span></h2>
    <table><tr><th>影厅</th><th>影片</th><th>开始</th><th>时长</th><th>状态</th><th></th></tr>${showRows}</table>`;
}

window.retryReset = (id) =>
  run(() => api(`/api/incidents/${id}/retry_reset`, { method: "POST" }), "计时器重置成功");
window.resolveInc = (id) => {
  const resolution = prompt("处置说明", "已处理完毕");
  if (resolution === null) return;
  run(() => api(`/api/incidents/${id}/resolve`, { body: { resolution } }), "异常已处置");
};
window.delayShow = (id) => {
  const minutes = Number(prompt("延误分钟数", "15"));
  if (!minutes) return;
  const reason = prompt("延误原因", "设备维护") || "";
  run(() => api(`/api/shows/${id}/delay`, { body: { minutes, reason } }), "已登记场次延误");
};

/* ---------------- 放行审批 ---------------- */
async function viewApprovals() {
  const [aps, wos] = await Promise.all([api("/api/approvals"), api("/api/work_orders")]);
  const apRows = aps.map((a) => `<tr>
    <td>#${a.id}</td><td>工单 #${a.work_order_id}</td><td>${esc(a.approver_name)}</td>
    <td>${badge(a.decision === "approved" ? "批准放行" : "驳回",
      a.decision === "approved" ? "green" : "red")}</td>
    <td>${esc(a.created_at)}</td><td class="muted">${esc(a.note || "")}</td>
  </tr>`).join("");
  const pending = wos.filter((w) => w.status === "pending_release");
  return `<h2>放行审批<span class="en">放映主管签署 · 换灯人不得签署本人工单</span></h2>
    ${pending.length
      ? `<p class="gold">有 ${pending.length} 张工单待放行，请前往「换灯工单」页签签署。</p>`
      : "<p class='muted'>当前无待放行工单。</p>"}
    <table><tr><th>#</th><th>工单</th><th>审批人</th><th>结论</th><th>时间</th><th>备注</th></tr>
    ${apRows || "<tr><td colspan='6' class='muted'>暂无审批记录</td></tr>"}</table>`;
}

/* ---------------- 启动 ---------------- */
render();
setInterval(() => { if (currentTab === "overview") render(); }, 30000);

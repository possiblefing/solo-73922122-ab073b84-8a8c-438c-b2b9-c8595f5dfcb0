// 操作页面 UI 层：所有竞态防护都基于 core.js 的纯逻辑。
import {
  collectInput,
  createGeneration,
  createView,
  isSuperseded,
  markDirty,
  noteSaved,
  noteServerState,
  adoptServerState,
  isResultFresh,
  guardAnalyze,
  guardDownload,
  sameInput,
  verifyDownloadedFile,
  extractError,
} from "./core.js";

const $ = (id) => document.getElementById(id);

const view = createView();
const gen = createGeneration();
const ui = { tables: [] }; // 仅承载目录编辑器的工作副本
let result = null; // 本窗口最近一次核查响应（含 version/fingerprint/status）
let inFlight = null; // 当前在途动作：'save' | 'analyze' | 'download' | null

/* ---------------- 基础请求 ---------------- */
async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opt);
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    /* 非 JSON */
  }
  return { ok: resp.ok, status: resp.status, data, headers: resp.headers };
}

function setAction(msg, isErr) {
  const el = $("actionStatus");
  el.textContent = msg;
  el.style.color = isErr ? "var(--bad)" : "var(--ok)";
}

function setBusy(action, busy) {
  inFlight = busy ? action : null;
  for (const id of ["saveBtn", "analyzeBtn", "downloadBtn", "syncBtn"]) {
    $(id).disabled = busy;
  }
}

/* ---------------- 目录编辑器 ---------------- */
function renderCatalog() {
  const root = $("catalogEditor");
  root.innerHTML = "";
  ui.tables.forEach((t, ti) => {
    const tbl = document.createElement("table");
    tbl.className = "cat";
    const head = document.createElement("tr");
    head.innerHTML = `<th colspan="3">表 ${ti + 1}：
      <input type="text" class="tname" style="display:inline-block;width:200px" value="${escapeHtml(t.name)}" placeholder="表名">
      <button type="button" class="del small">删除表</button></th>`;
    head.querySelector("input").addEventListener("input", (e) => {
      t.name = e.target.value;
      onLocalEdit();
    });
    head.querySelector(".del").addEventListener("click", () => {
      ui.tables.splice(ti, 1);
      onLocalEdit();
      renderCatalog();
    });
    tbl.appendChild(head);
    const sub = document.createElement("tr");
    sub.innerHTML = `<td class="cname"><b>列名</b></td><td><b>涉敏</b></td><td></td>`;
    tbl.appendChild(sub);
    t.columns.forEach((c, ci) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><input type="text" class="cname" value="${escapeHtml(c.name)}" placeholder="列名"></td>
        <td style="text-align:center"><input type="checkbox" ${c.sensitive ? "checked" : ""}></td>
        <td><button type="button" class="del small">删除</button></td>`;
      tr.querySelector(".cname").addEventListener("input", (e) => {
        c.name = e.target.value;
        onLocalEdit();
      });
      tr.querySelector('input[type=checkbox]').addEventListener("change", (e) => {
        c.sensitive = e.target.checked;
        onLocalEdit();
      });
      tr.querySelector(".del").addEventListener("click", () => {
        t.columns.splice(ci, 1);
        onLocalEdit();
        renderCatalog();
      });
      tbl.appendChild(tr);
    });
    const addRow = document.createElement("tr");
    addRow.innerHTML = `<td colspan="3"><button type="button" class="small">+ 添加列</button></td>`;
    addRow.querySelector("button").addEventListener("click", () => {
      t.columns.push({ name: "", sensitive: false });
      onLocalEdit();
      renderCatalog();
    });
    tbl.appendChild(addRow);
    root.appendChild(tbl);
  });
  $("addTableBtn").disabled = ui.tables.length >= 4;
}

function currentInput() {
  return collectInput(ui.tables, $("sqlEditor").value);
}

/* ---------------- 本地编辑：立即失效旧结论 ---------------- */
function onLocalEdit() {
  markDirty(view);
  $("saveBtn").classList.add("dirty");
  // 未保存的本地改动立即使旧结论失效：不清除结果卡片，但置灰并声明失效
  invalidateResult("本地输入已修改，此结论已失效，请重新保存并核查");
  refreshBadges();
  refreshConflictBanner();
}

function invalidateResult(reason) {
  const sec = $("resultSection");
  if (sec.style.display !== "none") {
    sec.classList.add("stale");
    $("resultMeta").textContent = `（${reason}）`;
  }
}

/* ---------------- 状态呈现 ---------------- */
function refreshBadges() {
  $("versionBadge").textContent =
    view.savedVersion === null ? "版本 v-" : "版本 v" + view.savedVersion;
  const b = $("analysisBadge");
  if (view.dirty) {
    b.textContent = "本地有未保存改动（结论失效）";
    b.className = "badge b-none";
    return;
  }
  if (isSuperseded(view)) {
    b.textContent = `已被其他窗口更新（服务端 v${view.serverVersion}）`;
    b.className = "badge b-error";
    return;
  }
  if (result && isResultFresh(view, result)) {
    const s = result.status;
    b.textContent =
      s === "pass"
        ? "核查通过（当前输入）"
        : s === "blocked"
          ? "涉敏拦截（当前输入）"
          : "核查报错（当前输入）";
    b.className =
      "badge " + (s === "pass" ? "b-pass" : s === "blocked" ? "b-blocked" : "b-error");
  } else {
    b.textContent = "当前输入无有效结论";
    b.className = "badge b-none";
  }
}

function refreshConflictBanner() {
  const banner = $("staleBanner");
  if (isSuperseded(view)) {
    banner.textContent =
      `检测到输入已在其他窗口更新（本窗口 v${view.savedVersion} → 服务端 v${view.serverVersion}）。` +
      "本地草稿已保留、未被覆盖；核查与下载已被拒绝。请确认后点击“刷新为当前输入”，或重新保存你的草稿。";
    banner.classList.add("show");
  }
}

/* 把服务端状态灌入编辑器（仅初始化 / 用户显式刷新时调用，轮询不自动覆盖草稿） */
function adoptState(state) {
  ui.tables = state.catalog.tables.map((t) => ({
    name: t.name,
    columns: t.columns.map((c) => ({ name: c.name, sensitive: c.sensitive })),
  }));
  $("sqlEditor").value = state.sql;
  renderCatalog();
  const savedInput = {
    catalog: { tables: state.catalog.tables },
    sql: state.sql,
  };
  adoptServerState(view, state.version, state.fingerprint, savedInput);
  $("saveBtn").classList.remove("dirty");
  if (state.analysis) {
    result = {
      version: state.version,
      fingerprint: state.fingerprint,
      status: state.analysis.status,
      result: state.analysis.result,
    };
    renderResult(result);
  } else {
    result = null;
    $("resultSection").style.display = "none";
  }
  $("staleBanner").classList.remove("show");
  refreshBadges();
}

/* ---------------- 保存 ---------------- */
async function saveInput(silent) {
  const myGen = gen.next();
  const input = currentInput();
  const baseVersion = view.savedVersion; // 乐观并发依据；首次保存前为 null
  setBusy("save", true);
  setAction("保存中…（仍可继续编辑，迟到响应不会覆盖你的草稿）", false);
  let r;
  try {
    r = await api("PUT", "/api/input", {
      catalog: input.catalog,
      sql: input.sql,
      base_version: baseVersion,
    });
  } finally {
    if (gen.isCurrent(myGen)) setBusy(null, false);
  }
  // 迟到的保存响应：已有更新一代的动作发起，绝不回写状态、不清草稿
  if (!gen.isCurrent(myGen)) {
    console.warn("忽略迟到的保存响应（世代已过期）");
    return null;
  }
  if (!r.ok) {
    const msg = extractError(r.data, "保存失败 HTTP " + r.status);
    if (r.status === 409) {
      // 被另一窗口取代：明确拒绝，保留本地草稿与编辑器内容。
      // 主动同步一次服务端事实（409 响应本身不保证携带 state）。
      const st = await api("GET", "/api/state");
      if (st.ok) noteServerState(view, st.data.version, st.data.fingerprint);
      setAction("保存被拒绝（冲突）：" + msg, true);
      refreshConflictBanner();
      refreshBadges();
    } else {
      setAction("保存失败：" + msg, true);
    }
    return null;
  }
  // 成功：以服务端回带的版本/指纹为准。若用户在等待期间继续编辑，
  // 已保存事实照常更新，但新编辑必须立即重新成为草稿、旧结论保持失效。
  const savedInput = input;
  const editedMeanwhile = sameInput(currentInput(), savedInput) === false;
  noteSaved(view, r.data.version, r.data.fingerprint, savedInput);
  if (editedMeanwhile) {
    markDirty(view);
    $("saveBtn").classList.add("dirty");
  } else {
    $("saveBtn").classList.remove("dirty");
    $("staleBanner").classList.remove("show");
  }
  if (r.data.changed) {
    invalidateResult("输入已变更并保存为 v" + r.data.version + "，旧结论已失效，请重新核查");
  }
  if (editedMeanwhile) {
    invalidateResult("保存期间又产生了新的本地编辑，旧结论仍失效");
  }
  if (!silent) {
    setAction(
      r.data.changed
        ? "已保存（v" + r.data.version + "），旧核查结论已失效，请重新核查"
        : "已保存（内容无变化，版本不变）",
      false
    );
  }
  refreshBadges();
  return r.data;
}

/* ---------------- 核查 ---------------- */
async function analyze() {
  // 本地门禁：有草稿先保存；被取代则明确拒绝
  if (view.dirty) {
    const saved = await saveInput(true);
    if (!saved) return; // 保存被拒（冲突/校验失败），草稿保留，不继续核查
  }
  const guard = guardAnalyze(view);
  if (!guard.allowed) {
    setAction("核查被拒绝：" + guard.reason, true);
    return;
  }
  const myGen = gen.next();
  const reqVersion = view.savedVersion;
  const reqFingerprint = view.savedFingerprint;
  setBusy("analyze", true);
  setAction("核查中…（仍可编辑；若期间改动，回来的结论会被判为过期）", false);
  let r;
  try {
    r = await api("POST", "/api/analyze", {
      version: reqVersion,
      fingerprint: reqFingerprint,
    });
  } finally {
    if (gen.isCurrent(myGen)) setBusy(null, false);
  }
  // 迟到响应：可能用户又保存/核查/编辑过，旧结论不得恢复
  if (!gen.isCurrent(myGen)) {
    console.warn("忽略迟到的核查响应（世代已过期）");
    return;
  }
  if (!r.ok) {
    const msg = extractError(r.data, "核查请求失败 HTTP " + r.status);
    setAction("核查被拒绝：" + msg, true);
    if (r.status === 409) {
      const st = await api("GET", "/api/state");
      if (st.ok) {
        noteServerState(view, st.data.version, st.data.fingerprint);
        refreshConflictBanner();
        refreshBadges();
      }
    }
    return;
  }
  // 响应到达时再次本地校验：等待期间若已产生草稿或被他窗推进，结论一律判过期
  if (view.dirty || isSuperseded(view)) {
    result = { ...r.data, stale: true };
    renderResult(result);
    invalidateResult("等待核查期间输入已变化，该结论为过期结论，不能用于下载");
    setAction("核查响应到达，但输入已变化：结论已判过期，禁止据此下载", true);
    refreshBadges();
    return;
  }
  result = { ...r.data, stale: false };
  renderResult(result);
  setAction("核查完成（绑定 v" + r.data.version + "，指纹一致）", false);
  refreshBadges();
}

/* ---------------- 下载 ---------------- */
async function download() {
  const guard = guardDownload(view, result);
  if (!guard.allowed) {
    setAction("禁止下载：" + guard.reason, true);
    return;
  }
  const myGen = gen.next();
  const reqVersion = view.savedVersion;
  const reqFingerprint = view.savedFingerprint;
  setBusy("download", true);
  setAction("准备下载并校验内容指纹…", false);
  let resp;
  try {
    const qs =
      "?version=" +
      encodeURIComponent(reqVersion) +
      "&fingerprint=" +
      encodeURIComponent(reqFingerprint);
    resp = await fetch("/api/download" + qs);
  } finally {
    if (gen.isCurrent(myGen)) setBusy(null, false);
  }
  if (!gen.isCurrent(myGen)) {
    console.warn("忽略迟到的下载响应（世代已过期）");
    return;
  }
  if (!resp.ok) {
    let msg = "HTTP " + resp.status;
    try {
      const d = await resp.json();
      msg = d.detail || msg;
    } catch (e) {
      /* ignore */
    }
    setAction("禁止下载：" + msg, true);
    if (resp.status === 409) {
      const st = await api("GET", "/api/state");
      if (st.ok) {
        noteServerState(view, st.data.version, st.data.fingerprint);
        refreshConflictBanner();
        refreshBadges();
      }
    }
    return;
  }
  const headerFp = resp.headers.get("X-Content-Fingerprint");
  const fileText = await resp.text();
  // 落盘前三方核验：响应头指纹 == 本地已保存指纹 == 重算指纹
  const verify = await verifyDownloadedFile(view, headerFp, fileText);
  if (!verify.ok) {
    setAction("下载已拦截：" + verify.reason, true);
    return;
  }
  // 到达这里再确认一次视图未在 await 期间变化
  if (view.dirty || isSuperseded(view) || !isResultFresh(view, result)) {
    setAction("下载已拦截：核验期间输入状态已变化，请重新核查后再下载", true);
    return;
  }
  const blob = new Blob([fileText], { type: "application/sql;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "telemetry_query.sql";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
  setAction("已下载并核验通过（v" + view.savedVersion + " 的当前查询，指纹一致）", false);
}

/* ---------------- 结果渲染 ---------------- */
function renderResult(data) {
  const sec = $("resultSection");
  sec.style.display = "block";
  sec.classList.remove("stale");
  const body = $("resultBody");
  body.innerHTML = "";
  $("resultMeta").textContent =
    "（绑定输入版本 v" + data.version + " / 指纹 " + data.fingerprint.slice(0, 10) + "…）";
  const res = data.result;
  const banner = document.createElement("div");
  if (res.status === "pass") {
    banner.innerHTML = `<div class="banner show" style="display:block;background:var(--ok-bg);color:var(--ok)">✔ 未发现涉敏来源，允许下载。输出 ${res.summary.output_columns} 列。</div>`;
  } else if (res.status === "blocked") {
    banner.innerHTML = `<div class="banner show" style="display:block;background:var(--bad-bg);color:var(--bad)">✖ 存在涉敏来源，已阻止下载。涉敏输出列：${escapeHtml(res.summary.sensitive_column_names.join("、"))}（聚合不视为脱敏）</div>`;
  } else {
    banner.innerHTML = `<div class="banner show" style="display:block;background:var(--warn-bg);color:var(--warn)">⚠ 核查未通过：SQL 被拒绝，禁止下载。</div>`;
  }
  body.appendChild(banner);
  if (res.errors && res.errors.length) {
    const el = document.createElement("div");
    el.className = "errlist";
    el.innerHTML =
      "<b>拒绝原因：</b><ul>" +
      res.errors.map((e) => `<li>${escapeHtml(e)}</li>`).join("") +
      "</ul>";
    body.appendChild(el);
  }
  (res.columns || []).forEach((col, i) => {
    const card = document.createElement("div");
    card.className = "colcard" + (col.sensitive ? " sensitive" : "");
    const agg = col.is_aggregate ? `<span class="via">含聚合（聚合不脱敏）</span>` : "";
    const sens = col.sensitive
      ? `<span class="tag-sens">涉敏</span>`
      : `<span class="tag-ok">未涉敏</span>`;
    let srcRows = col.sources
      .map(
        (s) => `
      <tr>
        <td class="mono">${escapeHtml(s.table)}.${escapeHtml(s.column)}</td>
        <td>${s.sensitive ? '<span class="tag-sens">涉敏</span>' : '<span class="tag-ok">普通</span>'}</td>
        <td>${s.via.map((v) => `<span class="via">${escapeHtml(v)}</span>`).join("")}</td>
      </tr>`
      )
      .join("");
    if (!col.sources.length)
      srcRows = `<tr><td colspan="3" class="statusline">无字段来源（常量 / COUNT(*) 等）</td></tr>`;
    card.innerHTML = `
      <div class="colhead">
        <span class="statusline">输出列 ${i + 1}</span>
        <span class="colname">${escapeHtml(col.name)}</span>
        <span class="colexpr">${escapeHtml(col.expression)}</span>
        ${agg}${sens}
      </div>
      <table class="src">
        <tr><th style="width:32%">原始来源（基表字段）</th><th style="width:12%">标记</th><th>影响路径</th></tr>
        ${srcRows}
      </table>`;
    body.appendChild(card);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* ---------------- 轮询：只观察、不覆盖草稿 ---------------- */
async function poll() {
  if (inFlight) return; // 有在途动作时跳过，避免与响应写回交错
  const r = await api("GET", "/api/state");
  if (!r.ok) {
    $("pollStatus").textContent = "状态同步失败";
    return;
  }
  const state = r.data;
  $("pollStatus").textContent = "已同步 " + new Date().toLocaleTimeString();
  const prevServer = view.serverVersion;
  noteServerState(view, state.version, state.fingerprint);
  if (view.savedVersion !== null && state.version !== view.savedVersion) {
    // 他窗已推进：提示并阻断，绝不自动覆盖本地草稿或编辑器
    refreshConflictBanner();
    invalidateResult(
      `输入已在其他窗口变更（v${view.savedVersion} → v${state.version}），此结论已失效`
    );
  } else if (prevServer !== null && prevServer === state.version && !view.dirty) {
    // 同一版本下结论可能由其他窗口刷新：仅在无本地草稿时静默同步结论徽标
    if (state.analysis && !(result && isResultFresh(view, result))) {
      result = {
        version: state.version,
        fingerprint: state.fingerprint,
        status: state.analysis.status,
        result: state.analysis.result,
      };
      renderResult(result);
    }
  }
  refreshBadges();
}

/* ---------------- 示例 ---------------- */
const DEMO_CATALOG = [
  {
    name: "events",
    columns: [
      { name: "event_id", sensitive: false },
      { name: "device_id", sensitive: false },
      { name: "ts", sensitive: false },
      { name: "event_type", sensitive: false },
      { name: "precise_lat", sensitive: true },
      { name: "precise_lng", sensitive: true },
      { name: "speed", sensitive: false },
    ],
  },
  {
    name: "devices",
    columns: [
      { name: "device_id", sensitive: false },
      { name: "model", sensitive: false },
      { name: "owner_id", sensitive: false },
      { name: "home_geohash", sensitive: true },
    ],
  },
  {
    name: "users",
    columns: [
      { name: "owner_id", sensitive: false },
      { name: "name", sensitive: false },
      { name: "phone", sensitive: true },
      { name: "city", sensitive: false },
    ],
  },
  {
    name: "regions",
    columns: [
      { name: "region_code", sensitive: false },
      { name: "region_name", sensitive: false },
    ],
  },
];
const DEMO_SQL_PASS = `WITH hourly AS (
  SELECT device_id,
         strftime('%Y-%m-%d %H:00', ts) AS hour,
         COUNT(*) AS cnt
  FROM events
  WHERE event_type = 'upload'
  GROUP BY device_id, hour
)
SELECT h.device_id, d.model, h.hour, h.cnt
FROM hourly h
JOIN devices d ON d.device_id = h.device_id
ORDER BY h.device_id, h.hour`;
const DEMO_SQL_BLOCKED = `SELECT device_id, AVG(speed) AS avg_speed, COUNT(*) AS cnt
FROM events
WHERE precise_lat > 30 AND precise_lng < 120
GROUP BY device_id`;

/* ---------------- 事件绑定 ---------------- */
$("addTableBtn").addEventListener("click", () => {
  if (ui.tables.length < 4) {
    ui.tables.push({ name: "", columns: [{ name: "", sensitive: false }] });
    onLocalEdit();
    renderCatalog();
  }
});
$("loadDemoCatalogBtn").addEventListener("click", () => {
  ui.tables = JSON.parse(JSON.stringify(DEMO_CATALOG));
  onLocalEdit();
  renderCatalog();
});
$("loadDemoPassBtn").addEventListener("click", () => {
  $("sqlEditor").value = DEMO_SQL_PASS;
  onLocalEdit();
});
$("loadDemoBlockedBtn").addEventListener("click", () => {
  $("sqlEditor").value = DEMO_SQL_BLOCKED;
  onLocalEdit();
});
$("saveBtn").addEventListener("click", () => saveInput(false));
$("analyzeBtn").addEventListener("click", analyze);
$("downloadBtn").addEventListener("click", download);
$("syncBtn").addEventListener("click", async () => {
  const myGen = gen.next();
  setBusy("sync", true);
  let r;
  try {
    r = await api("GET", "/api/state");
  } finally {
    if (gen.isCurrent(myGen)) setBusy(null, false);
  }
  // 迟到的刷新响应不得覆盖更新一代动作（保存/核查）的结果
  if (!gen.isCurrent(myGen)) {
    console.warn("忽略迟到的刷新响应（世代已过期）");
    return;
  }
  if (r.ok) {
    if (view.dirty) {
      // 有本地草稿时不静默覆盖：要求用户二次确认，确认后草稿才被服务端内容取代
      const ok = window.confirm(
        "当前窗口存在未保存的本地草稿。刷新为服务端当前输入将丢弃这份草稿，确定继续吗？"
      );
      if (!ok) {
        setAction("已取消刷新，本地草稿已保留", false);
        return;
      }
    }
    adoptState(r.data);
    setAction(
      "已刷新为服务端当前输入（v" + r.data.version + "），可重新保存、核查、下载",
      false
    );
  }
});
$("sqlEditor").addEventListener("input", onLocalEdit);

/* ---------------- 初始化 ---------------- */
(async function init() {
  const r = await api("GET", "/api/state");
  if (r.ok) adoptState(r.data);
  setInterval(poll, 3000);
})();

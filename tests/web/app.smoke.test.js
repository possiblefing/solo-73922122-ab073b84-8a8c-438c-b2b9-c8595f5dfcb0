// 浏览器环境冒烟测试：用最小 DOM/fetch 桩加载真实 app.js，
// 验证模块导入链、初始化、按钮事件绑定以及“迟到响应不覆盖草稿”的真实接线。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const APP_DIR = path.resolve("app/static/app");

function makeEl(id) {
  const listeners = {};
  const el = {
    id,
    value: "",
    textContent: "",
    style: { color: "", display: "" },
    disabled: false,
    innerHTML: "",
    classList: {
      _set: new Set(),
      add(c) {
        this._set.add(c);
      },
      remove(c) {
        this._set.delete(c);
      },
      contains(c) {
        return this._set.has(c);
      },
    },
    appendChild() {},
    addEventListener(ev, fn) {
      (listeners[ev] ||= []).push(fn);
    },
    dispatch(ev, payload) {
      for (const fn of listeners[ev] || []) fn(payload || { target: el });
    },
    // 极简桩：不真正解析 HTML，返回一个新元素即可满足 renderCatalog 的接线
    querySelector() {
      return makeEl(id + "-q");
    },
  };
  return el;
}

function makeHarness() {
  const els = {};
  const ids = [
    "actionStatus", "addTableBtn", "analysisBadge", "analyzeBtn", "catalogEditor",
    "downloadBtn", "loadDemoBlockedBtn", "loadDemoCatalogBtn", "loadDemoPassBtn",
    "pollStatus", "resultBody", "resultMeta", "resultSection", "saveBtn",
    "sqlEditor", "staleBanner", "syncBtn", "versionBadge",
  ];
  for (const id of ids) els[id] = makeEl(id);
  const created = [];
  globalThis.document = {
    getElementById: (id) => els[id],
    createElement: () => {
      const el = makeEl("dyn-" + created.length);
      created.push(el);
      return el;
    },
    body: { appendChild() {} },
  };
  globalThis.window = { confirm: () => true };
  globalThis.setInterval = () => 0;
  globalThis.URL = { createObjectURL: () => "blob:x", revokeObjectURL() {} };
  globalThis.Blob = class {};
  return { els, created };
}

// 可控的服务端桩：记录请求，支持制造 409 与延迟
function installFetch(route) {
  globalThis.fetch = async (url, opt) => route(url, opt || {});
  function jsonResponse(status, data, headers = {}) {
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: (k) => headers[k] ?? null },
      async json() {
        return data;
      },
      async text() {
        return JSON.stringify(data);
      },
    };
  }
  return { jsonResponse };
}

test("app.js 初始化并绑定按钮事件，渲染初始状态", async () => {
  makeHarness();
  const { jsonResponse } = installFetch(async () =>
    jsonResponse(200, {
      version: 0,
      fingerprint: "",
      catalog: { tables: [] },
      sql: "",
      analysis: null,
    })
  );
  const appUrl = pathToFileURL(path.join(APP_DIR, "app.js")).href + "?t=" + Date.now();
  await import(appUrl);
  await new Promise((r) => setTimeout(r, 10));
  // 初始化后版本徽标被更新
  const badge = globalThis.document.getElementById("versionBadge");
  assert.equal(badge.textContent, "版本 v0");
});

test("保存返回 409 时保留本地草稿、提示冲突、不覆盖编辑器", async () => {
  const h = makeHarness();
  let stateCalls = 0;
  const { jsonResponse } = installFetch(async (url, opt) => {
    if (url.endsWith("/api/state")) {
      stateCalls += 1;
      // 首次初始化看到 v1；保存冲突后再拉取时服务端已是 v2
      const version = stateCalls === 1 ? 1 : 2;
      return jsonResponse(200, {
        version,
        fingerprint: "srv" + version,
        catalog: { tables: [] },
        sql: version === 1 ? "" : "Y",
        analysis: null,
      });
    }
    if (opt.method === "PUT") {
      // 本次保存因 base_version=1 而服务端已到 v2 被拒
      return jsonResponse(409, {
        detail: "保存被拒绝：该窗口依据的是 v1，当前已被其他窗口更新到 v2。",
      });
    }
    return jsonResponse(500, {});
  });
  const appUrl = pathToFileURL(path.join(APP_DIR, "app.js")).href + "?t=" + Date.now();
  await import(appUrl);
  await new Promise((r) => setTimeout(r, 10));
  // 用户在编辑器里输入草稿 X
  const sql = h.els.sqlEditor;
  sql.value = "X";
  sql.dispatch("input");
  assert.equal(h.els.saveBtn.classList.contains("dirty"), true);
  // 点击保存 → 409
  h.els.saveBtn.dispatch("click");
  await new Promise((r) => setTimeout(r, 20));
  // 草稿保留：编辑器仍是 X，按钮仍标记 dirty，动作条显示冲突
  assert.equal(sql.value, "X");
  assert.equal(h.els.saveBtn.classList.contains("dirty"), true);
  assert.match(h.els.actionStatus.textContent, /保存被拒绝/);
  // 409 后前端拉取 state 看到 v2，横幅提示他窗更新
  assert.match(h.els.staleBanner.textContent, /其他窗口/);
});

test("迟到的保存响应不清除在途期间产生的新草稿", async () => {
  const h = makeHarness();
  let resolveSave;
  const { jsonResponse } = installFetch(async (url, opt) => {
    if (url.endsWith("/api/state")) {
      return jsonResponse(200, {
        version: 1, fingerprint: "fp1",
        catalog: { tables: [{ name: "t", columns: [{ name: "a", sensitive: false }] }] },
        sql: "X", analysis: null,
      });
    }
    if (opt.method === "PUT") {
      await new Promise((res) => (resolveSave = res));
      return jsonResponse(200, {
        version: 1, changed: false, fingerprint: "fp1",
        state: { version: 1, fingerprint: "fp1", catalog: { tables: [] }, sql: "X", analysis: null },
      });
    }
    return jsonResponse(500, {});
  });
  const appUrl = pathToFileURL(path.join(APP_DIR, "app.js")).href + "?t=" + Date.now();
  await import(appUrl);
  await new Promise((r) => setTimeout(r, 10));
  h.els.sqlEditor.value = "X";
  h.els.sqlEditor.dispatch("input");
  h.els.saveBtn.dispatch("click"); // 发起保存（挂起）
  await new Promise((r) => setTimeout(r, 5));
  // 等待期间继续编辑成新草稿 X2
  h.els.sqlEditor.value = "X2";
  h.els.sqlEditor.dispatch("input");
  resolveSave(); // 让迟到的保存响应返回
  await new Promise((r) => setTimeout(r, 20));
  // 新草稿 X2 必须仍在，且仍是 dirty（旧结论保持失效）
  assert.equal(h.els.sqlEditor.value, "X2");
  assert.equal(h.els.saveBtn.classList.contains("dirty"), true);
});

// 确保 app.js 源文件确实包含世代与指纹接线，防止桩测试漏掉真实代码
test("源码包含关键竞态防护接线", () => {
  const src = readFileSync(path.join(APP_DIR, "app.js"), "utf8");
  for (const token of [
    "gen.next()", "gen.isCurrent", "base_version", "fingerprint",
    "guardDownload", "verifyDownloadedFile", "isSuperseded", "noteServerState",
  ]) {
    assert.ok(src.includes(token), `缺少接线：${token}`);
  }
});

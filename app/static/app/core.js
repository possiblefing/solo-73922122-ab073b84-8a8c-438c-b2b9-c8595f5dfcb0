// 可独立单测的纯逻辑：规范指纹、请求世代、结论有效性、操作门禁。
// 不触碰 DOM，不发请求，可在 Node 与浏览器中以 ES 模块方式复用。
"use strict";

/* ------------------------------------------------------------------ *
 * 规范序列化（必须与后端 app/concurrency.py 的 canonical_bytes 对齐）：
 *   JSON.stringify([ tables, sql ])
 *   tables = [[表名, [[列名, 0|1], ...]], ...]
 * 名称在采集时已 trim；布尔转为 0/1；紧凑无空白。
 * ------------------------------------------------------------------ */
function canonicalText(input) {
  const tables = (input.catalog.tables || []).map((t) => [
    t.name,
    (t.columns || []).map((c) => [c.name, c.sensitive ? 1 : 0]),
  ]);
  return JSON.stringify([tables, input.sql]);
}

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  if (globalThis.crypto && globalThis.crypto.subtle) {
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }
  // Node 兜底（浏览器始终有 crypto.subtle；此处仅为可移植性）
  const { createHash } = await import("node:crypto");
  return createHash("sha256").update(Buffer.from(bytes)).digest("hex");
}

async function canonicalFingerprint(input) {
  return sha256Hex(canonicalText(input));
}

/* 同步的内容相等判断（规范文本比较），用于“等待期间是否又被编辑”这类即时判定 */
function sameInput(a, b) {
  return canonicalText(a) === canonicalText(b);
}

/* 采集编辑器内容：trim 名称、规整布尔，得到可序列化的输入 */
function collectInput(tables, sql) {
  return {
    catalog: {
      tables: tables.map((t) => ({
        name: t.name.trim(),
        columns: t.columns.map((c) => ({
          name: c.name.trim(),
          sensitive: !!c.sensitive,
        })),
      })),
    },
    sql,
  };
}

/* ------------------------------------------------------------------ *
 * 请求世代（request generation）：
 * 每个会改变状态的动作（保存/核查/下载）分配单调递增的世代号。
 * 响应回来时只认“仍是最新一代”的结果；迟到的旧世代响应一律忽略其
 * 状态写回，从而不能清除新草稿、不能恢复过期结论。
 * ------------------------------------------------------------------ */
function createGeneration() {
  let current = 0;
  return {
    next() {
      current += 1;
      return current;
    },
    isCurrent(gen) {
      return gen === current;
    },
    value() {
      return current;
    },
  };
}

/* ------------------------------------------------------------------ *
 * 窗口视图状态机
 *   savedVersion/savedFingerprint：本窗口“已保存并认知”的版本与指纹
 *   serverVersion/serverFingerprint：轮询/响应观察到的服务端当前状态
 *   dirty：本地是否存在未保存改动（草稿）
 * 不变量：
 *   - dirty 一旦为 true，旧结论立即对本窗口失效，直到重新保存+核查；
 *   - 服务端版本领先于本窗口已保存版本 => 已被另一窗口取代。
 * ------------------------------------------------------------------ */
function createView() {
  return {
    savedVersion: null,
    savedFingerprint: null,
    savedInput: null, // 已保存输入快照 {catalog:{tables}, sql}，用于下载文件重算指纹
    serverVersion: null,
    serverFingerprint: null,
    dirty: false,
  };
}

function isSuperseded(view) {
  return (
    view.savedVersion !== null &&
    view.serverVersion !== null &&
    view.serverVersion !== view.savedVersion
  );
}

function markDirty(view) {
  view.dirty = true;
}

/* 一次成功保存后更新视图（内容指纹以服务端回带为准，保证两端一致） */
function noteSaved(view, version, fingerprint, savedInput) {
  view.savedVersion = version;
  view.savedFingerprint = fingerprint;
  view.savedInput = savedInput;
  view.serverVersion = version;
  view.serverFingerprint = fingerprint;
  view.dirty = false;
}

/* 轮询/迟到响应观察到服务端状态；不修改草稿，只记录服务端事实 */
function noteServerState(view, version, fingerprint) {
  view.serverVersion = version;
  view.serverFingerprint = fingerprint;
}

/* 用户显式“刷新为当前输入”后，视图对齐服务端 */
function adoptServerState(view, version, fingerprint, savedInput) {
  view.savedVersion = version;
  view.savedFingerprint = fingerprint;
  view.savedInput = savedInput;
  view.serverVersion = version;
  view.serverFingerprint = fingerprint;
  view.dirty = false;
}

/* ------------------------------------------------------------------ *
 * 核查结论有效性
 * 结论只在同时满足以下条件时对本窗口有效：
 *   1. 存在结论且其版本/指纹等于本窗口已保存版本/指纹；
 *   2. 本地无未保存改动（dirty）；
 *   3. 服务端未被另一窗口推进到更新版本。
 * 任一不满足 => 结论失效（不得据此放行下载、不得展示为当前通过）。
 * ------------------------------------------------------------------ */
function isResultFresh(view, result) {
  if (!result) return false;
  if (view.dirty) return false;
  if (isSuperseded(view)) return false;
  if (view.savedVersion === null || result.version !== view.savedVersion) {
    return false;
  }
  if (
    view.savedFingerprint !== null &&
    result.fingerprint !== undefined &&
    result.fingerprint !== null &&
    result.fingerprint !== view.savedFingerprint
  ) {
    return false;
  }
  return true;
}

/* ------------------------------------------------------------------ *
 * 操作门禁：保存 / 核查 / 下载前的本地预检。
 * 返回 { allowed, reason }；reason 为 null 表示放行。
 * 注意：等待响应期间仍允许编辑，故这里不根据 in-flight 阻断编辑，
 * 仅阻断会引发并发写的动作按钮（由 UI 层按世代处理）。
 * ------------------------------------------------------------------ */
function guardSave(view) {
  // 保存本身总是允许尝试；冲突由服务端 base_version 校验拒绝，草稿保留
  return { allowed: true, reason: null };
}

function guardAnalyze(view) {
  if (view.dirty) {
    return {
      allowed: false,
      reason: "存在未保存的本地改动，旧结论已失效；请先保存输入再核查",
    };
  }
  if (isSuperseded(view)) {
    return {
      allowed: false,
      reason:
        "输入已在其他窗口被更新并取代本窗口版本，请先“刷新为当前输入”或重新保存后再核查",
    };
  }
  return { allowed: true, reason: null };
}

function guardDownload(view, result) {
  if (view.dirty) {
    return {
      allowed: false,
      reason: "本地有未保存改动，旧核查结论已失效，禁止下载；请重新保存并核查",
    };
  }
  if (isSuperseded(view)) {
    return {
      allowed: false,
      reason:
        "当前已保存输入已被其他窗口更新，不能下载其他查询；请刷新后重新保存、核查",
    };
  }
  if (!isResultFresh(view, result)) {
    return {
      allowed: false,
      reason: "当前输入没有有效的核查通过结论，请先核查通过再下载",
    };
  }
  if (result.status !== "pass") {
    return {
      allowed: false,
      reason: `当前输入核查结论为 ${result.status}，禁止下载`,
    };
  }
  return { allowed: true, reason: null };
}

/* 下载落盘前的内容校验：响应头指纹、本地已保存指纹、按“服务端目录 + 下载到
 * 的 SQL”重算出的指纹三方一致，任何不一致都拦截，绝不保存不匹配文件。 */
async function verifyDownloadedFile(view, headerFingerprint, fileText) {
  if (!headerFingerprint || headerFingerprint !== view.savedFingerprint) {
    return { ok: false, reason: "下载文件的指纹与本窗口已保存版本不一致，已拦截" };
  }
  if (!view.savedInput) {
    return { ok: false, reason: "缺少本窗口已保存输入快照，无法核验下载内容，已拦截" };
  }
  const actual = await canonicalFingerprint({
    catalog: view.savedInput.catalog,
    sql: fileText,
  });
  if (actual !== view.savedFingerprint) {
    return { ok: false, reason: "下载内容重算指纹不匹配，可能不是当前查询，已拦截" };
  }
  return { ok: true, reason: null };
}

/* 从 FastAPI 的 409/4xx 响应中提取中文拒绝原因 */
function extractError(data, fallback) {
  if (data && data.detail) {
    return Array.isArray(data.detail)
      ? data.detail.join("；")
      : String(data.detail);
  }
  return fallback;
}

export {
  canonicalText,
  sameInput,
  sha256Hex,
  canonicalFingerprint,
  collectInput,
  createGeneration,
  createView,
  isSuperseded,
  markDirty,
  noteSaved,
  noteServerState,
  adoptServerState,
  isResultFresh,
  guardSave,
  guardAnalyze,
  guardDownload,
  verifyDownloadedFile,
  extractError,
};

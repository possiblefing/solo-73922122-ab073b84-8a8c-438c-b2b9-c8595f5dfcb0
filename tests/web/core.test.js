// 前端纯逻辑单测：node --test tests/web/core.test.js
// 运行：node --test tests/web/
import test from "node:test";
import assert from "node:assert/strict";

import {
  canonicalText,
  canonicalFingerprint,
  sameInput,
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
  verifyDownloadedFile,
  extractError,
} from "../../app/static/app/core.js";

const CAT = {
  tables: [
    {
      name: "events",
      columns: [
        { name: "event_id", sensitive: false },
        { name: "precise_lat", sensitive: true },
      ],
    },
  ],
};
const inputA = { catalog: CAT, sql: "SELECT event_id FROM events" };
const inputB = { catalog: CAT, sql: "SELECT precise_lat FROM events" };

test("规范序列化：紧凑、布尔转 0/1、键顺序固定", () => {
  assert.equal(
    canonicalText(inputA),
    JSON.stringify([
      [["events", [["event_id", 0], ["precise_lat", 1]]]],
      "SELECT event_id FROM events",
    ])
  );
});

test("sameInput：内容相等与不等", () => {
  assert.equal(sameInput(inputA, { ...inputA }), true);
  assert.equal(sameInput(inputA, inputB), false);
  // trim 后相等
  const spaced = {
    catalog: {
      tables: [
        {
          name: " events ",
          columns: [{ name: " event_id ", sensitive: false }],
        },
      ],
    },
    sql: "x",
  };
  const trimmed = collectInput(
    [{ name: " events ", columns: [{ name: " event_id ", sensitive: false }] }],
    "x"
  );
  assert.equal(
    sameInput(trimmed, {
      catalog: { tables: [{ name: "events", columns: [{ name: "event_id", sensitive: false }] }] },
      sql: "x",
    }),
    true
  );
  void spaced;
});

test("指纹：确定且不同内容不同指纹", async () => {
  const fp1 = await canonicalFingerprint(inputA);
  const fp2 = await canonicalFingerprint(inputA);
  const fp3 = await canonicalFingerprint(inputB);
  assert.match(fp1, /^[0-9a-f]{64}$/);
  assert.equal(fp1, fp2);
  assert.notEqual(fp1, fp3);
});

test("请求世代：迟到的旧世代被识别为过期", () => {
  const g = createGeneration();
  const first = g.next();
  const second = g.next();
  assert.equal(g.isCurrent(first), false); // 第一世代已迟到
  assert.equal(g.isCurrent(second), true);
});

test("视图：被另一窗口取代的判定", () => {
  const v = createView();
  assert.equal(isSuperseded(v), false);
  noteSaved(v, 1, "fp1", inputA);
  assert.equal(isSuperseded(v), false);
  noteServerState(v, 2, "fp2"); // 他窗保存推进到 v2
  assert.equal(isSuperseded(v), true);
});

test("本地草稿立即失效旧结论", () => {
  const v = createView();
  noteSaved(v, 1, "fp1", inputA);
  const result = { version: 1, fingerprint: "fp1", status: "pass" };
  assert.equal(isResultFresh(v, result), true);
  markDirty(v); // 未保存的本地改动
  assert.equal(isResultFresh(v, result), false);
  const g = guardDownload(v, result);
  assert.equal(g.allowed, false);
  assert.match(g.reason, /未保存/);
});

test("他窗更新后：核查与下载都被门禁拒绝，且草稿仍在", () => {
  const v = createView();
  noteSaved(v, 1, "fp1", inputA);
  const result = { version: 1, fingerprint: "fp1", status: "pass" };
  markDirty(v); // 本地还有草稿
  noteServerState(v, 2, "fp2"); // 他窗推进
  assert.equal(v.dirty, true); // 草稿不被清除
  assert.equal(guardAnalyze(v).allowed, false);
  assert.equal(guardDownload(v, result).allowed, false);
});

test("结论版本/指纹不匹配即失效，不能恢复过期通过", () => {
  const v = createView();
  noteSaved(v, 2, "fp2", inputB);
  // 一个迟到的 v1 旧通过响应
  const stale = { version: 1, fingerprint: "fp1", status: "pass" };
  assert.equal(isResultFresh(v, stale), false);
  assert.equal(guardDownload(v, stale).allowed, false);
  // 同版本但指纹不符也失效
  const fpMismatch = { version: 2, fingerprint: "other", status: "pass" };
  assert.equal(isResultFresh(v, fpMismatch), false);
});

test("非 pass 结论禁止下载", () => {
  const v = createView();
  noteSaved(v, 1, "fp1", inputA);
  const blocked = { version: 1, fingerprint: "fp1", status: "blocked" };
  const g = guardDownload(v, blocked);
  assert.equal(g.allowed, false);
  assert.match(g.reason, /blocked/);
});

test("刷新采用服务端状态后可重新核查，保留的判定由服务端结论承载", () => {
  const v = createView();
  noteSaved(v, 1, "fp1", inputA);
  noteServerState(v, 2, "fp2");
  assert.equal(isSuperseded(v), true);
  adoptServerState(v, 2, "fp2", inputB);
  assert.equal(isSuperseded(v), false);
  assert.equal(v.dirty, false);
  assert.equal(guardAnalyze(v).allowed, true);
});

test("下载文件三方核验：指纹不一致被拦截，一致才放行", async () => {
  const v = createView();
  noteSaved(v, 1, await canonicalFingerprint(inputA), inputA);
  // 头指纹正确但文件内容被换成别的 SQL
  const goodFp = v.savedFingerprint;
  const tampered = await verifyDownloadedFile(v, goodFp, "SELECT * FROM secrets");
  assert.equal(tampered.ok, false);
  // 头指纹缺失/错误
  const badHeader = await verifyDownloadedFile(v, "deadbeef", inputA.sql);
  assert.equal(badHeader.ok, false);
  // 全部一致
  const ok = await verifyDownloadedFile(v, goodFp, inputA.sql);
  assert.equal(ok.ok, true);
});

test("extractError：数组/字符串/缺省", () => {
  assert.equal(extractError({ detail: ["a", "b"] }, "x"), "a；b");
  assert.equal(extractError({ detail: "boom" }, "x"), "boom");
  assert.equal(extractError(null, "fallback"), "fallback");
});

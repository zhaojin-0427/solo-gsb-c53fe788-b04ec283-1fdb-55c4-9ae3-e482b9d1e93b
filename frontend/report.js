"use strict";

const $ = (s) => document.querySelector(s);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function row(cells, tag = "td") {
  return "<tr>" + cells.map((c) => `<${tag}>${c}</${tag}>`).join("") + "</tr>";
}

async function main() {
  const id = new URLSearchParams(location.search).get("id");
  if (!id) {
    $("#rptBody").innerHTML = '<p class="error-box">缺少报告 id 参数（?id=N）。</p>';
    return;
  }
  let p;
  try {
    const res = await fetch(`/api/reports/${id}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    p = await res.json();
  } catch (err) {
    $("#rptBody").innerHTML = `<p class="error-box">无法加载报告：${esc(err.message)}</p>`;
    return;
  }

  const meta = p.report;
  document.title = `共识报告 v${meta.report_version} - ${meta.document_name}`;
  $("#rptTitle").textContent = `v${meta.report_version} · ${meta.document_name}`;
  $("#rptHash").textContent = "SHA-256 " + p.sha256.slice(0, 20) + "…";
  $("#rptDownload").href = `/api/reports/${id}/download`;

  const snap = p.input_snapshot;
  const alg = p.algorithm;
  const result = p.result;

  const metaRows = [
    ["报告版本", `v${meta.report_version}`],
    ["生成时间 (UTC)", meta.generated_at],
    ["应用", `${meta.app_name} v${meta.app_version}`],
    ["求解器", `OR-Tools ${meta.ortools_version}`],
    ["项目", `#${meta.project_id} ${meta.project_name}`],
    ["文档", `#${meta.document_id} ${meta.document_name}`],
    ["裁决编号", `#${meta.adjudication_id}`],
    ["不可变", meta.immutable ? "是（重新导入/改规则均不会改写本报告）" : "否"],
    ["SHA-256", p.sha256],
  ];

  const annotatorTable = `<table><thead>${row(["ID", "姓名", "权重"], "th")}</thead><tbody>${
    snap.annotators.map((a) => row([a.id, esc(a.name), a.weight])).join("")}</tbody></table>`;

  const relTable = `<table><thead>${row(["外层/标签A", "关系", "内层/标签B"], "th")}</thead><tbody>${
    snap.label_relations.map((r) =>
      row([esc(r.outer_label_name),
        r.kind === "nested" ? "⊇ 可嵌套" : "⨯ 互斥",
        esc(r.inner_label_name)])).join("") ||
    '<tr><td colspan="3" class="hint">无显式关系（不同标签默认互斥）</td></tr>'
  }</tbody></table>`;

  const inputTable = `<table><thead>${row(
    ["ID", "标注者(权重)", "标签", "区间", "原文"], "th")}</thead><tbody>${
    snap.annotations.map((a) => row([
      a.id,
      `${esc(a.annotator_name)} (w${a.weight})`,
      esc(a.label_name),
      `[${a.start},${a.end})`,
      `<span class="snippet">${esc(snap.document.content.slice(a.start, a.end))}</span>`,
    ])).join("")}</tbody></table>`;

  const candTable = `<table><thead>${row(
    ["区间", "标签", "得分", "是否采纳", "强制", "支持者"], "th")}</thead><tbody>${
    result.candidates.map((c) => row([
      `[${c.start},${c.end})`,
      esc(c.label_name),
      c.score,
      c.adopted ? "✅" : "—",
      c.forced === "force_include" ? "强制采纳" : c.forced === "force_exclude" ? "强制驳回" : "",
      c.supporters.map((s) => `${esc(s.annotator_name)}(w${s.weight}×${s.jaccard})`).join("，"),
    ])).join("")}</tbody></table>`;

  const rejectedTable = result.rejected_annotations.length
    ? `<table><thead>${row(["标注者", "标签", "区间", "原文", "同标签最大重叠", "支持者"], "th")}</thead><tbody>${
      result.rejected_annotations.map((a) => row([
        esc(a.annotator_name), esc(a.label_name), `[${a.start},${a.end})`,
        `<span class="snippet">${esc(snap.document.content.slice(a.start, a.end))}</span>`,
        a.same_label_max_jaccard,
        a.supporters.map((s) => `${esc(s.annotator_name)}(w${s.weight}×${s.jaccard})`).join("，") || "—",
      ])).join("")}</tbody></table>`
    : '<p class="hint">无未采纳标注。</p>';

  const overrideTable = result.overrides.length
    ? `<table><thead>${row(["区间", "标签", "动作", "理由"], "th")}</thead><tbody>${
      result.overrides.map((o) => {
        const label = snap.labels.find((l) => l.id === o.label_id);
        return row([`[${o.start},${o.end})`, esc(label ? label.name : o.label_id),
          o.action === "force_include" ? "强制采纳" : "强制驳回", esc(o.reason)]);
      }).join("")}</tbody></table>`
    : '<p class="hint">裁决者未作人工覆盖。</p>';

  $("#rptBody").innerHTML = `
  <section>
    <h2>① 报告元数据</h2>
    <table class="meta"><tbody>${metaRows.map(([k, v]) =>
      `<tr><th>${esc(k)}</th><td class="${k === "SHA-256" ? "mono" : ""}">${esc(v)}</td></tr>`).join("")}</tbody></table>
  </section>

  <section>
    <h2>② 算法参数</h2>
    <table class="meta"><tbody>
      <tr><th>候选与计分</th><td>${esc(alg.scoring)}</td></tr>
      <tr><th>优化模型</th><td>${esc(alg.optimization)}</td></tr>
      <tr><th>约束</th><td>${esc(alg.constraints)}</td></tr>
      <tr><th>平局规则</th><td>${esc(alg.tie_breaker)}</td></tr>
      <tr><th>整数放大系数</th><td>${alg.score_scale}</td></tr>
      <tr><th>CP-SAT 参数</th><td class="mono">${esc(JSON.stringify(alg.solver_params))}</td></tr>
      <tr><th>最优总得分</th><td>${result.objective}（整数目标 ${result.objective_int}）</td></tr>
    </tbody></table>
  </section>

  <section>
    <h2>③ 输入快照</h2>
    <h3>文档（UTF-8）</h3>
    <p>名称：${esc(snap.document.name)}；长度：${snap.document.content.length} 字符；词元：${snap.document.tokens.length} 个</p>
    <pre class="doc-content">${esc(snap.document.content)}</pre>
    <details><summary>词元边界 JSON（${snap.document.tokens.length}）</summary>
      <pre class="mono small">${esc(JSON.stringify(snap.document.tokens))}</pre></details>
    <h3>标签</h3>
    <p>${snap.labels.map((l) => esc(l.name)).join("、")}</p>
    <h3>标签关系</h3>${relTable}
    <h3>标注者</h3>${annotatorTable}
    <h3>输入标注（${snap.annotations.length} 条）</h3>${inputTable}
  </section>

  <section>
    <h2>④ 共识结果</h2>
    <h3>候选与得分</h3>${candTable}
    <h3>裁决者覆盖</h3>${overrideTable}
    <h3>未采纳标注及其支持者（${result.rejected_annotations.length} 条）</h3>${rejectedTable}
  </section>
  `;
}

main();

"use strict";

// ── 全局状态 ───────────────────────────────────────────────────────────
const state = {
  projects: [],
  projectId: null,
  overview: null, // {project, documents, labels, relations, annotators}
  documents: new Map(), // id -> doc (含 content/tokens)
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const msg = (data && data.detail) ? JSON.stringify(data.detail) : `HTTP ${res.status}`;
    const extra = data && data.errors ? "\n" + data.errors.join("\n") : "";
    throw new Error(msg + extra);
  }
  return data;
}

function toast(msg, ok = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show " + (ok ? "ok" : "err");
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = ""), 4200);
}

// ── Tab 切换 ───────────────────────────────────────────────────────────
$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  $$("main .tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + btn.dataset.tab));
  refreshActiveTab(btn.dataset.tab);
});

function refreshActiveTab(tab) {
  if (!state.projectId) return;
  if (tab === "docs") renderDocs();
  if (tab === "labels") renderLabels();
  if (tab === "annotators") renderAnnotators();
  if (tab === "annotations") initAnnotationsTab();
  if (tab === "adjudicate") initAdjudicateTab();
  if (tab === "reports") renderReports();
}

// ── 项目 ───────────────────────────────────────────────────────────────
async function loadProjects(keepId) {
  state.projects = await api("GET", "/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = "";
  if (!state.projects.length) {
    const o = document.createElement("option");
    o.textContent = "（无项目，请新建）";
    o.value = "";
    sel.appendChild(o);
    state.projectId = null;
    return;
  }
  for (const p of state.projects) {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = `#${p.id} ${p.name}`;
    sel.appendChild(o);
  }
  state.projectId = (keepId && state.projects.some((p) => p.id === keepId))
    ? keepId : state.projects[0].id;
  sel.value = state.projectId;
  await loadOverview();
}

$("#projectSelect").addEventListener("change", async (e) => {
  state.projectId = Number(e.target.value) || null;
  if (state.projectId) await loadOverview();
});

$("#newProjectBtn").addEventListener("click", () => $("#projectDialog").showModal());
$("#projectDialogForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  const name = new FormData(e.target).get("name");
  try {
    const p = await api("POST", "/api/projects", { name });
    e.target.reset();
    await loadProjects(p.id);
    toast(`项目 #${p.id} 已创建`, true);
  } catch (err) {
    toast(err.message);
    e.preventDefault();
  }
});

async function loadOverview() {
  if (!state.projectId) return;
  state.overview = await api("GET", `/api/projects/${state.projectId}`);
  const active = $$("#tabs button.active")[0].dataset.tab;
  refreshActiveTab(active);
}

// ── ① 文档 ─────────────────────────────────────────────────────────────
$("#docForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.projectId) return toast("请先创建/选择项目");
  const fd = new FormData(e.target);
  try {
    const r = await api("POST", `/api/projects/${state.projectId}/documents`, fd);
    toast(`文档「${r.name}」已导入：${r.content_length} 字符，${r.token_count} 个词元`, true);
    e.target.reset();
    await loadOverview();
  } catch (err) {
    toast(err.message);
  }
});

async function renderDocs() {
  const ov = state.overview;
  const box = $("#docList");
  if (!ov.documents.length) { box.innerHTML = '<p class="hint">尚未导入文档。</p>'; return; }
  box.innerHTML = `<table><thead><tr><th>ID</th><th>名称</th><th>长度</th><th>状态</th><th>导入时间(UTC)</th><th></th></tr></thead>
  <tbody>${ov.documents.map((d) => `<tr>
    <td>${d.id}</td><td>${esc(d.name)}</td><td>${d.content_length}</td>
    <td>${d.is_active ? '<span class="tag ok">当前</span>' : '<span class="tag">旧版</span>'}</td>
    <td>${d.imported_at}</td>
    <td><button data-del-doc="${d.id}">删除</button></td>
  </tr>`).join("")}</tbody></table>`;
  box.onclick = async (e) => {
    const id = e.target.dataset?.delDoc;
    if (!id || !confirm("删除该文档？（已产生裁决/报告的文档禁止删除）")) return;
    try {
      await api("DELETE", `/api/documents/${id}`);
      await loadOverview();
      toast("已删除", true);
    } catch (err) { toast(err.message); }
  };
}

// ── ② 标签与关系 ───────────────────────────────────────────────────────
$("#labelForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = new FormData(e.target).get("name");
  try {
    await api("POST", `/api/projects/${state.projectId}/labels`, { name });
    e.target.reset();
    await loadOverview();
  } catch (err) { toast(err.message); }
});

function labelOptions(selected) {
  return state.overview.labels
    .map((l) => `<option value="${l.id}" ${l.id === selected ? "selected" : ""}>${esc(l.name)}</option>`)
    .join("");
}

function renderLabels() {
  const labels = state.overview.labels;
  $("#labelList").innerHTML = labels.length
    ? `<table><thead><tr><th>ID</th><th>名称</th><th></th></tr></thead><tbody>${
        labels.map((l) => `<tr><td>${l.id}</td><td>${esc(l.name)}</td>
        <td><button data-del-label="${l.id}">删除</button></td></tr>`).join("")
      }</tbody></table>`
    : '<p class="hint">尚无标签。</p>';

  const f = $("#relationForm");
  f.elements.outer_id.innerHTML = labelOptions();
  f.elements.inner_id.innerHTML = labelOptions();
  f.style.display = labels.length >= 2 ? "" : "none";

  const nameOf = (id) => {
    const l = labels.find((x) => x.id === id);
    return l ? l.name : "#" + id;
  };
  $("#relationList").innerHTML = state.overview.relations.length
    ? `<table><thead><tr><th>外层/标签A</th><th>关系</th><th>内层/标签B</th><th></th></tr></thead><tbody>${
        state.overview.relations.map((r) => `<tr>
          <td>${esc(nameOf(r.outer_id))}</td>
          <td>${r.kind === "nested" ? "⊇ 可嵌套" : "⨯ 互斥"}</td>
          <td>${esc(nameOf(r.inner_id))}</td>
          <td><button data-del-rel="${r.id}">删除</button></td></tr>`).join("")
      }</tbody></table>`
    : '<p class="hint">未声明任何嵌套关系；此时所有不同标签重叠均按互斥处理。</p>';
}

$("#labelList").addEventListener("click", async (e) => {
  const id = e.target.dataset?.delLabel;
  if (!id || !confirm("删除该标签将同时删除使用它的标注，确认？")) return;
  try { await api("DELETE", `/api/labels/${id}`); await loadOverview(); }
  catch (err) { toast(err.message); }
});

$("#relationForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    outer_id: Number(fd.get("outer_id")),
    inner_id: Number(fd.get("inner_id")),
    kind: fd.get("kind"),
  };
  try {
    await api("POST", `/api/projects/${state.projectId}/relations`, body);
    await loadOverview();
    toast("关系已添加", true);
  } catch (err) { toast(err.message); }
});

$("#relationList").addEventListener("click", async (e) => {
  const id = e.target.dataset?.delRel;
  if (!id) return;
  try { await api("DELETE", `/api/relations/${id}`); await loadOverview(); }
  catch (err) { toast(err.message); }
});

// ── ③ 标注者 ───────────────────────────────────────────────────────────
$("#annotatorForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    await api("POST", `/api/projects/${state.projectId}/annotators`,
      { name: fd.get("name"), weight: Number(fd.get("weight")) });
    e.target.reset();
    await loadOverview();
  } catch (err) { toast(err.message); }
});

function renderAnnotators() {
  const list = state.overview.annotators;
  $("#annotatorList").innerHTML = list.length
    ? `<table><thead><tr><th>ID</th><th>姓名</th><th>权重 1～10</th><th></th></tr></thead><tbody>${
        list.map((a) => `<tr>
          <td>${a.id}</td><td>${esc(a.name)}</td>
          <td><input type="number" min="1" max="10" value="${a.weight}" data-weight-for="${a.id}">
              <button data-save-weight="${a.id}">保存</button></td>
          <td><button data-del-ann-user="${a.id}">删除</button></td></tr>`).join("")
      }</tbody></table>`
    : '<p class="hint">尚无标注者（需要 2～8 名）。</p>';
}

$("#annotatorList").addEventListener("click", async (e) => {
  const saveId = e.target.dataset?.saveWeight;
  if (saveId) {
    const input = $(`[data-weight-for="${saveId}"]`);
    const a = state.overview.annotators.find((x) => x.id === Number(saveId));
    try {
      await api("PATCH", `/api/annotators/${saveId}`,
        { name: a.name, weight: Number(input.value) });
      await loadOverview();
      toast("权重已更新", true);
    } catch (err) { toast(err.message); }
    return;
  }
  const delId = e.target.dataset?.delAnnUser;
  if (delId && confirm("删除该标注者将同时删除其全部标注，确认？")) {
    try { await api("DELETE", `/api/annotators/${delId}`); await loadOverview(); }
    catch (err) { toast(err.message); }
  }
});

// ── ④ 标注提交 ─────────────────────────────────────────────────────────
async function initAnnotationsTab() {
  const docs = state.overview.documents;
  $("#annDocSelect").innerHTML = docs.map((d) =>
    `<option value="${d.id}" ${d.is_active ? "selected" : ""}>#${d.id} ${esc(d.name)}</option>`).join("");
  $("#annAnnotatorSelect").innerHTML = state.overview.annotators.map((a) =>
    `<option value="${a.id}">${esc(a.name)}（权重 ${a.weight}）</option>`).join("");
  await showAllAnnotations();
}

$("#loadAnnBtn").addEventListener("click", async () => {
  const docId = $("#annDocSelect").value;
  const annId = $("#annAnnotatorSelect").value;
  if (!docId || !annId) return;
  const rows = await api("GET", `/api/documents/${docId}/annotations`);
  const labelName = (id) => {
    const l = state.overview.labels.find((x) => x.id === id);
    return l ? l.name : id;
  };
  $("#annText").value = rows
    .filter((r) => r.annotator_id === Number(annId))
    .map((r) => `${r.start_offset},${r.end_offset},${labelName(r.label_id)}`)
    .join("\n");
});

$("#submitAnnBtn").addEventListener("click", async () => {
  const docId = Number($("#annDocSelect").value);
  const annotatorId = Number($("#annAnnotatorSelect").value);
  if (!docId || !annotatorId) return toast("请选择文档与标注者");
  const byName = new Map(state.overview.labels.map((l) => [l.name, l.id]));
  const items = [];
  const parseErrors = [];
  $("#annText").value.split("\n").forEach((line, i) => {
    line = line.trim();
    if (!line || line.startsWith("#")) return;
    const parts = line.split(/[,，\t]/).map((x) => x.trim());
    if (parts.length !== 3) { parseErrors.push(`第 ${i + 1} 行格式应为 start,end,标签名`); return; }
    const [s, e, label] = parts;
    if (!/^\d+$/.test(s) || !/^\d+$/.test(e)) { parseErrors.push(`第 ${i + 1} 行列号须为整数`); return; }
    if (!byName.has(label)) { parseErrors.push(`第 ${i + 1} 行未知标签「${label}」`); return; }
    items.push({ label_id: byName.get(label), start: Number(s), end: Number(e) });
  });
  if (parseErrors.length) return toast(parseErrors.join("\n"));
  try {
    const r = await api("PUT", `/api/documents/${docId}/annotations`,
      { annotator_id: annotatorId, annotations: items });
    $("#annResult").textContent = `已接受 ${r.accepted} 条标注`;
    toast("标注已保存", true);
    await showAllAnnotations();
  } catch (err) { toast(err.message); }
});

$("#showAllAnnBtn").addEventListener("click", showAllAnnotations);
$("#annDocSelect").addEventListener("change", showAllAnnotations);

async function showAllAnnotations() {
  const docId = $("#annDocSelect")?.value;
  if (!docId) return;
  const rows = await api("GET", `/api/documents/${docId}/annotations`);
  const box = $("#allAnnList");
  box.innerHTML = rows.length
    ? `<h3>该文档全部标注（${rows.length}）</h3>
       <table><thead><tr><th>标注者</th><th>标签</th><th>区间</th><th>原文片段</th></tr></thead><tbody>${
      rows.map((r) => `<tr>
        <td>${esc(r.annotator_name)}</td><td>${esc(r.label_name)}</td>
        <td class="mono">[${r.start_offset},${r.end_offset})</td>
        <td class="snippet">${esc(snippet(docId, r.start_offset, r.end_offset))}</td>
      </tr>`).join("")}</tbody></table>`
    : '<p class="hint">该文档还没有任何标注。</p>';
}

function snippet(docId, s, e) {
  const doc = state.documents.get(docId);
  if (!doc) return "…";
  return (s > 0 ? "…" : "") + doc.content.slice(s, e) + (e < doc.content.length ? "…" : "");
}

async function ensureDoc(docId) {
  if (!state.documents.has(Number(docId))) {
    state.documents.set(Number(docId), await api("GET", `/api/documents/${docId}`));
  }
  return state.documents.get(Number(docId));
}

// ── ⑤ 裁决台 ───────────────────────────────────────────────────────────
async function initAdjudicateTab() {
  const docs = state.overview.documents;
  $("#adjDocSelect").innerHTML = docs.map((d) =>
    `<option value="${d.id}" ${d.is_active ? "selected" : ""}>#${d.id} ${esc(d.name)}</option>`).join("");
  await loadAdjSelect();
  const adjId = $("#adjSelect").value;
  if (adjId) await renderAdjudication(adjId);
  else $("#adjPanel").innerHTML = '<p class="hint">点击「新建裁决」对当前文档求最优共识。</p>';
}

async function loadAdjSelect() {
  const docId = $("#adjDocSelect").value;
  const list = docId ? await api("GET", `/api/documents/${docId}/adjudications`) : [];
  $("#adjSelect").innerHTML = list.map((a) =>
    `<option value="${a.id}">裁决 #${a.id}${a.frozen ? `（已冻结，报告 v${a.report_version}）` : ""}</option>`).join("");
  return list;
}

$("#adjDocSelect").addEventListener("change", initAdjudicateTab);

$("#newAdjBtn").addEventListener("click", async () => {
  const docId = $("#adjDocSelect").value;
  if (!docId) return toast("请先导入文档");
  try {
    const r = await api("POST", `/api/documents/${docId}/adjudications`);
    await loadAdjSelect();
    $("#adjSelect").value = r.id;
    await renderAdjudication(r.id);
    toast("已求得最优共识", true);
  } catch (err) { toast(err.message); }
});

$("#adjSelect").addEventListener("change", (e) => {
  if (e.target.value) renderAdjudication(e.target.value);
});

async function renderAdjudication(adjId) {
  const adj = await api("GET", `/api/adjudications/${adjId}`);
  const docId = adj.document_id;
  await ensureDoc(docId);
  const r = adj.result;
  const panel = $("#adjPanel");

  if (r.solver_status === "INFEASIBLE") {
    panel.innerHTML = `<div class="error-box">${esc(r.infeasible_reason)}</div>
      <div id="overrideList"></div>`;
    renderOverrideList(adjId, r, true);
    return;
  }

  const adopted = r.candidates.filter((c) => c.adopted);
  const rejected = r.rejected_annotations;

  const forcedMark = (c) =>
    c.forced === "force_include" ? '<span class="tag ok">强制采纳</span>'
    : c.forced === "force_exclude" ? '<span class="tag bad">强制驳回</span>' : "";

  panel.innerHTML = `
  <div class="summary">
    <div>最优总得分：<b>${r.objective}</b> <span class="hint">(整数目标 ${r.objective_int})</span></div>
    <div>采纳候选：<b>${adopted.length}</b>　未采纳输入标注：<b>${rejected.length}</b></div>
    <div>状态：${adj.frozen ? '<span class="tag bad">已冻结（报告已生成）</span>' : '<span class="tag ok">可覆盖</span>'}</div>
    <button id="genReportBtn" ${adj.frozen ? "disabled" : ""}>生成不可变共识报告（新版本号）</button>
    <span id="reportLinkSlot"></span>
  </div>

  <h3>共识候选与得分（按平局序排列）</h3>
  <table><thead><tr><th>区间</th><th>标签</th><th>得分</th><th>支持者（权重×重叠）</th><th>状态</th></tr></thead>
  <tbody>${r.candidates.map((c) => `<tr class="${c.adopted ? "row-ok" : ""}">
    <td class="mono">[${c.start},${c.end})</td>
    <td>${esc(c.label_name)}</td>
    <td>${c.score}</td>
    <td>${c.supporters.map((s) =>
      `${esc(s.annotator_name)}(w${s.weight}×${s.jaccard})`).join("，") || "—"}</td>
    <td>${c.adopted ? "✅ 采纳" : "⬜ 未采纳"} ${forcedMark(c)}</td>
  </tr>`).join("")}</tbody></table>

  <h3>未采纳标注及其支持者</h3>
  ${rejected.length === 0 ? '<p class="hint">全部输入标注都被采纳。</p>' :
  `<table><thead><tr><th>标注者</th><th>标签</th><th>区间</th><th>片段</th>
    <th>同标签最大重叠</th><th>支持者</th><th>${adj.frozen ? "" : "裁决"}</th></tr></thead><tbody>${
    rejected.map((a) => `<tr>
      <td>${esc(a.annotator_name)}</td>
      <td>${esc(a.label_name)}</td>
      <td class="mono">[${a.start},${a.end})</td>
      <td class="snippet">${esc(snippet(docId, a.start, a.end))}</td>
      <td>${a.same_label_max_jaccard}</td>
      <td>${a.supporters.length
        ? a.supporters.map((s) => `${esc(s.annotator_name)}(w${s.weight}×${s.jaccard})`).join("，")
        : "无（可能是独有标签/区间）"}</td>
      <td>${adj.frozen ? "" : `
        <button data-force='{"label_id":${a.label_id},"start":${a.start},"end":${a.end},"action":"force_include"}'>强制采纳</button>
        <button data-force='{"label_id":${a.label_id},"start":${a.start},"end":${a.end},"action":"force_exclude"}'>强制驳回</button>`}
      </td>
    </tr>`).join("")}</tbody></table>`}

  <h3>裁决者覆盖（含理由）</h3>
  <div id="overrideList"></div>
  `;

  renderOverrideList(adjId, r, adj.frozen);

  if (adj.frozen) {
    const reports = await api("GET", `/api/reports?project_id=${state.projectId}`);
    const rep = reports.filter((x) => x.adjudication_id === adjId).pop();
    if (rep) {
      $("#reportLinkSlot").innerHTML =
        `<a class="button" href="/report.html?id=${rep.id}" target="_blank">打开共识报告 v${rep.version}</a>`;
    }
  }

  panel.onclick = async (e) => {
    const btn = e.target.closest("[data-force]");
    if (!btn) return;
    const spec = JSON.parse(btn.dataset.force);
    const reason = prompt(
      `${spec.action === "force_include" ? "强制采纳" : "强制驳回"} [${spec.start},${spec.end}) ${
        state.overview.labels.find((l) => l.id === spec.label_id)?.name ?? spec.label_id} —— 请注明理由：`,
      "");
    if (reason === null) return;
    try {
      await api("POST", `/api/adjudications/${adjId}/overrides`, { ...spec, reason });
      await renderAdjudication(adjId);
      toast("覆盖已应用，共识已重算", true);
    } catch (err) { toast(err.message); }
  };

  const genBtn = $("#genReportBtn");
  if (genBtn) genBtn.addEventListener("click", async () => {
    if (!confirm("生成报告后该裁决将永久冻结，且旧报告不可变。继续？")) return;
    try {
      const rep = await api("POST", `/api/adjudications/${adjId}/reports`);
      await loadOverview();
      await loadAdjSelect();
      await renderAdjudication(adjId);
      toast(`报告 v${rep.version} 已生成并冻结`, true);
      window.open(`/report.html?id=${rep.id}`, "_blank");
    } catch (err) { toast(err.message); }
  });
}

function renderOverrideList(adjId, r, frozen) {
  const box = $("#overrideList");
  if (!r.overrides.length) {
    box.innerHTML = '<p class="hint">暂无人工覆盖。</p>';
    return;
  }
  const nameOf = (id) => state.overview.labels.find((l) => l.id === id)?.name ?? id;
  box.innerHTML = `<table><thead><tr><th>区间</th><th>标签</th><th>动作</th><th>理由</th><th></th></tr></thead>
  <tbody>${r.overrides.map((o) => `<tr>
    <td class="mono">[${o.start},${o.end})</td><td>${esc(nameOf(o.label_id))}</td>
    <td>${o.action === "force_include" ? "强制采纳" : "强制驳回"}</td>
    <td>${esc(o.reason)}</td>
    <td>${frozen ? "" : `<button data-del-ov="${o.id}">撤销</button>`}</td></tr>`).join("")}</tbody></table>`;
  box.onclick = async (e) => {
    const ovId = e.target.dataset?.delOv;
    if (ovId === undefined) return;
    try {
      await api("DELETE", `/api/adjudications/${adjId}/overrides/${ovId}`);
      await renderAdjudication(adjId);
      toast("已撤销覆盖并恢复自动共识", true);
    } catch (err) { toast(err.message); }
  };
}

// ── ⑥ 报告列表 ─────────────────────────────────────────────────────────
async function renderReports() {
  const list = await api("GET", `/api/reports?project_id=${state.projectId}`);
  const box = $("#reportList");
  box.innerHTML = list.length
    ? `<table><thead><tr><th>ID</th><th>版本</th><th>文档</th><th>应用版本</th><th>生成时间(UTC)</th>
       <th>SHA-256</th><th>操作</th></tr></thead><tbody>${
      list.map((r) => `<tr>
        <td>${r.id}</td><td>v${r.version}</td><td>${esc(r.document_name)}</td>
        <td>${r.app_version}</td><td>${r.created_at}</td>
        <td class="mono hash">${r.sha256.slice(0, 16)}…</td>
        <td><a class="button" href="/report.html?id=${r.id}" target="_blank">打开报告</a>
            <a class="button" href="/api/reports/${r.id}/download">下载</a></td>
      </tr>`).join("")}</tbody></table>`
    : '<p class="hint">尚无报告。在「裁决台」生成共识后点击「生成不可变共识报告」。</p>';
}

// ── 启动 ───────────────────────────────────────────────────────────────
(async function init() {
  try {
    const h = await api("GET", "/api/health");
    $("#envBadge").textContent = `v${h.app_version} · OR-Tools ${h.ortools_version}`;
  } catch {
    $("#envBadge").textContent = "后端不可用";
  }
  await loadProjects();
})();

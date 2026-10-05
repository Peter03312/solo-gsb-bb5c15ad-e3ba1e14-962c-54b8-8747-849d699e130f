"use strict";

const PREVIEW_LIMIT = 1000;

const STATUS_TEXT = {
  mapped: "完全映射",
  partial: "部分映射",
  ambiguous: "歧义（待人工裁决）",
  unmapped: "无映射",
};

const STAGE_TEXT = {
  segment1: "第一段未映射",
  segment2: "第二段未映射",
};

const form = document.getElementById("lift-form");
const bedInput = document.getElementById("bed");
const chainInput = document.getElementById("chain");
const chain1Input = document.getElementById("chain1");
const chain2Input = document.getElementById("chain2");
const bedHint = document.getElementById("bed-hint");
const chainHint = document.getElementById("chain-hint");
const chain1Hint = document.getElementById("chain1-hint");
const chain2Hint = document.getElementById("chain2-hint");
const singleFields = document.getElementById("single-fields");
const doubleFields = document.getElementById("double-fields");
const submitBtn = document.getElementById("submit-btn");
const errorBox = document.getElementById("error-box");
const errorList = document.getElementById("error-list");
const resultBox = document.getElementById("result-box");
const statsEl = document.getElementById("stats");
const tbody = document.getElementById("result-tbody");
const previewNote = document.getElementById("preview-note");
const downloadBtn = document.getElementById("download-btn");
const downloadAdjBtn = document.getElementById("download-adj-btn");
const decisionNote = document.getElementById("decision-note");
const actionMsg = document.getElementById("action-msg");
const filterBtns = document.querySelectorAll(".filter");

let currentToken = null;
let currentName = null;
let currentRows = [];
let currentKind = "single"; // single / double（来自服务端响应）
let activeFilter = "all";
// 裁决状态：{源行号: 候选/路径序号(1基)}；撤销后该键删除
let decisions = {};
let resultVersion = 0;
let decisionPending = false;

function bindHint(input, hintEl) {
  input.addEventListener("change", () => {
    hintEl.textContent = input.files[0]
      ? `${input.files[0].name}（${formatSize(input.files[0].size)}）`
      : "未选择文件";
  });
}
bindHint(bedInput, bedHint);
bindHint(chainInput, chainHint);
bindHint(chain1Input, chain1Hint);
bindHint(chain2Input, chain2Hint);

function currentMode() {
  return form.querySelector('input[name="mode"]:checked').value;
}

form.querySelectorAll('input[name="mode"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    const mode = currentMode();
    singleFields.classList.toggle("hidden", mode !== "single");
    doubleFields.classList.toggle("hidden", mode !== "double");
  });
});

filterBtns.forEach((btn) => {
  btn.addEventListener("click", () => {
    activeFilter = btn.dataset.f;
    filterBtns.forEach((b) => b.classList.toggle("active", b === btn));
    renderRows();
  });
});

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  errorBox.classList.add("hidden");
  resultBox.classList.add("hidden");

  const mode = currentMode();
  const clientErrors = [];
  if (!bedInput.files[0]) clientErrors.push("请选择 BED 文件");
  if (mode === "single" && !chainInput.files[0]) {
    clientErrors.push("请选择 UCSC chain 文件");
  }
  if (mode === "double") {
    if (!chain1Input.files[0]) clientErrors.push("请选择 chain 文件 1（第一段）");
    if (!chain2Input.files[0]) clientErrors.push("请选择 chain 文件 2（第二段）");
  }
  if (clientErrors.length) {
    showErrors(clientErrors);
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = "转换中…";

  const fd = new FormData();
  fd.append("bed", bedInput.files[0]);
  let url = "/api/liftover";
  if (mode === "double") {
    fd.append("chain1", chain1Input.files[0]);
    fd.append("chain2", chain2Input.files[0]);
    fd.append("side1", form.querySelector('input[name="side1"]:checked').value);
    fd.append("side2", form.querySelector('input[name="side2"]:checked').value);
    url = "/api/liftover2";
  } else {
    fd.append("chain", chainInput.files[0]);
    fd.append("side", form.querySelector('input[name="side"]:checked').value);
  }

  try {
    const resp = await fetch(url, { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) {
      showErrors(data.errors || [`请求失败（HTTP ${resp.status}）`]);
      return;
    }
    currentToken = data.download_token;
    currentName = data.download_name;
    currentKind = data.kind || mode;
    currentRows = data.preview.rows;
    decisions = {};
    resultVersion = 0;
    renderStats(data.preview);
    await refreshDecisions();
    renderRows();
    resultBox.classList.remove("hidden");
    resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showErrors([`网络或服务错误：${err && err.message ? err.message : err}`]);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "校验并转换";
  }
});

downloadBtn.addEventListener("click", () => {
  if (!currentToken) return;
  // 直接触发浏览器下载；token 只在转换完全成功后才存在。
  // 原始结果不随裁决改变。
  triggerDownload(`/api/download/${encodeURIComponent(currentToken)}`, currentName || "");
});

downloadAdjBtn.addEventListener("click", () => {
  if (!currentToken) return;
  triggerDownload(
    `/api/download/${encodeURIComponent(currentToken)}/adjudicated`,
    "",
  );
});

function triggerDownload(href, name) {
  const a = document.createElement("a");
  a.href = href;
  if (name) a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function refreshDecisions() {
  // 从服务端拉取当前选择与版本（裁决只保存在服务端该结果上）
  const resp = await fetch(`/api/result/${encodeURIComponent(currentToken)}/decisions`);
  if (!resp.ok) {
    decisions = {};
    return;
  }
  const data = await resp.json();
  resultVersion = data.version;
  decisions = {};
  for (const d of data.decisions) {
    if (d.candidate_index !== null && d.candidate_index !== undefined) {
      decisions[d.source_line] = d.candidate_index;
    }
  }
  renderDecisionNote();
}

function renderDecisionNote() {
  const noun = currentKind === "double" ? "路径" : "候选";
  const total = currentRows.filter((r) => r.status === "ambiguous").length;
  const picked = currentRows.filter(
    (r) => r.status === "ambiguous" && decisions[r.line_no] !== undefined,
  ).length;
  if (total === 0) {
    decisionNote.textContent =
      "本次结果没有歧义行，无需人工裁决；原始结果与裁决版内容一致。";
  } else {
    decisionNote.innerHTML =
      `歧义行人工裁决：共 <b>${total}</b> 行歧义，已选${noun} <b>${picked}</b> 行，` +
      `未裁决 <b>${total - picked}</b> 行（结果版本 v${resultVersion}）。` +
      `在每行${noun}前点选单选框即提交裁决，选「撤销」可恢复全部${noun}；` +
      "裁决只作用于本次结果，不改动原始转换，原始状态（partial/ambiguous 等）保持不变。";
  }
}

function setActionMsg(text, kind) {
  actionMsg.textContent = text || "";
  actionMsg.className = `action-msg${kind ? " " + kind : ""}`;
}

async function submitDecision(sourceLine, candidateIndex, label) {
  if (decisionPending) return;
  decisionPending = true;
  setActionMsg(`正在提交第 ${sourceLine} 行的裁决（${label}）…`, "muted");
  renderRows();
  try {
    const resp = await fetch(
      `/api/result/${encodeURIComponent(currentToken)}/decisions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version: resultVersion,
          source_line: sourceLine,
          candidate_index: candidateIndex,
        }),
      },
    );
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409) {
      setActionMsg(
        (data.errors && data.errors[0]) ||
          "结果版本已被其他操作更新，已自动刷新当前选择，请重试。",
        "error",
      );
      await refreshDecisions();
      return;
    }
    if (!resp.ok) {
      setActionMsg((data.errors && data.errors.join("；")) || `提交失败（HTTP ${resp.status}）`, "error");
      return;
    }
    resultVersion = data.version;
    if (candidateIndex === null) {
      delete decisions[sourceLine];
    } else {
      decisions[sourceLine] = candidateIndex;
    }
    setActionMsg(
      data.modified
        ? `第 ${sourceLine} 行裁决已保存，结果版本递增至 v${resultVersion}。`
        : `第 ${sourceLine} 行与当前选择一致，版本不变（v${resultVersion}）。`,
      "ok",
    );
    renderDecisionNote();
  } catch (err) {
    setActionMsg(`网络或服务错误：${err && err.message ? err.message : err}`, "error");
  } finally {
    decisionPending = false;
    renderRows();
  }
}

function showErrors(errors) {
  errorList.innerHTML = "";
  for (const msg of errors) {
    const li = document.createElement("li");
    li.textContent = msg;
    errorList.appendChild(li);
  }
  errorBox.classList.remove("hidden");
  errorBox.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderStats(preview) {
  const c = preview.counts;
  statsEl.innerHTML = "";
  const items = [
    ["总数", preview.total, ""],
    ["完全映射", c.mapped, "mapped"],
    ["部分映射", c.partial, "partial"],
    ["歧义", c.ambiguous, "ambiguous"],
    ["无映射", c.unmapped, "unmapped"],
  ];
  for (const [label, n, cls] of items) {
    const div = document.createElement("div");
    div.className = `stat ${cls}`;
    div.innerHTML = `<b>${n}</b>${label}`;
    statsEl.appendChild(div);
  }
}

function fmtUncovered(unc) {
  return (unc || []).map((iv) => `[${iv[0]},${iv[1]})`).join(";");
}

// 单段结果的“候选 / 片段”列
function renderSingleDest(row, tdDest) {
  if (row.candidates.length === 0) {
    tdDest.textContent = "—";
    return;
  }
  if (row.status === "ambiguous") {
    const picked = decisions[row.line_no];
    const box = document.createElement("div");
    box.className = "decision-box";
    row.candidates.forEach((cand, ci) => {
      const div = document.createElement("div");
      div.className = "cand decision-cand";
      if (picked === ci + 1) div.classList.add("selected");

      const label = document.createElement("label");
      label.className = "cand-pick";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = `dec-${row.line_no}`;
      radio.value = String(ci + 1);
      radio.checked = picked === ci + 1;
      radio.disabled = decisionPending;
      radio.addEventListener("change", () => {
        submitDecision(row.line_no, ci + 1, `候选 ${ci + 1}/${row.candidates.length}`);
      });
      label.appendChild(radio);
      label.appendChild(
        document.createTextNode(`候选 ${ci + 1}/${row.candidates.length} · chain ${cand.chain_id}`),
      );
      div.appendChild(label);

      cand.fragments.forEach((frag, fi) => {
        fl_appendSingleFrag(div, cand, frag, fi);
      });
      box.appendChild(div);
    });
    box.appendChild(buildUndoPick(row));
    tdDest.appendChild(box);
    return;
  }
  row.candidates.forEach((cand, ci) => {
    const div = document.createElement("div");
    div.className = "cand";
    if (row.candidates.length > 1) {
      const head = document.createElement("div");
      head.className = "cand-head";
      head.textContent =
        `候选 ${ci + 1}/${row.candidates.length} · chain ${cand.chain_id}`;
      div.appendChild(head);
    }
    cand.fragments.forEach((frag, fi) => {
      fl_appendSingleFrag(div, cand, frag, fi);
    });
    tdDest.appendChild(div);
  });
}

function fl_appendSingleFrag(div, cand, frag, fi) {
  const fl = document.createElement("div");
  fl.className = "frag-line";
  const prefix = cand.fragments.length > 1
    ? `片段${fi + 1}(${frag.src_start}-${frag.src_end}) → `
    : "";
  fl.textContent =
    `${prefix}${cand.dest_chrom}:${frag.dest_start}-${frag.dest_end} ` +
    `(${cand.dest_strand})`;
  div.appendChild(fl);
}

// 双段结果的“路径 / 片段”列
function renderDoubleDest(row, tdDest) {
  if (row.candidates.length === 0) {
    tdDest.textContent = "—";
    return;
  }
  const isAmb = row.status === "ambiguous";
  const picked = decisions[row.line_no];
  const box = isAmb ? document.createElement("div") : null;
  if (isAmb) box.className = "decision-box";

  row.candidates.forEach((cand, ci) => {
    const div = document.createElement("div");
    div.className = isAmb ? "cand decision-cand" : "cand";
    if (isAmb && picked === ci + 1) div.classList.add("selected");

    const headText =
      `路径 ${cand.path_index}/${row.candidates.length} · ` +
      `chain1 #${cand.chain1_id} × chain2 #${cand.chain2_id}`;
    if (isAmb) {
      const label = document.createElement("label");
      label.className = "cand-pick";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = `dec-${row.line_no}`;
      radio.value = String(ci + 1);
      radio.checked = picked === ci + 1;
      radio.disabled = decisionPending;
      radio.addEventListener("change", () => {
        submitDecision(row.line_no, ci + 1, `路径 ${ci + 1}/${row.candidates.length}`);
      });
      label.appendChild(radio);
      label.appendChild(document.createTextNode(headText));
      div.appendChild(label);
    } else {
      const head = document.createElement("div");
      head.className = "cand-head";
      head.textContent = headText;
      div.appendChild(head);
    }

    const cov = document.createElement("div");
    cov.className = "path-cov";
    cov.textContent =
      `覆盖：第一段 ${cand.seg1_covered}/${cand.total} bp → ` +
      `第二段 ${cand.seg2_covered}/${cand.seg1_covered} bp` +
      (cand.uncovered_src && cand.uncovered_src.length
        ? `；未覆盖源区间 ${fmtUncovered(cand.uncovered_src)}`
        : "");
    div.appendChild(cov);

    cand.fragments.forEach((frag, fi) => {
      const fl = document.createElement("div");
      fl.className = "frag-line";
      fl.textContent =
        `片段${fi + 1} 源[${frag.src_start},${frag.src_end}) → ` +
        `中间 ${cand.mid_chrom}:[${frag.mid_start},${frag.mid_end})(${cand.mid_strand}) → ` +
        `${cand.dest_chrom}:[${frag.dest_start},${frag.dest_end})(${cand.dest_strand})`;
      div.appendChild(fl);
    });
    (isAmb ? box : tdDest).appendChild(div);
  });

  if (isAmb) {
    box.appendChild(buildUndoPick(row));
    tdDest.appendChild(box);
  }
}

function buildUndoPick(row) {
  const picked = decisions[row.line_no];
  const undoLabel = document.createElement("label");
  undoLabel.className = "cand-pick undo-pick";
  const undo = document.createElement("input");
  undo.type = "radio";
  undo.name = `dec-${row.line_no}`;
  undo.checked = picked === undefined;
  undo.disabled = decisionPending;
  undo.addEventListener("change", () => {
    submitDecision(row.line_no, null, "撤销选择");
  });
  undoLabel.appendChild(undo);
  undoLabel.appendChild(
    document.createTextNode(
      currentKind === "double"
        ? "撤销选择（恢复全部路径，标记为未裁决）"
        : "撤销选择（恢复全部候选，标记为未裁决）",
    ),
  );
  return undoLabel;
}

function renderRows() {
  tbody.innerHTML = "";
  const rows = currentRows.filter((r) => activeFilter === "all" || r.status === activeFilter);
  const shown = rows.slice(0, PREVIEW_LIMIT);
  const noun = currentKind === "double" ? "路径" : "候选";
  previewNote.textContent =
    currentRows.length > PREVIEW_LIMIT
      ? `预览仅显示前 ${PREVIEW_LIMIT} 行（当前筛选 ${rows.length} 行 / 共 ${currentRows.length} 行）；下载的 TSV 包含全部结果。`
      : `共 ${currentRows.length} 行源区间；多片段、歧义${noun}在“候选 / 片段”列内分行展开。`;

  for (const row of shown) {
    const tr = document.createElement("tr");

    const tdLine = document.createElement("td");
    tdLine.className = "mono";
    tdLine.textContent = row.line_no;

    const tdSrc = document.createElement("td");
    tdSrc.className = "mono";
    tdSrc.textContent = `${row.src_chrom}:${row.src_start}-${row.src_end}`;

    const tdStatus = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${row.status}`;
    badge.textContent = STATUS_TEXT[row.status] || row.status;
    tdStatus.appendChild(badge);
    if (row.status === "unmapped" && row.failed_stage) {
      const tag = document.createElement("span");
      tag.className = "stage-tag";
      tag.textContent = STAGE_TEXT[row.failed_stage] || row.failed_stage;
      tdStatus.appendChild(document.createElement("br"));
      tdStatus.appendChild(tag);
    }
    if (row.status === "ambiguous") {
      const tag = document.createElement("span");
      const picked = decisions[row.line_no];
      tag.className = `decision-tag ${picked === undefined ? "pending" : "picked"}`;
      tag.textContent = picked === undefined
        ? "未裁决"
        : `已选${currentKind === "double" ? "路径" : "候选"} ${picked}`;
      tdStatus.appendChild(document.createElement("br"));
      tdStatus.appendChild(tag);
    }

    const tdDest = document.createElement("td");
    if (currentKind === "double") {
      renderDoubleDest(row, tdDest);
    } else {
      renderSingleDest(row, tdDest);
    }

    const tdReason = document.createElement("td");
    tdReason.className = "reason";
    tdReason.textContent = row.reason || "";

    tr.append(tdLine, tdSrc, tdStatus, tdDest, tdReason);
    tbody.appendChild(tr);
  }
}

function formatSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

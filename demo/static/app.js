/* tablefold demo.
   화면은 세 단계로만 읽힌다: 무엇이 줄었나 → 어떻게 묶였나 → 실제로 돌려보기.
   전문 용어는 화면에 쓰지 않고, 필요한 곳에서는 한 줄로 풀어 쓴다. */

const $ = (id) => document.getElementById(id);

const KIND = {
  base: {
    label: "원래 있던 컬럼",
    why: "이 모델의 중심 테이블이 원래 가지고 있던 값입니다.",
    cls: "seg-base",
    color: "#4b6fa8",
  },
  joined: {
    label: "붙여온 값",
    why: "다른 테이블에서 가져왔습니다. 한 줄에 하나씩만 대응되므로 줄 수가 늘지 않습니다.",
    cls: "seg-joined",
    color: "#7d9bc4",
  },
  aggregated: {
    label: "합계 · 개수",
    why: "여러 줄짜리 하위 테이블을 미리 더하거나 세어 둔 값입니다. 그냥 이어 붙이면 금액이 부풀기 때문에 미리 접어 둡니다.",
    cls: "seg-agg",
    color: "#b9c9de",
  },
};

const num = (n) => Number(n).toLocaleString("ko-KR");

let foldData = null;

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  loadSample();
});

function bindEvents() {
  $("loadSample").addEventListener("click", loadSample);
  $("runFold").addEventListener("click", runFold);
  $("runExpand").addEventListener("click", runExpand);
  $("copySql").addEventListener("click", copySql);

  document.querySelectorAll(".presets .chip-btn").forEach((b) => {
    b.addEventListener("click", () => {
      $("logicalSql").value = b.dataset.sql;
    });
  });

  $("sheetClose").addEventListener("click", closeSheet);
  $("sheet").addEventListener("click", (e) => {
    if (e.target === $("sheet")) closeSheet();
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSheet();
  });
}

// ── 데이터 ──────────────────────────────────────────

function readSettings() {
  const areas = $("maxAreas").value;
  return {
    coverage: parseFloat($("coverage").value),
    min_gain: parseInt($("minGain").value, 10),
    max_cost: parseFloat($("maxCost").value),
    field_budget: parseInt($("fieldBudget").value, 10),
    max_areas: areas ? parseInt(areas, 10) : null,
  };
}

async function loadSample() {
  const btn = $("loadSample");
  btn.disabled = true;
  try {
    const res = await fetch("/api/sample");
    if (!res.ok) throw new Error("예제를 불러오지 못했습니다.");
    $("ddlInput").value = (await res.json()).ddl;
    await runFold();
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function runFold() {
  const ddl = $("ddlInput").value;
  if (!ddl.trim()) {
    toast("데이터베이스 정의(DDL)를 먼저 넣어 주세요.");
    return;
  }

  const btn = $("runFold");
  btn.disabled = true;
  btn.textContent = "묶는 중…";

  try {
    const res = await fetch("/api/fold", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ddl, ...readSettings() }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "묶기에 실패했습니다.");

    foldData = await res.json();
    render(foldData);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "다시 묶기";
  }
}

async function runExpand() {
  const ddl = $("ddlInput").value;
  const sql = $("logicalSql").value;
  if (!ddl.trim() || !sql.trim()) {
    toast("SQL을 입력해 주세요.");
    return;
  }

  const btn = $("runExpand");
  btn.disabled = true;
  btn.textContent = "바꾸는 중…";

  try {
    const res = await fetch("/api/expand", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ddl, sql, ...readSettings() }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "변환에 실패했습니다.");

    const data = await res.json();
    $("expandedSql").textContent = data.expanded_sql;

    const stat = $("expandStat");
    stat.hidden = false;
    stat.textContent =
      `이 질문에 실제로 필요한 연결은 ${data.joins_emitted}개입니다. ` +
      `모델이 가진 나머지 ${data.joins_pruned}개는 쓰지 않았습니다.`;
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "바꿔보기";
  }
}

async function copySql() {
  const text = $("expandedSql").textContent;
  if (!text || text.startsWith("바꿔보기")) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("복사했습니다.");
  } catch {
    toast("복사하지 못했습니다.");
  }
}

// ── 그리기 ──────────────────────────────────────────

function render(data) {
  renderHeadline(data);
  renderModels(data);
  renderLeftover(data);
  renderAdvanced(data);
}

function renderHeadline(data) {
  const s = data.size;
  const t = data.tier_summary;

  $("headlineEmpty").hidden = true;
  $("headlineBody").hidden = false;
  $("factsRow").hidden = false;

  const saved = Math.max(0, Math.round((1 - s.core_prompt_chars / s.ddl_chars) * 100));

  $("beforeChars").textContent = num(s.ddl_chars);
  $("beforeNote").textContent = `테이블 ${t.total_physical_tables_count}개를 그대로`;
  $("afterChars").textContent = num(s.core_prompt_chars);
  $("afterNote").textContent = `모델 ${t.tier1_core_models_count}개로`;
  $("savedPct").textContent = `${saved}%`;

  $("headlinePlain").innerHTML =
    `AI에게 데이터베이스를 설명하려면 원래 <b>${num(s.ddl_chars)}자</b>를 통째로 넣어야 했습니다. ` +
    `묶은 뒤에는 <b>${num(s.core_prompt_chars)}자</b>면 됩니다. ` +
    `설명이 짧아지면 AI가 덜 헷갈리고, 비용도 그만큼 줄어듭니다.`;

  const pct = Math.round(
    (t.tier1_covered_physical_tables_count / t.total_physical_tables_count) * 100
  );

  $("factModels").textContent = `${t.tier1_core_models_count}개`;
  $("factCovered").textContent = `${t.tier1_covered_physical_tables_count}개`;
  $("factCoveredHelp").textContent = `전체 ${t.total_physical_tables_count}개 중 ${pct}%`;
  $("factEdge").textContent = `${t.tier2_edge_tables_count}개`;
  $("factLinks").textContent = `${data.physical.inferred_fk_count}개`;
}

function renderModels(data) {
  const models = data.logical.models;
  const list = $("modelList");

  if (!models.length) {
    list.innerHTML = `<div class="empty">묶인 모델이 없습니다.</div>`;
    return;
  }

  list.innerHTML = "";

  models.forEach((m, i) => {
    const counts = countKinds(m.fields);
    const total = m.fields.length || 1;
    const tables = m.absorbed_tables.length + 1;

    const btn = document.createElement("button");
    btn.className = "model";
    btn.type = "button";
    btn.innerHTML = `
      <div>
        <div class="model-name">${esc(m.name)}</div>
        <div class="model-line">
          ${esc(m.base_table)} 1건이 한 줄. 테이블 ${tables}개의 내용이 이 줄에 들어 있습니다.
        </div>
      </div>
      <div class="model-right">
        <div class="model-count">${tables}</div>
        <div class="model-count-label">테이블</div>
      </div>
      <div class="model-bar">
        ${["base", "joined", "aggregated"]
          .map((k) =>
            counts[k]
              ? `<i class="${KIND[k].cls}" style="width:${(counts[k] / total) * 100}%"></i>`
              : ""
          )
          .join("")}
      </div>
      <div class="model-legend">
        ${["base", "joined", "aggregated"]
          .map((k) =>
            counts[k]
              ? `<span><i class="dot" style="background:${KIND[k].color}"></i>${KIND[k].label} ${counts[k]}</span>`
              : ""
          )
          .join("")}
        <span style="color:var(--ink-3)">눌러서 자세히 보기</span>
      </div>
    `;
    btn.addEventListener("click", () => openSheet(models[i]));
    list.appendChild(btn);
  });
}

function renderLeftover(data) {
  const edge = data.tier2_edge_tables || [];
  const box = $("leftoverBox");
  if (!edge.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("leftoverCount").textContent = `${edge.length}개`;
  $("leftoverChips").innerHTML = edge
    .map((t) => `<span class="chip">${esc(t.name)}</span>`)
    .join("");
}

function renderAdvanced(data) {
  $("promptText").textContent = data.prompt_text;

  const fks = data.physical.inferred_fks || [];
  $("fkBody").innerHTML = fks.length
    ? fks
        .map(
          (fk) => `
        <tr>
          <td class="mono">${esc(fk.from_table)}</td>
          <td class="mono">${esc(fk.from_columns.join(", "))}</td>
          <td class="mono">${esc(fk.to_table)}.${esc(fk.to_columns.join(", "))}</td>
          <td class="num">${Math.round(fk.confidence * 100)}%</td>
        </tr>`
        )
        .join("")
    : `<tr><td colspan="4" class="empty">DDL에 모든 연결이 이미 적혀 있어 되찾을 것이 없었습니다.</td></tr>`;

  $("tableBody").innerHTML = data.physical.tables
    .map((t) => {
      const inModel = t.tier.includes("Core");
      return `
        <tr>
          <td class="mono">${esc(t.name)}</td>
          <td><span class="pill ${inModel ? "pill-in" : "pill-out"}">${
            inModel ? "모델에 포함" : "따로 남음"
          }</span></td>
          <td class="num">${t.fact_score.toFixed(2)}</td>
          <td class="num">${t.column_count}</td>
          <td class="num">${t.in_degree}</td>
          <td class="num">${t.out_degree}</td>
        </tr>`;
    })
    .join("");
}

// ── 상세 시트 ───────────────────────────────────────

function openSheet(model) {
  $("sheetName").textContent = model.name;
  $("sheetDesc").textContent =
    `${model.base_table} 1건이 한 줄입니다. ` +
    `테이블 ${model.absorbed_tables.length + 1}개의 내용이 항목 ${model.fields.length}개로 들어 있습니다.`;

  const groups = ["base", "joined", "aggregated"]
    .map((kind) => {
      const fields = model.fields.filter((f) => f.kind === kind);
      if (!fields.length) return "";
      return `
        <div class="field-group">
          <div class="field-group-head">
            <h4>${KIND[kind].label}</h4>
            <span class="field-group-count">${fields.length}개</span>
          </div>
          <p class="field-group-why">${KIND[kind].why}</p>
          ${fields
            .map(
              (f) => `
            <div class="field-row">
              <span class="field-nm">${esc(f.name)}</span>
              <span class="field-src">${esc(sourceOf(f))}</span>
            </div>`
            )
            .join("")}
        </div>`;
    })
    .join("");

  $("sheetBody").innerHTML = `
    <div class="field-group">
      <div class="field-group-head"><h4>합쳐진 테이블</h4>
        <span class="field-group-count">${model.absorbed_tables.length + 1}개</span></div>
      <div class="chips">
        <span class="chip">${esc(model.base_table)} (중심)</span>
        ${model.absorbed_tables.map((t) => `<span class="chip">${esc(t)}</span>`).join("")}
      </div>
    </div>
    ${groups}`;

  $("sheet").hidden = false;
}

function closeSheet() {
  $("sheet").hidden = true;
}

function sourceOf(f) {
  const base = `${f.source.table}.${f.source.column}`;
  return f.source.aggregate ? `${f.source.aggregate}(${base})` : base;
}

// ── 자잘한 것 ───────────────────────────────────────

function countKinds(fields) {
  return fields.reduce(
    (acc, f) => ({ ...acc, [f.kind]: (acc[f.kind] || 0) + 1 }),
    { base: 0, joined: 0, aggregated: 0 }
  );
}

function esc(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

let toastTimer;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2600);
}

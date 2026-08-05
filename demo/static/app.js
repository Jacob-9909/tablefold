/**
 * 데모 화면의 흐름. 읽는 사람이 개발자가 아니라는 전제로 쓴다.
 *
 * 기본 소스는 연결된 데이터베이스다. 예제 DDL 은 연결이 없을 때의 대비책이지
 * 출발점이 아니다 — 이 도구가 답해야 하는 질문은 "내 데이터베이스가 어떻게
 * 묶이는가" 이고, 예제로는 그 답이 안 나온다.
 */

const $ = (id) => document.getElementById(id);

const KIND = {
  base: {
    label: "표 자신의 항목",
    help: "이 표에 원래 있던 값입니다.",
  },
  joined: {
    label: "붙여 온 항목",
    help: "관련된 표에서 한 줄씩 가져와 붙였습니다. 조인이 이미 끝나 있습니다.",
  },
  aggregated: {
    label: "미리 합계 낸 항목",
    help: "여러 줄짜리 내용을 한 줄로 합쳤습니다. 이미 합계이므로 다시 더해도 중복되지 않습니다.",
  },
  filter: {
    label: "조건에만 쓰는 항목",
    help: "위 합계에 기간·구분 같은 조건을 걸 때만 씁니다. 값으로 꺼낼 수는 없습니다.",
  },
};

const num = (n) => Number(n).toLocaleString("ko-KR");
const esc = (s) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

let foldData = null;
let liveAvailable = false;

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  await discoverSources();
  await loadSample({ silent: true });
  runFold();
});

/** 연결된 데이터베이스가 있으면 그것을 기본으로, 없으면 예제로 내려앉는다. */
async function discoverSources() {
  const option = $("sourceSelect").querySelector('option[value="live"]');
  try {
    const res = await fetch("/api/sources");
    const info = await res.json();
    liveAvailable = Boolean(info.live_available);
    if (liveAvailable) {
      option.textContent = `데이터베이스 연결${
        info.live_label ? ` (${info.live_label})` : ""
      }`;
      $("sourceSelect").value = "live";
    } else {
      option.textContent = "데이터베이스 연결 (연결 없음)";
      option.disabled = true;
      $("sourceSelect").value = "ddl";
    }
  } catch {
    option.textContent = "데이터베이스 연결 (확인 실패)";
    option.disabled = true;
    $("sourceSelect").value = "ddl";
  }
  syncSourceUI();
}

function syncSourceUI() {
  const live = $("sourceSelect").value === "live";
  $("anchorKnob").hidden = !live;
  $("ddlBlock").hidden = live;
  $("loadSample").hidden = live;
  $("sourceNote").textContent = live
    ? "연결된 데이터베이스의 카탈로그를 직접 읽습니다. 선언된 외래 키가 없어도 기본 키와 실제 데이터로 관계를 되찾습니다."
    : "위에 붙여 넣은 정의문(DDL)을 읽습니다.";
}

function bindEvents() {
  $("sourceSelect").addEventListener("change", () => {
    syncSourceUI();
    runFold();
  });
  $("anchorSelect").addEventListener("change", runFold);
  $("loadSample").addEventListener("click", () => loadSample());
  $("runFold").addEventListener("click", runFold);
  $("runExpand").addEventListener("click", runExpand);
  $("copySql").addEventListener("click", copySql);

  $("sheetClose").addEventListener("click", closeSheet);
  $("sheet").addEventListener("click", (e) => {
    if (e.target.id === "sheet") closeSheet();
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSheet();
  });
}

function readSettings() {
  const source = $("sourceSelect").value;
  return {
    ddl: source === "ddl" ? $("ddlInput").value : "",
    source,
    anchor_mode: source === "live" ? $("anchorSelect").value : "auto",
    coverage: parseFloat($("coverage").value),
    min_gain: parseInt($("minGain").value, 10),
    max_cost: parseFloat($("maxCost").value),
    field_budget: parseInt($("fieldBudget").value, 10),
    max_areas: parseInt($("maxAreas").value, 10),
  };
}

async function loadSample({ silent = false } = {}) {
  try {
    const res = await fetch("/api/sample");
    const data = await res.json();
    $("ddlInput").value = data.ddl;
    if (!silent) {
      toast("예제를 불러왔습니다.");
      runFold();
    }
  } catch {
    if (!silent) toast("예제를 불러오지 못했습니다.");
  }
}

async function runFold() {
  $("headlineEmpty").hidden = false;
  $("headlineEmpty").textContent = "데이터베이스를 읽는 중입니다…";
  $("headlineBody").hidden = true;

  try {
    const res = await fetch("/api/fold", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readSettings()),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "묶기에 실패했습니다.");
    foldData = data;
    render(data);
  } catch (err) {
    $("headlineEmpty").hidden = false;
    $("headlineEmpty").textContent = err.message;
    toast(err.message);
  }
}

async function runExpand() {
  const sql = $("logicalSql").value.trim();
  if (!sql) return;

  try {
    const res = await fetch("/api/expand", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...readSettings(), sql }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "바꾸지 못했습니다.");

    $("expandedSql").textContent = data.expanded_sql;
    $("expandStat").hidden = false;
    $("expandStat").textContent =
      data.joins_pruned > 0
        ? `필요한 연결 ${data.joins_emitted}개만 만들었습니다. ${data.joins_pruned}개는 이 질문에 필요 없어 생략했습니다.`
        : `필요한 연결 ${data.joins_emitted}개를 만들었습니다.`;
  } catch (err) {
    $("expandedSql").textContent = err.message;
    $("expandStat").hidden = true;
  }
}

async function copySql() {
  try {
    await navigator.clipboard.writeText($("expandedSql").textContent);
    toast("복사했습니다.");
  } catch {
    toast("복사하지 못했습니다.");
  }
}

// ── 렌더 ──────────────────────────────────────────────────────────────────────

function render(data) {
  renderHeadline(data);
  renderModels(data);
  renderLeftover(data);
  window.renderLineage?.(data.lineage);
  window.renderFidelity?.(data.fidelity);
  renderPresets(data);
  renderAdvanced(data);
}

function renderHeadline(data) {
  const size = data.size;
  const tier = data.tier_summary;

  $("headlineEmpty").hidden = true;
  $("headlineBody").hidden = false;
  $("factsRow").hidden = false;

  // 읽는 쪽이 실제로 지불하는 것은 조인 횟수다. 글자 수는 그 다음이고,
  // 늘어날 때도 있다 — 늘었으면 늘었다고 쓴다.
  const joins = data.physical.declared_fk_count + data.physical.inferred_fk_count;
  $("beforeJoins").textContent = num(joins);

  // 오른쪽 큰 숫자는 "답할 수 있느냐"다. 글자 수가 아니다.
  //
  // 글자 수를 여기 두면 정직하지 않게 읽힌다. 묶는 방식에 따라 설명은 길어지기도
  // 하는데(기준 정보 중심으로 묶으면 그렇다), 그 대가로 못 묻던 질문이 답이 된다.
  // "설명 +256%" 만 크게 보이면 나빠진 것처럼 보이지만 실제로는 사각지대가
  // 사라진 것이다. 그래서 길이는 아래 한 줄로 내리고 여기엔 답변 가능률을 둔다.
  const fid = data.fidelity;
  const answerable = Math.round(fid.pair_answerability * 100);
  $("sizeDelta").textContent = `${answerable}%`;
  $("sizeDelta").className = `ba-gain-num ${
    answerable >= 80 ? "good" : answerable >= 50 ? "warn" : ""
  }`;
  $("sizeLabel").textContent = "조인 없이 답할 수 있는 질문";

  const before = size.ddl_chars || 0;
  const after = size.core_prompt_chars || 0;
  const lengthNote =
    before > 0
      ? (() => {
          const delta = Math.round(((before - after) / before) * 100);
          return delta >= 0
            ? `AI에게 주는 설명은 ${num(after)}자로, 원래 정의문보다 ${delta}% 짧습니다.`
            : `AI에게 주는 설명은 ${num(after)}자입니다. 원래 정의문보다 ${Math.abs(delta)}% 길지만, 그만큼 조인 없이 답할 수 있는 질문이 늘어납니다.`;
        })()
      : `AI에게 주는 설명은 ${num(after)}자입니다.`;

  $("headlinePlain").textContent =
    `${num(tier.total_physical_tables_count)}개 테이블을 ${num(tier.tier1_core_models_count)}개의 넓은 표로 묶었습니다. ` +
    lengthNote;

  $("factModels").textContent = num(tier.tier1_core_models_count);
  $("factCovered").textContent = num(tier.tier1_covered_physical_tables_count);
  $("factCoveredHelp").textContent = `전체 ${num(tier.total_physical_tables_count)}개 중`;
  $("factEdge").textContent = num(tier.tier2_edge_tables_count);
  $("factLinks").textContent = num(data.physical.inferred_fk_count);
}

function renderModels(data) {
  const models = data.logical.models;
  const box = $("modelList");

  if (!models.length) {
    box.innerHTML = '<div class="empty">묶인 모델이 없습니다.</div>';
    return;
  }

  box.innerHTML = models
    .map((m, i) => {
      const counts = countKinds(m.fields);
      const total = m.fields.length || 1;
      const bar = ["base", "joined", "aggregated", "filter"]
        .filter((k) => counts[k])
        .map(
          (k) =>
            `<span class="seg seg-${k}" style="width:${(counts[k] / total) * 100}%" title="${KIND[k].label} ${counts[k]}개"></span>`
        )
        .join("");

      return `
        <button class="model-card" data-i="${i}">
          <div class="model-top">
            <span class="model-name">${esc(m.name)}</span>
            <span class="model-count">${num(m.fields.length)}개 항목</span>
          </div>
          <p class="model-grain">${esc(m.base_table)} 1건이 한 줄 · ${num(m.absorbed_tables.length + 1)}개 표를 담음</p>
          <div class="bar">${bar}</div>
          <div class="legend">
            ${["base", "joined", "aggregated", "filter"]
              .filter((k) => counts[k])
              .map((k) => `<span><i class="dot dot-${k}"></i>${KIND[k].label} ${counts[k]}</span>`)
              .join("")}
          </div>
        </button>`;
    })
    .join("");

  box.querySelectorAll(".model-card").forEach((btn) => {
    btn.addEventListener("click", () => openSheet(models[Number(btn.dataset.i)]));
  });
}

function renderLeftover(data) {
  const names = data.tier2_edge_tables.map((t) => t.name);
  const box = $("leftoverBox");
  box.hidden = names.length === 0;
  if (!names.length) return;

  $("leftoverCount").textContent = `${num(names.length)}개`;
  $("leftoverChips").innerHTML = names
    .map((n) => `<span class="chip">${esc(n)}</span>`)
    .join("");
}

/**
 * 예시 SQL 은 실제 레이어의 필드 이름에서 만든다.
 *
 * 고정 문자열로 두면 필드 이름이 바뀌는 순간 조용히 깨진다 — 실제로 그렇게 깨져
 * 있었다. 화면이 자기 데이터에서 예시를 만들면 그럴 일이 없다.
 */
/** 예시로 쓰기 좋은 모델인지. 종류가 고루 있고 필드가 많을수록 높다. */
function score(model) {
  const counts = countKinds(model.fields);
  const variety = Object.values(counts).filter((n) => n > 0).length;
  return variety * 100 + model.fields.length;
}

function renderPresets(data) {
  const box = document.querySelector(".presets");
  box.innerHTML = '<span class="presets-label">예시:</span>';

  // 첫 모델이 아니라 *보여 줄 것이 있는* 모델을 고른다. 스타 스키마의 첫 모델은
  // 컬럼 서너 개짜리 팩트라, 그걸로 예시를 만들면 "한 항목만" 하나가 전부다.
  const model = data.logical.models
    .slice()
    .sort((a, b) => score(b) - score(a))[0];
  if (!model) return;

  const used = new Set();
  const pick = (test) => {
    const found = model.fields.find((f) => !used.has(f.name) && test(f));
    if (found) used.add(found.name);
    return found;
  };

  const label = pick((f) => f.kind !== "aggregated" && !f.filter_only && /char|text/i.test(f.type));
  const measure = pick((f) => f.kind === "aggregated" && !f.filter_only && f.source.aggregate === "sum");
  const count = pick((f) => f.kind === "aggregated" && f.source.aggregate === "count");
  const plain = pick((f) => f.kind === "base" && !f.filter_only);

  const samples = [];
  if (label && measure) {
    samples.push([
      `${label.name}별 합계`,
      `SELECT ${label.name}, SUM(${measure.name}) AS 합계 FROM ${model.name} GROUP BY ${label.name}`,
    ]);
  }
  if (count) {
    samples.push([`줄 수 세기`, `SELECT ${count.name} FROM ${model.name}`]);
  }
  if (label && plain) {
    samples.push([
      `항목 두 개만`,
      `SELECT ${label.name}, ${plain.name} FROM ${model.name}`,
    ]);
  }
  if (!samples.length && plain) {
    samples.push([`한 항목만`, `SELECT ${plain.name} FROM ${model.name}`]);
  }

  samples.forEach(([title, sql], i) => {
    const btn = document.createElement("button");
    btn.className = "chip-btn";
    btn.textContent = title;
    btn.addEventListener("click", () => {
      $("logicalSql").value = sql;
      runExpand();
    });
    box.appendChild(btn);
    if (i === 0) $("logicalSql").value = sql;
  });

  if (samples.length) runExpand();
}

function renderAdvanced(data) {
  $("promptText").textContent = data.prompt_text || "아직 결과가 없습니다.";

  const fks = data.physical.inferred_fks;
  $("fkBody").innerHTML = fks.length
    ? fks
        .map(
          (fk) => `<tr>
            <td>${esc(fk.from_table)}</td>
            <td><code>${esc(fk.from_columns.join(", "))}</code></td>
            <td>${esc(fk.to_table)}<span class="dim">.${esc(fk.to_columns.join(", "))}</span></td>
            <td>${Math.round(fk.confidence * 100)}%</td>
          </tr>`
        )
        .join("")
    : '<tr><td colspan="4" class="empty">되찾은 연결이 없습니다. 외래 키가 이미 선언돼 있습니다.</td></tr>';

  $("tableBody").innerHTML = data.physical.tables
    .map(
      (t) => `<tr>
        <td>${esc(t.name)}</td>
        <td><span class="tag tag-${t.role}">${t.role === "fact" ? "사건 기록" : t.role === "dimension" ? "기준 정보" : "홀로 있음"}</span></td>
        <td>${t.fact_score.toFixed(2)}</td>
        <td>${num(t.column_count)}</td>
        <td>${num(t.in_degree)}</td>
        <td>${num(t.out_degree)}</td>
      </tr>`
    )
    .join("");
}

// ── 모델 상세 ─────────────────────────────────────────────────────────────────

function openSheet(model) {
  $("sheetName").textContent = model.name;
  $("sheetDesc").textContent = `${model.base_table} 1건이 한 줄입니다. ${
    model.absorbed_tables.length
      ? `${model.absorbed_tables.join(", ")}의 내용이 이미 붙어 있습니다.`
      : "붙여 온 표는 없습니다."
  }`;

  const groups = ["base", "joined", "aggregated", "filter"];
  $("sheetBody").innerHTML = groups
    .map((kind) => {
      const fields = model.fields.filter((f) => kindOf(f) === kind);
      if (!fields.length) return "";
      return `
        <section class="sheet-group">
          <h4><i class="dot dot-${kind}"></i>${KIND[kind].label} <span class="dim">${num(fields.length)}개</span></h4>
          <p class="sheet-help">${KIND[kind].help}</p>
          <div class="field-grid">
            ${fields
              .map(
                (f) => `<div class="field">
                  <div class="field-name">${esc(f.name)}</div>
                  <div class="field-src">${esc(sourceOf(f))}</div>
                </div>`
              )
              .join("")}
          </div>
        </section>`;
    })
    .join("");

  $("sheet").hidden = false;
}

function closeSheet() {
  $("sheet").hidden = true;
}

function sourceOf(f) {
  const s = f.source;
  if (s.aggregate === "count") return `${s.table}의 줄 수`;
  if (s.aggregate) return `${s.table}.${s.column}을 ${s.aggregate === "sum" ? "합산" : s.aggregate}`;
  return `${s.table}.${s.column}`;
}

function kindOf(f) {
  return f.filter_only ? "filter" : f.kind;
}

function countKinds(fields) {
  const out = { base: 0, joined: 0, aggregated: 0, filter: 0 };
  fields.forEach((f) => {
    out[kindOf(f)] = (out[kindOf(f)] || 0) + 1;
  });
  return out;
}

// ── 알림 ──────────────────────────────────────────────────────────────────────

let toastTimer;
function toast(msg) {
  $("toast").textContent = msg;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 3000);
}

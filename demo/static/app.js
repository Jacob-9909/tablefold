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
  // 자동 탐색은 처음에 돌리지 않는다. 그렸던 숫자가 몇 초 뒤 스스로 바뀌면
  // 읽는 사람은 측정값이 아니라 화면 결함으로 본다. 탐색은 버튼을 누른 사람이
  // 기다리겠다고 선언했을 때만 한다.
  runFold();
});



/** 연결된 데이터베이스가 있으면 그것을 기본으로, 없으면 예제로 내려앉는다. */
async function discoverSources() {
  const option = $("sourceSelect").querySelector('option[value="live"]');
  const financial = $("sourceSelect").querySelector('option[value="financial"]');
  try {
    const res = await fetch("/api/sources");
    const info = await res.json();
    liveAvailable = Boolean(info.live_available);
    // 금융 소스도 같은 접속을 쓴다. 고를 수 있게 두면 누르는 순간 503 이다.
    if (financial) financial.disabled = !info.financial_available;
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
    if (financial) financial.disabled = true;
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

// 골드셋이 있어야 곡선을 잴 수 있다. 없는 설치에서는 버튼을 숨긴다 —
// 눌러도 404 가 나는 버튼을 보여 주는 것보다 낫다.
async function syncCurveButton() {
  const btn = $("runCurve");
  if (!btn) return;
  try {
    const info = await (await fetch("/api/goldset")).json();
    btn.hidden = !info.available;
    if (info.available) {
      btn.title = `${info.name} — ${info.cases}건 · 주제 ${info.subjects}개`;
    }
  } catch (e) {
    btn.hidden = true;
  }
}

async function runCurve() {
  const btn = $("runCurve");
  const block = $("curveBlock");
  const body = $("curveBody");
  if (!btn || !block || !body) return;

  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "재는 중...";
  try {
    const res = await fetch("/api/curve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readSettings()),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    body.innerHTML = "";
    for (const p of data.points) {
      const row = document.createElement("tr");
      if (p.prompt_budget === data.knee) row.className = "is-knee";
      const cells = [
        p.prompt_budget.toLocaleString(),
        p.prompt_length.toLocaleString(),
        p.models,
        p.fields,
        `${p.answered}/${p.total}`,
        `${p.answered_subjects}/${p.total_subjects}`,
      ];
      for (const value of cells) {
        const td = document.createElement("td");
        td.textContent = value;
        row.appendChild(td);
      }
      // 곡선의 한 점을 그대로 적용할 수 있어야 곡선이 결정으로 이어진다.
      const apply = document.createElement("td");
      apply.className = "curve-apply";
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = p.prompt_budget === data.knee ? "포화 · 적용" : "적용";
      button.addEventListener("click", () => {
        $("promptBudget").value = p.prompt_budget;
        runFold();
      });
      apply.appendChild(button);
      row.appendChild(apply);
      body.appendChild(row);
    }
    block.hidden = false;
  } catch (e) {
    showToast("곡선 측정 실패");
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function bindEvents() {
  syncGreedyKnobs();
  syncCurveButton();
  if ($("runCurve")) $("runCurve").addEventListener("click", runCurve);
  $("sourceSelect").addEventListener("change", () => {
    syncSourceUI();
    syncGreedyKnobs();
    runFold();
  });
  $("anchorSelect").addEventListener("change", () => {
    syncGreedyKnobs();
    runFold();
  });
  $("loadSample").addEventListener("click", () => loadSample());
  $("runFold").addEventListener("click", runFold);
  if ($("runAutoTune")) {
    $("runAutoTune").addEventListener("click", runAutoTune);
  }
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

async function runAutoTune(e) {
  if (e) {
    e.preventDefault();
    e.stopPropagation();
  }
  const details = document.querySelector("details.advanced");
  if (details) details.open = true;

  const btn = $("runAutoTune");
  if (!btn) return;
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⚡ AI 파라미터 최적화 탐색 중...";


  const progressBox = $("autotuneProgress");
  const statusText = $("autotuneStatusText");
  const scoreText = $("autotuneScoreText");
  const progressBar = $("autotuneProgressBar");
  const detailText = $("autotuneDetailText");

  if (progressBox) progressBox.hidden = false;
  if (progressBar) progressBar.style.width = "0%";
  if (statusText) statusText.textContent = "📊 AI 파라미터 최적화 탐색 준비 중... [0 / 24]";
  if (scoreText) scoreText.textContent = "최고 점수: -";
  if (detailText) detailText.textContent = "탐색 시작...";

  try {
    const payload = {
      source: $("sourceSelect").value,
      ddl: $("ddlInput") ? $("ddlInput").value : ""
    };
    const res = await fetch("/api/autotune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      btn.disabled = false;
      btn.textContent = originalText;
      if (progressBox) progressBox.hidden = true;
      showToast("자동 탐색 실패 (서버 오류)");
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let finalResult = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const item = JSON.parse(line);
          if (item.event === "progress") {
            if (statusText) statusText.textContent = `📊 AI 파라미터 최적화 탐색 중... [${item.current} / ${item.total} (${item.pct}%)]`;
            if (progressBar) progressBar.style.width = `${item.pct}%`;
            if (scoreText && item.current_best_score > 0) scoreText.textContent = `최고 점수: ${item.current_best_score}/100`;
            if (detailText) detailText.textContent = `측정 조건: ${item.evaluating}`;
          } else if (item.event === "done") {
            finalResult = item.result;
          }
        } catch (e) {
          console.error("JSON parse error:", e);
        }
      }
    }

    btn.disabled = false;
    btn.textContent = originalText;

    if (finalResult) {
      // 앵커 모드를 먼저 맞춘다. 스칼라 다섯 개로는 스타 프리셋을 재현할 수 없다 —
      // 다른 셀렉터이지 같은 셀렉터의 다른 설정이 아니다. 이 줄이 없던 동안
      // 화면은 스타 프리셋의 점수를 띄운 뒤 탐욕 폴드를 그렸다.
      if (finalResult.anchor_mode) {
        $("anchorSelect").value = finalResult.anchor_mode;
      }
      $("maxAreas").value = finalResult.max_areas;
      // 문자 예산으로 넣는다. 이긴 레이어가 실제로 찍은 길이라 그대로 재현된다 —
      // 필드 수로 되돌리면 이름·주석 길이 차이만큼 어긋난다.
      if (finalResult.prompt_budget) $("promptBudget").value = finalResult.prompt_budget;
      $("minGain").value = finalResult.min_gain;
      $("maxCost").value = finalResult.max_cost;
      $("coverage").value = finalResult.coverage;

      if (progressBar) progressBar.style.width = "100%";
      if (statusText) statusText.textContent = `✅ 파라미터 최적화 탐색 완료 [24 / 24 (100%)]`;
      if (scoreText) scoreText.textContent = `최종 최고 점수: ${finalResult.score}/100`;
      if (detailText) detailText.textContent = finalResult.reason;

      showToast(`🎯 최적 파라미터 적용 완료! (점수 ${finalResult.score}/100) — ${finalResult.reason}`);
      runFold();
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = originalText;
    if (progressBox) progressBox.hidden = true;
    showToast("네트워크 오류: " + err.message);
  }
}



function readSettings() {
  const source = $("sourceSelect").value;
  const isLive = source === "live" || source === "financial";
  const promptBudget = parseInt($("promptBudget").value, 10);
  return {
    ddl: source === "ddl" ? $("ddlInput").value : "",
    source,
    anchor_mode: isLive ? $("anchorSelect").value : "auto",
    coverage: parseFloat($("coverage").value),
    min_gain: parseInt($("minGain").value, 10),
    max_cost: parseFloat($("maxCost").value),
    prompt_budget: Number.isFinite(promptBudget) ? promptBudget : null,
    max_areas: parseInt($("maxAreas").value, 10),
    monthly_summaries: $("monthlySummaries") ? $("monthlySummaries").checked : false,
  };
}

// 탐욕 선택기에서만 작동하는 노브. 앵커를 이름으로 지목하는 모드(star/mixed/dim/
// fact)에서는 고를 여지가 없어 돌려도 아무것도 안 바뀐다. 안 알려 주면 사용자는
// "값을 바꿨는데 결과가 그대로"인 이유를 알 방법이 없다.
function syncGreedyKnobs() {
  const source = $("sourceSelect").value;
  const isLive = source === "live" || source === "financial";
  const mode = isLive ? $("anchorSelect").value : "auto";
  const inert = mode !== "auto";
  document.querySelectorAll(".greedy-only").forEach((el) => {
    el.classList.toggle("is-inert", inert);
    const input = el.querySelector("input");
    if (input) input.disabled = inert;
  });
  const note = $("greedyNote");
  if (note) note.hidden = !inert;
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
    renderExpandWarnings(data.warnings);
  } catch (err) {
    $("expandedSql").textContent = err.message;
    $("expandStat").hidden = true;
    renderExpandWarnings([]);
  }
}

/** 확장 경고. 오류가 아니라 주의이므로 결과 SQL 아래에 작게 나열한다.
 *  값의 범위가 섞인 답은 실행해도 에러가 나지 않아, 여기서 말해 주지 않으면
 *  읽는 사람은 치우친 숫자를 그대로 믿는다. */
function renderExpandWarnings(warnings) {
  const box = $("expandWarnings");
  if (!box) return;
  const list = Array.isArray(warnings) ? warnings : [];
  box.hidden = list.length === 0;
  box.innerHTML = list
    .map((w) => `<div class="expand-warning">⚠ ${esc(w)}</div>`)
    .join("");
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
  renderCompression(data.compression, data.information);
  renderPresets(data);
  renderAdvanced(data);
}

/**
 * 컬럼 압축 스트립. "무엇이 줄었나"의 반대편 축인 "몇 개가 몇 개로 접혔나".
 *
 * 비율 하나만 크게 보면 근거가 안 보여서 믿기 어렵다. 물리 컬럼 수와 논리
 * 필드 수를 나란히 놓고, 아래에 모델별 유입 통로를 막대로 보여 준다.
 */
function renderCompression(comp, info) {
  const panel = $("compressionPanel");
  if (!panel) return;
  if (!comp || !comp.models || !comp.models.length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  // 비율이 1을 넘으면 압축이다. 넘지 못하면 같은 컬럼이 합계·조건 등 여러
  // 통로로 나뉜 것이라서 항목 수가 컬럼 수보다 많아진다 — 이걸 "압축됐다"고
  // 쓰면 거짓말이 된다. 보이는 대로 쓴다.
  const ratioText =
    comp.ratio >= 1.5
      ? `${comp.ratio}:1 압축`
      : comp.ratio >= 1
        ? `${comp.ratio}:1`
        : `항목 1개당 물리 컬럼 ${(comp.logical_fields / (comp.physical_columns || 1)).toFixed(1)}개`;

  let infoLine = "";
  if (info && info.duplication_factor != null) {
    const dup =
      info.duplication_factor > 1
        ? `같은 컬럼이 평균 ${info.duplication_factor}개 통로로 복제돼 있습니다`
        : `컬럼 중복 없이 1:1 로 옮겼습니다`;
    if (info.measured && info.retention != null) {
      const pct = Math.round(info.retention * 100);
      infoLine = `정보 보존율 ${pct}% (원본 ${num(Math.round(info.source_bits))}비트 → 노출 ${num(Math.round(info.exposed_bits))}비트) · ${dup}.`;
    } else {
      const skipped = (info.unmeasured_tables || []).length;
      infoLine =
        `${dup}` +
        (skipped
          ? ` · 가상 표 ${skipped}개는 카디널리티를 측정할 수 없어 비트 보존율이 생략됩니다.`
          : " · 데이터에 연결되면 비트 단위 정보 보존율까지 측정됩니다.");
    }
  }

  $("compressionStrip").innerHTML = `
    <div class="comp-ratio">
      <span class="comp-num">${num(comp.physical_columns)}</span>
      <span class="comp-arrow">→</span>
      <span class="comp-num comp-num-after">${num(comp.logical_fields)}</span>
      <span class="comp-label">물리 컬럼 → 논리 항목 · ${ratioText}</span>
    </div>
    ${infoLine ? `<p class="comp-info">${esc(infoLine)}</p>` : ""}
    <div class="comp-models">
      ${comp.models
        .map((m) => {
          const width = Math.min(100, (m.logical_fields / (comp.logical_fields || 1)) * 100);
          const title = `${m.name}: 표 ${m.source_tables}개에서 ${m.logical_fields}개 · 최대 ${m.max_hops}홉`;
          return `<div class="comp-row" title="${esc(title)}">
            <span class="comp-name">${esc(m.name)}</span>
            <span class="comp-bar"><i style="width:${width}%"></i></span>
            <span class="comp-val">${num(m.source_tables)}표→${num(m.logical_fields)}항목</span>
          </div>`;
        })
        .join("")}
    </div>`;
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
  //
  // 두 지표 중 **낮은 쪽**을 쓴다. 둘은 서로 다른 방식으로 답을 막고, 어느 쪽이든
  // 막히면 그 질문은 답이 없다. 쌍만 보고 100% 를 크게 띄우던 동안 실제 생성은
  // 10/15 였다 — 거래처와 계정 속성이 조건으로만 걸려 "…별" 질문이 전부 죽었다.
  const fid = data.fidelity;
  const groupRate = fid.table_groupability ?? 1;
  const answerable = Math.round(Math.min(fid.pair_answerability, groupRate) * 100);
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

  // 담긴 표의 수를 쓴다. 예전에는 "묶인 테이블 19 / 전체 19개 중"을 썼는데,
  // 그건 커버리지지 압축이 아니다. 모델 수와 나란히 놓으면 두 19가 붙어서
  // 아무것도 안 한 것처럼 읽힌다. 실제로 줄어드는 것은 읽는 쪽이 쓰는 조인이고,
  // 그걸 만드는 것은 모델 하나가 몇 개의 표를 안고 있느냐다.
  const models = data.logical.models;
  const absorbed = models.reduce((n, m) => n + m.absorbed_tables.length + 1, 0);
  const avg = models.length ? absorbed / models.length : 0;
  const widest = models.reduce(
    (best, m) => (m.absorbed_tables.length > best.absorbed_tables.length ? m : best),
    models[0] || { name: "", absorbed_tables: [] }
  );
  $("factCovered").textContent = avg.toFixed(1);
  $("factCoveredHelp").textContent = widest.name
    ? `가장 많은 ${widest.name}는 ${num(widest.absorbed_tables.length + 1)}개`
    : "";

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

let sheetReturnFocus = null;

function openSheet(model) {
  // 닫은 뒤 어디로 돌아갈지 미리 기억해 둔다. 초점이 시트 안에 갇히지 않으면,
  // 키보드만 쓰는 사람은 닫은 후 자기가 어디쯤 왔는지 잃어버린다.
  sheetReturnFocus = document.activeElement;
  $("sheetName").textContent = model.name;
  $("sheetDesc").textContent = `${model.base_table} 1건이 한 줄입니다. ${
    model.absorbed_tables.length
      ? `${model.absorbed_tables.join(", ")}의 내용이 이미 붙어 있습니다.`
      : "붙여 온 표는 없습니다."
  }`;

  // 컬럼 흐름도: 어느 표의 컬럼이 몇 개, 어떤 통로로 들어왔는지.
  window.renderColumnFlow?.($("sheetFlow"), model);

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
  // 초점을 시트 안으로 옮긴다. 열렸다는 것을 스크린리더와 키보드가 알아야
  // Esc 나 닫기 버튼이 다음 동작이 된다.
  $("sheetClose").focus();
}

function closeSheet() {
  const wasOpen = !$("sheet").hidden;
  $("sheet").hidden = true;
  if (wasOpen && sheetReturnFocus && document.contains(sheetReturnFocus)) {
    sheetReturnFocus.focus();
  }
  sheetReturnFocus = null;
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

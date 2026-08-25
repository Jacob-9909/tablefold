/* tablefold 데모 — 계보 ERD 와 반영도 게이지.
 *
 * 외부 라이브러리를 쓰지 않는다. 순수 SVG DOM API 로만 그린다.
 */

(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (str) => String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const num = (n) => Number(n || 0).toLocaleString("ko-KR");

  /** 0.8 이상 초록, 0.5~0.8 노랑, 미만 빨강. */
  function getGaugeColor(ratio) {
    if (ratio >= 0.8) return "var(--good, #1f7a4d)";
    if (ratio >= 0.5) return "var(--warn, #8a6516)";
    return "var(--bad, #a33a2e)";
  }

  // ── 계보를 ERD 로 그린다 ────────────────────────────────────────────────
  //
  // 방사형 노드 그래프로 그렸을 때는 "무엇이 무엇에 붙었나"만 보이고 "어느 컬럼이
  // 어느 컬럼에 붙었나"가 안 보였다. 사람들이 DB 다이어그램에서 실제로 읽는 것은
  // 뒤쪽이므로, 표를 상자로 두고 컬럼을 줄로 세운 뒤 줄과 줄을 잇는다.
  //
  // 배치는 세 칸이다. 가운데가 기준 표, 오른쪽이 그대로 붙은 표(N:1), 왼쪽이 미리
  // 합계 낸 표(1:N). 방향이 뜻을 담는다 — 오른쪽은 값을 가져오는 쪽, 왼쪽은 여러
  // 줄을 접어 넣는 쪽이다.

  const NS = "http://www.w3.org/2000/svg";

  const BOX_W = 210;      // 상자 가로
  const ROW_H = 21;       // 컬럼 한 줄 높이
  const HEAD_H = 28;      // 상자 머리(표 이름) 높이
  const COL_GAP = 120;    // 칸 사이 가로 간격
  const BOX_GAP = 22;     // 같은 칸 안 상자 사이 세로 간격
  const PAD = 18;
  const MAX_ROWS = 9;     // 상자 하나에 보여 줄 컬럼 수. 넘으면 "외 N개"

  function el(name, attrs = {}) {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  }

  /** 한 소스(표)가 이 모델에 넣은 컬럼 줄들. 조인 키를 맨 앞에 둔다. */
  function rowsOf(source) {
    const keyNames = new Set(
      (source.join_columns || []).map((c) => String(c).split(".").pop().toLowerCase())
    );
    const seen = new Set();
    const rows = [];

    for (const f of source.fields || []) {
      const col = f.column || f.name;
      if (seen.has(col)) continue;
      seen.add(col);
      rows.push({
        column: col,
        field: f.name,
        type: f.type || "",
        aggregate: f.aggregate || null,
        filter: Boolean(f.filter_only),
        key: keyNames.has(String(col).toLowerCase()),
      });
    }
    // 조인 키가 필드로 안 나온 경우에도 줄을 만든다. 선이 붙을 자리가 필요하다.
    for (const raw of source.join_columns || []) {
      const col = String(raw).split(".").pop();
      if (seen.has(col)) continue;
      seen.add(col);
      rows.unshift({ column: col, field: "", type: "", key: true });
    }
    rows.sort((a, b) => Number(b.key) - Number(a.key));
    return rows;
  }

  function boxHeight(rowCount) {
    return HEAD_H + Math.min(rowCount, MAX_ROWS) * ROW_H +
      (rowCount > MAX_ROWS ? ROW_H : 0);
  }

  /** 상자 하나를 그린다. 각 줄의 y 좌표를 돌려줘야 선을 붙일 수 있다. */
  function drawBox(svg, box, onPick) {
    const g = el("g", {
      class: "erd-box",
      "data-table": box.table,
      // 마우스 없이도 상자를 고를 수 있어야 한다. 탭으로 초점을 받고
      // Enter·스페이스로 누른다 — 클릭으로만 고를 수 있던 것의 최소 수정.
      tabindex: "0",
      role: "button",
      "aria-label": `${box.table} 표 선택`,
    });

    const height = boxHeight(box.rows.length);
    const tone =
      box.role === "anchor" ? "anchor" : box.role === "aggregated" ? "agg" : "inl";

    g.appendChild(
      el("rect", {
        x: box.x, y: box.y, width: BOX_W, height,
        rx: 7, class: `erd-rect erd-${tone}`,
      })
    );
    g.appendChild(
      el("path", {
        d: `M${box.x} ${box.y + HEAD_H} h${BOX_W}`,
        class: "erd-sep",
      })
    );

    const title = el("text", {
      x: box.x + 11, y: box.y + HEAD_H / 2 + 4, class: `erd-title erd-title-${tone}`,
    });
    title.textContent = box.table;
    g.appendChild(title);

    const badge = el("text", {
      x: box.x + BOX_W - 11, y: box.y + HEAD_H / 2 + 4,
      "text-anchor": "end", class: "erd-badge",
    });
    badge.textContent =
      box.role === "anchor" ? "기준" : box.role === "aggregated" ? "Σ 합계" : `${box.rows.length}`;
    g.appendChild(badge);

    box.rows.slice(0, MAX_ROWS).forEach((row, i) => {
      const y = box.y + HEAD_H + i * ROW_H;
      row.cy = y + ROW_H / 2;

      if (i % 2 === 1) {
        g.appendChild(
          el("rect", { x: box.x + 1, y, width: BOX_W - 2, height: ROW_H, class: "erd-stripe" })
        );
      }

      const name = el("text", { x: box.x + 11, y: y + ROW_H / 2 + 4, class: "erd-col" });
      name.textContent = (row.key ? "◆ " : "") + row.column;
      g.appendChild(name);

      const meta = el("text", {
        x: box.x + BOX_W - 11, y: y + ROW_H / 2 + 4,
        "text-anchor": "end", class: "erd-type",
      });
      meta.textContent = row.filter
        ? "조건전용"
        : row.aggregate
          ? row.aggregate.toUpperCase()
          : shortType(row.type);
      g.appendChild(meta);

      const tip = el("title");
      tip.textContent = row.field
        ? `${row.field} ← ${box.table}.${row.column}${row.type ? ` (${row.type})` : ""}`
        : `${box.table}.${row.column} — 연결에 쓰이는 키`;
      g.appendChild(tip);
    });

    if (box.rows.length > MAX_ROWS) {
      const more = el("text", {
        x: box.x + 11,
        y: box.y + HEAD_H + MAX_ROWS * ROW_H + ROW_H / 2 + 4,
        class: "erd-more",
      });
      more.textContent = `… 외 ${box.rows.length - MAX_ROWS}개 항목`;
      g.appendChild(more);
    }

    // 클릭과 키보드가 같은 동작으로 모인다. 두 갈래가 따로 놀면 나중에
    // 한쪽만 고쳐지는 결함이 생긴다.
    const pick = () => onPick(box.table);
    g.addEventListener("click", pick);
    g.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      pick();
    });
    svg.appendChild(g);
    box.height = height;
  }

  function shortType(type) {
    return String(type || "").replace(/\s*\([^)]*\)/, "").slice(0, 12);
  }

  /** 두 줄 사이를 잇는다. 가로로 나가서 세로로 꺾고 다시 가로로 들어온다. */
  function drawLink(svg, from, to, dashed) {
    const midX = (from.x + to.x) / 2;
    const path = el("path", {
      d: `M${from.x} ${from.y} H${midX} V${to.y} H${to.x}`,
      class: `erd-link${dashed ? " erd-link-agg" : ""}`,
      fill: "none",
    });
    svg.appendChild(path);

    svg.appendChild(el("circle", { cx: from.x, cy: from.y, r: 3, class: "erd-dot" }));
    svg.appendChild(el("circle", { cx: to.x, cy: to.y, r: 3, class: "erd-dot" }));

    if (dashed) {
      const badge = el("g");
      badge.appendChild(
        el("circle", { cx: midX, cy: (from.y + to.y) / 2, r: 9, class: "erd-sigma-bg" })
      );
      const sigma = el("text", {
        x: midX, y: (from.y + to.y) / 2, class: "erd-sigma",
        "text-anchor": "middle", "dominant-baseline": "central",
      });
      sigma.textContent = "Σ";
      badge.appendChild(sigma);
      svg.appendChild(badge);
    }
  }

  /** 상자에서 선이 붙을 지점. 열쇠 줄이 있으면 그 줄, 없으면 머리 아래. */
  function anchorPoint(box, side) {
    const key = box.rows.slice(0, MAX_ROWS).find((r) => r.key) || box.rows[0];
    const y = key && key.cy ? key.cy : box.y + HEAD_H + ROW_H / 2;
    return { x: side === "left" ? box.x : box.x + BOX_W, y };
  }

  // ── ERD 확대·이동 ────────────────────────────────────────────────────────
  //
  // 큰 모델은 그림이 화면보다 넓다. 스크롤바로 읽던 때는 지금 어디쯤 보고 있나가
  // 감각에 잡히지 않았다. viewBox 를 바꾸는 방식은 그림을 다시 그리지 않고
  // 보는 창만 옮기는 것이므로 좌표 계산이 이 함수 한 곳에 모인다.
  //
  // 리스너는 전부 svg 에 건다. 탭을 옮길 때마다 svg 는 통째로 다시 만들어지므로,
  // 오래된 그림의 리스너가 남아 옛 좌표를 움직이는 일이 없다.
  const ZOOM_MIN = 0.5;    // 자연 크기의 절반까지 축소(배율 하한)
  const ZOOM_MAX = 3;      // 3 배까지 확대
  const WHEEL_STEP = 1.1;  // 휠 한 칸의 배율
  const CLICK_SLOP_PX = 4; // 이보다 적게 움직였으면 누름, 넘으면 이동

  function enableErdNavigation(svg, naturalW, naturalH) {
    let view = { x: 0, y: 0, w: naturalW, h: naturalH };
    const apply = () =>
      svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
    apply();

    const reset = () => {
      view = { x: 0, y: 0, w: naturalW, h: naturalH };
      apply();
    };

    svg.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const rect = svg.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        // 커서 자리를 기준으로 창을 줄이고 늘린다. 가운데 기준이면 커서가
        // 가리키던 곳이 손에서 벗어난다.
        const cx = view.x + ((e.clientX - rect.left) / rect.width) * view.w;
        const cy = view.y + ((e.clientY - rect.top) / rect.height) * view.h;
        const factor = e.deltaY > 0 ? WHEEL_STEP : 1 / WHEEL_STEP;
        const width = Math.min(
          naturalW / ZOOM_MIN,
          Math.max(naturalW / ZOOM_MAX, view.w * factor)
        );
        const k = width / view.w;
        view.x = cx - (cx - view.x) * k;
        view.y = cy - (cy - view.y) * k;
        view.w *= k;
        view.h *= k;
        apply();
      },
      { passive: false }
    );

    // 드래그 이동. 포인터를 캡처해 커서가 그림 밖으로 나가도 잡고 있는다.
    let drag = null;
    let moved = 0;

    svg.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      drag = { px: e.clientX, py: e.clientY, vx: view.x, vy: view.y };
      moved = 0;
      svg.classList.add("is-panning");
      svg.setPointerCapture(e.pointerId);
    });

    svg.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.px;
      const dy = e.clientY - drag.py;
      moved = Math.max(moved, Math.hypot(dx, dy));
      const rect = svg.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      view.x = drag.vx - (dx / rect.width) * view.w;
      view.y = drag.vy - (dy / rect.height) * view.h;
      apply();
    });

    const release = (e) => {
      if (!drag) return;
      drag = null;
      svg.classList.remove("is-panning");
      if (svg.hasPointerCapture && svg.hasPointerCapture(e.pointerId)) {
        svg.releasePointerCapture(e.pointerId);
      }
    };
    svg.addEventListener("pointerup", release);
    svg.addEventListener("pointercancel", release);

    // 문턱을 넘은 손놀림은 선택이 아니라 이동이다. 상자의 click 핸들러보다
    // 먼저(capture) 끊지 않으면 드래그를 마친 자리의 상자가 몰려 선택된다.
    svg.addEventListener(
      "click",
      (e) => {
        if (moved >= CLICK_SLOP_PX) {
          e.stopPropagation();
          e.preventDefault();
        }
      },
      true
    );

    svg.addEventListener("dblclick", reset);
  }

  function drawErd(container, model, detailContainer) {
    container.innerHTML = "";

    const sources = model.sources || [];
    const anchor = sources.find((s) => s.role === "anchor");
    const inlined = sources.filter((s) => s.role === "inlined");
    const aggregated = sources.filter((s) => s.role === "aggregated");

    if (!anchor) {
      container.innerHTML = '<div class="empty">그릴 내용이 없습니다.</div>';
      return;
    }

    const make = (s) => ({ table: s.table, role: s.role, rows: rowsOf(s), source: s });
    const anchorBox = make(anchor);
    const leftBoxes = aggregated.map(make);
    const rightBoxes = inlined.map(make);

    // 세 칸의 높이를 각각 쌓고, 전체를 가장 높은 칸에 맞춘다.
    const stackHeight = (boxes) =>
      boxes.reduce((h, b) => h + boxHeight(b.rows.length) + BOX_GAP, -BOX_GAP);

    const heights = [stackHeight(leftBoxes), boxHeight(anchorBox.rows.length),
      stackHeight(rightBoxes)].map((h) => Math.max(h, 0));
    const canvasH = Math.max(...heights) + PAD * 2;

    const hasLeft = leftBoxes.length > 0;
    const hasRight = rightBoxes.length > 0;
    const columns = 1 + (hasLeft ? 1 : 0) + (hasRight ? 1 : 0);
    const canvasW = columns * BOX_W + (columns - 1) * COL_GAP + PAD * 2;

    const place = (boxes, x, totalH) => {
      let y = PAD + (canvasH - PAD * 2 - totalH) / 2;
      boxes.forEach((b) => {
        b.x = x;
        b.y = y;
        y += boxHeight(b.rows.length) + BOX_GAP;
      });
    };

    const anchorX = PAD + (hasLeft ? BOX_W + COL_GAP : 0);
    place(leftBoxes, PAD, heights[0]);
    place([anchorBox], anchorX, heights[1]);
    place(rightBoxes, anchorX + BOX_W + COL_GAP, heights[2]);

    const svg = el("svg", {
      viewBox: `0 0 ${canvasW} ${canvasH}`,
      width: canvasW,
      height: canvasH,
      class: "erd-svg",
    });

    const pick = (table) => renderLineageDetail(detailContainer, model, table);

    // 선을 먼저 그려 상자 아래로 깔리게 한다.
    const anchorLeft = { ...anchorPoint(anchorBox, "left") };
    const anchorRight = { ...anchorPoint(anchorBox, "right") };

    drawBox(svg, anchorBox, pick);
    leftBoxes.forEach((b) => drawBox(svg, b, pick));
    rightBoxes.forEach((b) => drawBox(svg, b, pick));

    const links = el("g", { class: "erd-links" });
    svg.insertBefore(links, svg.firstChild);

    leftBoxes.forEach((b) => {
      drawLink(links, anchorPoint(b, "right"), { ...anchorPoint(anchorBox, "left") }, true);
    });
    rightBoxes.forEach((b) => {
      drawLink(links, { ...anchorPoint(anchorBox, "right") }, anchorPoint(b, "left"), false);
    });
    void anchorLeft;
    void anchorRight;

    enableErdNavigation(svg, canvasW, canvasH);

    container.appendChild(svg);

    // 휠·드래그·더블클릭은 눈에 보이지 않는 동작이다. 화면에 적어 둔다.
    const hint = document.createElement("p");
    hint.className = "erd-hint";
    hint.textContent = "휠로 확대 · 드래그로 이동 · 두 번 클릭으로 원래대로";
    container.appendChild(hint);

    renderLineageDetail(detailContainer, model, anchorBox.table);
  }

  function renderLineage(lineage) {
    const tabs = $("lineageTabs");
    const caption = $("lineageCaption");
    const chart = $("lineageChart");
    const detail = $("lineageDetail");
    if (!tabs || !chart) return;

    if (!lineage || !Array.isArray(lineage.models) || lineage.models.length === 0) {
      tabs.innerHTML = "";
      if (caption) caption.textContent = "";
      chart.innerHTML = '<div class="empty">아직 결과가 없습니다.</div>';
      if (detail) detail.innerHTML = "";
      return;
    }

    const models = lineage.models;
    let current = 0;

    const draw = () => {
      const model = models[current];
      tabs.querySelectorAll("button").forEach((b, i) => {
        b.classList.toggle("active", i === current);
      });

      const inlined = (model.sources || []).filter((s) => s.role === "inlined").length;
      const agg = (model.sources || []).filter((s) => s.role === "aggregated").length;
      if (caption) {
        caption.textContent =
          `${model.anchor} 1건이 한 줄입니다. ` +
          `실선으로 이어진 ${inlined}개 표는 값을 그대로 가져와 붙였고, ` +
          `점선 Σ로 이어진 ${agg}개 표는 여러 줄을 미리 합계 냈습니다. ` +
          `◆ 표시가 서로를 잇는 열쇠 컬럼입니다.`;
      }

      drawErd(chart, model, detail);
    };

    tabs.innerHTML = "";
    models.forEach((m, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = `${m.name} ${m.field_count}항목/${m.table_count}표`;
      btn.addEventListener("click", () => {
        current = i;
        draw();
      });
      tabs.appendChild(btn);
    });

    draw();
  }

  function renderLineageDetail(detailContainer, model, selectedTableName) {
    const source = model.sources.find((s) => s.table === selectedTableName);
    if (!source) {
      detailContainer.innerHTML = '<div class="empty">선택된 테이블 정보를 찾을 수 없습니다.</div>';
      return;
    }

    let roleBadge = '<span class="role-badge anchor">기준 테이블</span>';
    if (source.role === "inlined") {
      roleBadge = '<span class="role-badge inlined">그대로 붙은 테이블</span>';
    } else if (source.role === "aggregated" || source.cardinality === "one_to_many") {
      roleBadge = '<span class="role-badge aggregated">미리 합계 낸 테이블 (Σ)</span>';
    }

    const fields = source.fields || [];

    let fieldsHtml = "";
    fields.forEach((f) => {
      const isFilter = f.filter_only;
      const isAgg = !!f.aggregate;

      // 이름 및 원본 표시
      let leftName = f.name;
      if (isAgg) {
        leftName = `${f.aggregate.toUpperCase()}(${f.column})`;
      }
      const rightCol = f.column;

      fieldsHtml += `<div class="lineage-field-item${isFilter ? " filter-only" : ""}">
        <div class="lineage-field-top">
          <span class="lineage-field-name">${esc(leftName)}</span>
          ${isFilter ? '<span class="filter-badge">[조건 전용]</span>' : ""}
        </div>
        <span class="lineage-field-source">← ${esc(source.table)}.${esc(rightCol)} (${esc(f.type || "")})</span>
      </div>`;
    });

    detailContainer.innerHTML = `
      <div class="lineage-detail-head">
        <span class="lineage-detail-title">${esc(source.table)} (${fields.length}개 필드 제공)</span>
        ${roleBadge}
      </div>
      <div class="lineage-field-grid">
        ${fieldsHtml || '<div class="empty">제공하는 필드가 없습니다.</div>'}
      </div>
    `;
  }

  // ─────────────────────────────────────────────────────────
  // 2. Fidelity (얼마나 잘 담았나 - 스키마 충실도)
  // ─────────────────────────────────────────────────────────

  /**
   * data.fidelity 를 받아 게이지 3개, 답할 수 없는 조합, 테이블별 보존율을 그린다.
   */
  function renderFidelity(fidelity) {
    const panel = $("fidelityPanel");
    if (!panel) return;

    // 데이터 누락 시 조용히 초기화
    if (!fidelity) {
      panel.innerHTML = '<div class="empty" style="padding:40px; color:var(--ink-3);">아직 결과가 없습니다.</div>';
      return;
    }

    const colRet = fidelity.column_retention ?? 0;
    const joinAbs = fidelity.join_absorption ?? 0;
    const pairAns = fidelity.pair_answerability ?? 0;
    const groupable = fidelity.groupable_tables || [];
    const ungroupable = fidelity.ungroupable || [];
    const groupRate = fidelity.table_groupability ?? 0;
    const counts = fidelity.counts || {};

    const colColor = getGaugeColor(colRet);
    const joinColor = getGaugeColor(joinAbs);
    const pairColor = getGaugeColor(pairAns);
    const groupColor = getGaugeColor(groupRate);

    // 2-1. 게이지 바 HTML 조립
    let html = `
      <h3 class="fidelity-title">얼마나 잘 담았나</h3>
      <p class="fidelity-sub">원래 데이터베이스의 구조와 관계를 논리 모델에 얼마나 보존했는지 측정합니다.</p>
      
      <div class="fidelity-gauges">
        <!-- 1. 컬럼 보존 -->
        <div class="fidelity-gauge-card">
          <div class="gauge-header">
            <span class="gauge-label">컬럼 보존</span>
            <span class="gauge-val" style="color:${colColor}">${(colRet * 100).toFixed(1)}%</span>
          </div>
          <div class="gauge-bar-track">
            <div class="gauge-bar-fill" style="width:${Math.min(100, colRet * 100)}%; background:${colColor};"></div>
          </div>
          <span class="gauge-desc">답이 될 수 있는 ${num(
            (counts.total_columns || 0) - (counts.dropped_noise_columns || 0)
          )}개 중 ${num(counts.exposed_columns)}개를 꺼내 쓸 수 있음${
            counts.dropped_noise_columns
              ? ` (적재 메타 ${num(counts.dropped_noise_columns)}개는 셈에서 제외)`
              : ""
          }</span>
        </div>

        <!-- 2. 조인 흡수 -->
        <div class="fidelity-gauge-card">
          <div class="gauge-header">
            <span class="gauge-label">조인 흡수</span>
            <span class="gauge-val" style="color:${joinColor}">${(joinAbs * 100).toFixed(1)}%</span>
          </div>
          <div class="gauge-bar-track">
            <div class="gauge-bar-fill" style="width:${Math.min(100, joinAbs * 100)}%; background:${joinColor};"></div>
          </div>
          <span class="gauge-desc">${num(counts.total_edges)}개 관계 중 ${num(counts.absorbed_edges)}개가 모델 안으로</span>
        </div>

        <!-- 3. 함께 읽기 -->
        <div class="fidelity-gauge-card">
          <div class="gauge-header">
            <span class="gauge-label">함께 읽기</span>
            <span class="gauge-val" style="color:${pairColor}">${(pairAns * 100).toFixed(1)}%</span>
          </div>
          <div class="gauge-bar-track">
            <div class="gauge-bar-fill" style="width:${Math.min(100, pairAns * 100)}%; background:${pairColor};"></div>
          </div>
          <span class="gauge-desc">${num(counts.askable_pairs)}쌍 중 ${num(counts.answerable_pairs)}쌍을 조인 없이</span>
        </div>

        <!-- 4. 그룹 가능 -->
        <div class="fidelity-gauge-card">
          <div class="gauge-header">
            <span class="gauge-label">그룹 가능</span>
            <span class="gauge-val" style="color:${groupColor}">${(groupRate * 100).toFixed(1)}%</span>
          </div>
          <div class="gauge-bar-track">
            <div class="gauge-bar-fill" style="width:${Math.min(100, groupRate * 100)}%; background:${groupColor};"></div>
          </div>
          <span class="gauge-desc">흡수된 ${num(
            groupable.length + ungroupable.length
          )}표 중 ${num(groupable.length)}표를 GROUP BY 할 수 있음</span>
        </div>
      </div>
    `;

    // "…별" 질문이 못 닿는 표. 흡수는 됐지만 WHERE 로만 걸 수 있다.
    if (ungroupable.length > 0) {
      html += `
        <details class="fidelity-details">
          <summary>GROUP BY 할 수 없는 표 ${ungroupable.length}개 — WHERE 로만 걸 수 있음</summary>
          <div class="chip-cloud">${ungroupable
            .map((t) => `<span class="chip-item">${esc(t)}</span>`)
            .join("")}</div>
        </details>
      `;
    }

    // 2-2. 조인 없이 답할 수 없는 조합 목록 (<details> 접기)
    const unanswerable = fidelity.unanswerable || [];
    if (unanswerable.length > 0) {
      let chipsHtml = "";
      unanswerable.forEach((pair) => {
        if (Array.isArray(pair) && pair.length >= 2) {
          chipsHtml += `<span class="chip-item">${esc(pair[0])} &times; ${esc(pair[1])}</span>`;
        }
      });

      // 목록은 잘려 오므로 개수는 서버가 준 총계를 쓴다. 잘린 길이를 개수로
      // 표시하면 사각지대가 실제보다 작아 보인다.
      const total = fidelity.unanswerable_total ?? unanswerable.length;
      const more = total > unanswerable.length
        ? `<p class="chip-more">… 외 ${num(total - unanswerable.length)}개는 생략했습니다.</p>`
        : "";

      html += `
        <div class="fidelity-section">
          <details class="fidelity-details">
            <summary>조인 없이 답할 수 없는 조합 ${num(total)}개</summary>
            <div class="chip-cloud">
              ${chipsHtml}
            </div>
            ${more}
          </details>
        </div>
      `;
    }

    // 2-3. 테이블별 보존율 상세 표 (<details> 기본 닫힘)
    const tables = (fidelity.tables || []).slice();
    if (tables.length > 0) {
      // 보존율 낮은 순(오름차순) 정렬
      tables.sort((a, b) => (a.retention ?? 0) - (b.retention ?? 0));

      let rowsHtml = "";
      tables.forEach((t) => {
        const inModels = Array.isArray(t.in_models) && t.in_models.length > 0;
        const rowCls = inModels ? "" : " class=\"row-dimmed\"";
        const retPct = ((t.retention ?? 0) * 100).toFixed(1);
        const barColor = getGaugeColor(t.retention ?? 0);

        // 소실된 값 컬럼 추출 (최대 6개 + "외 N개")
        const dropped = (t.dropped_values && t.dropped_values.length > 0) ? t.dropped_values : (t.dropped_keys || []);
        let droppedChips = "";
        const maxChips = 6;

        if (dropped.length > 0) {
          const visible = dropped.slice(0, maxChips);
          visible.forEach((col) => {
            droppedChips += `<span class="chip-item">${esc(col)}</span>`;
          });
          if (dropped.length > maxChips) {
            droppedChips += `<span class="chip-more">외 ${dropped.length - maxChips}개</span>`;
          }
        } else {
          droppedChips = '<span style="color:var(--ink-3); font-size:0.8rem;">없음</span>';
        }

        const unmappedTag = inModels ? "" : '<span class="table-badge-unmapped">어느 모델에도 안 들어감</span>';

        rowsHtml += `<tr${rowCls}>
          <td><b>${esc(t.table)}</b>${unmappedTag}</td>
          <td style="text-align:right">${num(t.total_columns)}개</td>
          <td>
            <span class="mini-bar-track"><span class="mini-bar-fill" style="width:${Math.min(100, retPct)}%; background:${barColor};"></span></span>
            <span style="font-family:var(--mono); font-size:0.85rem;">${retPct}%</span>
          </td>
          <td><div class="chip-cloud" style="margin:0">${droppedChips}</div></td>
        </tr>`;
      });

      html += `
        <div class="fidelity-section">
          <details class="fidelity-details">
            <summary>테이블별 상세 보존율 (${num(tables.length)}개 테이블)</summary>
            <div class="table-scroll" style="margin-top:10px;">
              <table class="data">
                <thead>
                  <tr>
                    <th>테이블</th>
                    <th style="text-align:right">전체 컬럼</th>
                    <th>보존율</th>
                    <th>소실된 컬럼</th>
                  </tr>
                </thead>
                <tbody>
                  ${rowsHtml}
                </tbody>
              </table>
            </div>
          </details>
        </div>
      `;
    }

    panel.innerHTML = html;
  }

  // 전역 함수로 외부에 노출 (script 태그로 먼저 로드되므로 window 객체에 할당)
  window.renderLineage = renderLineage;
  window.renderFidelity = renderFidelity;

  // ── 컬럼 흐름도 (모델 상세) ──────────────────────────────────────────────
  //
  // "이 모델의 항목들이 어느 표에서 어떻게 왔나"를 리본으로 보여 준다. 왼쪽이
  // 물리 표, 오른쪽이 접힌 결과 네 통(기본값 · 붙여 온 값 · 미리 합계 낸 값 ·
  // 조건 전용)이다. 리본 굵기가 컬럼 수다 — 굵은 리본이 압축을 만든 주범이다.
  //
  // ERD 가 "표끼리의 구조"를 그린다면 이것은 "컬럼의 물류"를 그린다. 둘이 같은
  // 데이터를 다르게 읽는 것이다.

  const FLOW_W = 580;
  const NODE_W = 132;
  const ROW = 18;
  const GAP = 10;
  const FLOW_PAD = 14;

  const KIND_META = {
    base: { label: "표 자신의 항목", color: "var(--base, #2f5eaa)" },
    joined: { label: "붙여 온 항목", color: "var(--joined, #4b9b7a)" },
    aggregated: { label: "미리 합계 낸 항목", color: "var(--aggregated, #8a6516)" },
    filter: { label: "조건에만 쓰는 항목", color: "var(--filter, #8a8a82)" },
  };

  function renderColumnFlow(container, model) {
    if (!container || !model || !Array.isArray(model.fields)) return;
    container.innerHTML = "";

    // 표별로 네 통에 몇 개씩 넣었는지 센다.
    const tables = new Map();
    const buckets = ["base", "joined", "aggregated", "filter"];
    const bucketTotals = { base: 0, joined: 0, aggregated: 0, filter: 0 };

    for (const f of model.fields) {
      const kind = f.filter_only ? "filter" : (f.kind || "base");
      const table = (f.source && f.source.table) || "?";
      bucketTotals[kind] += 1;
      let entry = tables.get(table);
      if (!entry) {
        entry = { table, counts: { base: 0, joined: 0, aggregated: 0, filter: 0 } };
        tables.set(table, entry);
      }
      entry.counts[kind] += 1;
    }

    if (!tables.size) {
      container.innerHTML = '<div class="empty">흐름을 그릴 항목이 없습니다.</div>';
      return;
    }

    const leftNodes = [...tables.values()].sort(
      (a, b) => sumCounts(b.counts) - sumCounts(a.counts) || a.table.localeCompare(b.table)
    );
    // 통(桶) 노드도 왼쪽과 같은 모양으로 만들어 같은 배치기를 쓴다.
    const rightNodes = buckets
      .map((k) => ({
        table: KIND_META[k].label,
        kind: k,
        counts: { [k]: bucketTotals[k] },
        isBucket: true,
      }))
      .filter((n) => n.counts[n.kind] > 0);

    const leftH = leftNodes.reduce((h, n) => h + nodeHeight(n) + GAP, -GAP);
    const rightH = rightNodes.reduce((h, n) => h + nodeHeight(n) + GAP, -GAP);
    const canvasH = Math.max(leftH, rightH) + FLOW_PAD * 2 + HEAD_ROOM;
    const midX = FLOW_PAD + NODE_W + (FLOW_W - (NODE_W * 2 + FLOW_PAD * 2)) / 2;

    const svg = el("svg", {
      viewBox: `0 0 ${FLOW_W} ${canvasH}`,
      width: "100%",
      class: "flow-svg",
      role: "img",
      "aria-label": `${model.name} 모델의 컬럼 유입 흐름`,
    });

    // 세로 가운데 정렬로 두 열을 놓는다.
    placeColumn(leftNodes, FLOW_PAD, canvasH);
    placeColumn(rightNodes, FLOW_W - FLOW_PAD - NODE_W, canvasH);

    const linksLayer = el("g", {});
    svg.appendChild(linksLayer);

    // 리본: 표 → 통. 두께는 컬럼 수.
    for (const node of leftNodes) {
      let yFrom = node.y + FLOW_PAD + HEAD_ROOM_INNER;
      for (const target of rightNodes) {
        const count = node.counts[target.kind];
        if (!count) continue;
        const w = Math.max(3, count * RIBBON_PER_COLUMN);
        const y0 = yFrom + w / 2;
        const x1 = FLOW_W - FLOW_PAD - NODE_W;
        const y1 = ribbonOffset(target, count);
        yFrom += w;

        const path = el("path", {
          d: `M${FLOW_PAD + NODE_W} ${y0} C ${midX} ${y0}, ${midX} ${y1}, ${x1} ${y1}`,
          class: "flow-ribbon",
          stroke: KIND_META[target.kind].color,
          "stroke-width": w,
          fill: "none",
        });
        const tip = el("title");
        tip.textContent =
          `${node.table} → ${KIND_META[target.kind].label} ${count}개`;
        path.appendChild(tip);
        linksLayer.appendChild(path);
      }
    }

    drawFlowNode(svg, leftNodes);
    drawFlowNode(svg, rightNodes);

    container.appendChild(svg);

    // 리본 오프셋 계산용: 각 오른쪽 통에서 이미 쓴 두께를 기억한다.
    function ribbonOffset(bucketNode, count) {
      const w = Math.max(3, count * RIBBON_PER_COLUMN);
      const key = bucketNode.kind;
      bucketNode._used = bucketNode._used || {};
      const start = bucketNode._used[key] || 0;
      bucketNode._used[key] = start + w;
      return bucketNode.y + FLOW_PAD + HEAD_ROOM_INNER + start + w / 2;
    }
  }

  const RIBBON_PER_COLUMN = 7;   // 컬럼 하나당 리본 두께(px)
  const HEAD_ROOM = 26;          // 위쪽 여백(제목 없음, 여유만)
  const HEAD_ROOM_INNER = 12;    // 노드 안쪽 첫 리본까지 거리

  function sumCounts(counts) {
    return Object.values(counts).reduce((a, b) => a + b, 0);
  }

  function nodeHeight(node) {
    return Math.max(sumCounts(node.counts) * RIBBON_PER_COLUMN + HEAD_ROOM_INNER * 2, ROW);
  }

  function placeColumn(nodes, x, canvasH) {
    const totalH = nodes.reduce((h, n) => h + nodeHeight(n) + GAP, -GAP);
    let y = Math.max(FLOW_PAD, (canvasH - totalH) / 2);
    for (const n of nodes) {
      n.x = x;
      n.y = y;
      y += nodeHeight(n) + GAP;
    }
  }

  function drawFlowNode(svg, nodes) {
    for (const node of nodes) {
      const h = nodeHeight(node);
      const g = el("g", {});

      const rect = el("rect", {
        x: node.x,
        y: node.y,
        width: NODE_W,
        height: h,
        rx: 6,
        class: `flow-node${node.isBucket ? " flow-node-bucket" : ""}`,
        stroke: node.isBucket ? KIND_META[node.kind].color : undefined,
        fill: node.isBucket ? "var(--panel, #fff)" : "var(--accent-soft, #eaf0fa)",
      });
      g.appendChild(rect);

      const nameText = el("text", {
        x: node.x + 8,
        y: node.y + 16,
        class: "flow-node-name",
      });
      nameText.textContent = truncate(node.table, 17);
      g.appendChild(nameText);

      const countText = el("text", {
        x: node.x + 8,
        y: node.y + 32,
        class: "flow-node-count",
      });
      countText.textContent = `${num(sumCounts(node.counts))}개`;
      g.appendChild(countText);

      svg.appendChild(g);
    }
  }

  function truncate(text, max) {
    const s = String(text ?? "");
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }

  window.renderColumnFlow = renderColumnFlow;
})();

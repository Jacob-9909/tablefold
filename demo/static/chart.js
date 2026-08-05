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
    const g = el("g", { class: "erd-box", "data-table": box.table });

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

    g.addEventListener("click", () => onPick(box.table));
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

    container.appendChild(svg);
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
    const counts = fidelity.counts || {};

    const colColor = getGaugeColor(colRet);
    const joinColor = getGaugeColor(joinAbs);
    const pairColor = getGaugeColor(pairAns);

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

        <!-- 3. 답변 가능 -->
        <div class="fidelity-gauge-card">
          <div class="gauge-header">
            <span class="gauge-label">답변 가능</span>
            <span class="gauge-val" style="color:${pairColor}">${(pairAns * 100).toFixed(1)}%</span>
          </div>
          <div class="gauge-bar-track">
            <div class="gauge-bar-fill" style="width:${Math.min(100, pairAns * 100)}%; background:${pairColor};"></div>
          </div>
          <span class="gauge-desc">${num(counts.askable_pairs)}쌍 중 ${num(counts.answerable_pairs)}쌍을 조인 없이</span>
        </div>
      </div>
    `;

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
})();

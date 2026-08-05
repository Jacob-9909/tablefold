/* tablefold demo chart & fidelity visualizer.
   물리 DB 스키마가 논리 모델로 어떻게 묶였는지와 스키마 보존율을 시각화한다.
   외부 라이브러리 없이 순수 Vanilla JS와 SVG DOM API만 사용한다. */

(function () {
  // DOM 헬퍼 및 문자열 처리 유틸리티
  const $ = (id) => document.getElementById(id);

  // 앵커 사각형의 가로 크기. 노드를 그리는 쪽과 Σ 뱃지를 놓는 쪽이 같은 값을
  // 알아야 뱃지가 사각형 위로 올라타지 않는다.
  const ANCHOR_WIDTH = 130;
  const esc = (str) => String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const num = (n) => Number(n || 0).toLocaleString("ko-KR");

  // 내부 상태 (현재 선택된 모델 인덱스 및 세부 소스)
  let currentModelIndex = 0;
  let currentSelectedTable = null;

  /**
   * 게이지 바 비율에 따른 색상 토큰을 반환한다.
   * 0.8 이상 초록, 0.5~0.8 노랑, 0.5 미만 빨강.
   */
  function getGaugeColor(ratio) {
    if (ratio >= 0.8) return "var(--ok, #2c6e49)";
    if (ratio >= 0.5) return "var(--warn, #8a6516)";
    return "var(--danger, #a83232)";
  }

  // ─────────────────────────────────────────────────────────
  // 1. Lineage (왜 이렇게 묶였나 - 모델별 방사형 지도)
  // ─────────────────────────────────────────────────────────

  /**
   * data.lineage 객체를 받아 모델 탭, 한줄 설명, SVG 지도, 상세 필드 목록을 그린다.
   */
  function renderLineage(lineage) {
    const tabsContainer = $("lineageTabs");
    const captionContainer = $("lineageCaption");
    const chartContainer = $("lineageChart");
    const detailContainer = $("lineageDetail");

    if (!tabsContainer || !captionContainer || !chartContainer || !detailContainer) {
      return;
    }

    // 예외 처리: 데이터가 없는 경우 조용히 초기화 문구만 출력
    if (!lineage || !Array.isArray(lineage.models) || lineage.models.length === 0) {
      tabsContainer.innerHTML = "";
      captionContainer.innerHTML = "";
      chartContainer.innerHTML = '<div class="empty" style="padding:40px; color:var(--ink-3);">아직 결과가 없습니다.</div>';
      detailContainer.innerHTML = "";
      return;
    }

    // 유효한 인덱스로 제한
    if (currentModelIndex >= lineage.models.length) {
      currentModelIndex = 0;
    }

    // 1-1. 모델 탭 렌더링
    let tabsHtml = "";
    lineage.models.forEach((m, idx) => {
      const activeCls = idx === currentModelIndex ? " active" : "";
      const tableCount = m.table_count || (m.sources ? m.sources.length : 0);
      tabsHtml += `<button class="lineage-tab-btn${activeCls}" data-idx="${idx}">
        [${esc(m.name)} ${num(m.field_count)}필드/${tableCount}테이블]
      </button>`;
    });
    tabsContainer.innerHTML = tabsHtml;

    // 탭 클릭 이벤트 바인딩
    tabsContainer.querySelectorAll(".lineage-tab-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const idx = parseInt(e.currentTarget.getAttribute("data-idx"), 10);
        currentModelIndex = idx;
        currentSelectedTable = null; // 모델 변경 시 선택 테이블 초기화
        renderLineage(lineage);
      });
    });

    const model = lineage.models[currentModelIndex];
    if (!model || !Array.isArray(model.sources)) {
      chartContainer.innerHTML = '<div class="empty">모델 상세 정보가 없습니다.</div>';
      return;
    }

    // 1-2. 한 줄 설명 캡션 생성
    const anchorName = model.anchor || model.name;
    const solidCount = model.sources.filter((s) => s.role === "inlined" || (s.role !== "anchor" && s.cardinality !== "one_to_many")).length;
    const dashedCount = model.sources.filter((s) => s.role === "aggregated" || s.cardinality === "one_to_many").length;

    captionContainer.textContent = `${anchorName} 1건이 한 줄. 실선으로 이어진 ${solidCount}개 테이블은 이미 붙어 있고, 점선 Σ 로 이어진 ${dashedCount}개는 미리 합계 낸 값입니다.`;

    // 1-3. 방사형 SVG 그리기
    drawRadialSVG(chartContainer, model, detailContainer);
  }

  /**
   * 선택된 모델을 억지 force-directed 대신 방사형(Radial)으로 그린다.
   * 앵커 노드는 중앙 사각형, 주위 테이블은 홉(hops)별 반지름으로 배치한다.
   */
  function drawRadialSVG(chartContainer, model, detailContainer) {
    const svgWidth = 800;
    const ANCHOR_HALF_WIDTH = ANCHOR_WIDTH / 2;
    const svgHeight = 420;
    const cx = svgWidth / 2;
    const cy = svgHeight / 2;

    const sources = model.sources || [];
    const anchorSource = sources.find((s) => s.role === "anchor" || s.table === model.anchor) || sources[0];
    const otherSources = sources.filter((s) => s !== anchorSource);

    // 필드 개수에 비례하는 노드 반지름 계산 (최소 18, 최대 44)
    let minFields = Infinity;
    let maxFields = -Infinity;
    otherSources.forEach((s) => {
      const fc = s.field_count || (s.fields ? s.fields.length : 1);
      if (fc < minFields) minFields = fc;
      if (fc > maxFields) maxFields = fc;
    });
    if (minFields === Infinity) {
      minFields = 1;
      maxFields = 1;
    }

    function getNodeRadius(fieldCount) {
      if (maxFields === minFields) return 24;
      const ratio = (fieldCount - minFields) / (maxFields - minFields);
      return Math.round(18 + ratio * (44 - 18));
    }

    // 각 노드의 좌표 및 메타데이터 계산
    const nodePositions = new Map();
    // 앵커 노드 좌표 저장
    nodePositions.set(anchorSource.table, {
      x: cx,
      y: cy,
      isAnchor: true,
      source: anchorSource,
    });

    const count = otherSources.length;
    otherSources.forEach((source, idx) => {
      // 12시 방향부터 시계 방향으로 각도 균등 분할
      const angle = -Math.PI / 2 + (idx / count) * (2 * Math.PI);
      
      // 홉스(hops) 기반 링 반지름 분할 (1홉: 135px, 2홉 이상: 220px)
      const hops = source.hops || 1;
      const radiusRing = hops >= 2 ? 220 : 135;

      const x = cx + radiusRing * Math.cos(angle);
      const y = cy + radiusRing * Math.sin(angle);
      const fc = source.field_count || (source.fields ? source.fields.length : 0);
      const r = getNodeRadius(fc);

      nodePositions.set(source.table, {
        x,
        y,
        angle,
        r,
        hops,
        isAnchor: false,
        source,
      });
    });

    // 기본 선택 테이블 설정 (현재 선택된 게 없으면 앵커 테이블)
    if (!currentSelectedTable || !sources.some((s) => s.table === currentSelectedTable)) {
      currentSelectedTable = anchorSource.table;
    }

    // SVG DOM 구조 작성
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${svgWidth} ${svgHeight}`);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "420");

    // 1) 엣지(연결선) 및 뱃지 그리기
    otherSources.forEach((source) => {
      const pos = nodePositions.get(source.table);
      if (!pos) return;

      // 부모 노드 추적 (path가 지정되어 있다면 직전 테이블 연결)
      let parentTable = anchorSource.table;
      if (Array.isArray(source.path) && source.path.length > 0) {
        const lastStep = source.path[source.path.length - 1]; // 예: "orders → customers"
        const parts = lastStep.split("→").map((s) => s.trim());
        if (parts.length >= 2 && nodePositions.has(parts[0])) {
          parentTable = parts[0];
        }
      }
      const parentPos = nodePositions.get(parentTable) || nodePositions.get(anchorSource.table);

      const isAggregated = source.role === "aggregated" || source.cardinality === "one_to_many";

      // 엣지 라인 생성
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", parentPos.x);
      line.setAttribute("y1", parentPos.y);
      line.setAttribute("x2", pos.x);
      line.setAttribute("y2", pos.y);
      line.setAttribute("stroke", isAggregated ? "var(--warn, #8a6516)" : "var(--line, #7d9bc4)");
      line.setAttribute("stroke-width", "1.5");
      if (isAggregated) {
        line.setAttribute("stroke-dasharray", "5 4");
      }
      svg.appendChild(line);

      // cardinality === "one_to_many" 인 경우 엣지 위에 Σ 뱃지 삽입.
      //
      // 중점에 두면 안 된다. 앵커는 원이 아니라 가로로 긴 사각형이라, 좌우로 뻗은
      // 엣지의 중점이 사각형 안쪽에 들어와 뱃지가 글자를 덮는다. 앵커 표면에서
      // 일정 거리 떨어진 지점에 놓아야 방향과 무관하게 겹치지 않는다.
      if (isAggregated) {
        const dx = pos.x - parentPos.x;
        const dy = pos.y - parentPos.y;
        const len = Math.hypot(dx, dy) || 1;
        const clearance = Math.min(
          Math.max(len * 0.5, ANCHOR_HALF_WIDTH + 26),
          len - 24,
        );
        const mx = parentPos.x + (dx / len) * clearance;
        const my = parentPos.y + (dy / len) * clearance;

        const badgeG = document.createElementNS(ns, "g");
        const badgeBg = document.createElementNS(ns, "circle");
        badgeBg.setAttribute("cx", mx);
        badgeBg.setAttribute("cy", my);
        badgeBg.setAttribute("r", "9");
        badgeBg.setAttribute("fill", "#ffffff");
        badgeBg.setAttribute("stroke", "var(--warn, #8a6516)");
        badgeBg.setAttribute("stroke-width", "1.5");

        const badgeText = document.createElementNS(ns, "text");
        badgeText.setAttribute("x", mx);
        badgeText.setAttribute("y", my);
        badgeText.setAttribute("fill", "var(--warn, #8a6516)");
        badgeText.setAttribute("font-size", "11");
        badgeText.setAttribute("font-weight", "bold");
        badgeText.setAttribute("text-anchor", "middle");
        badgeText.setAttribute("dominant-baseline", "central");
        badgeText.textContent = "Σ";

        badgeG.appendChild(badgeBg);
        badgeG.appendChild(badgeText);
        svg.appendChild(badgeG);
      }

      // 조인 컬럼 라벨 (선 중심부에 조인 정보 표기)
      if (Array.isArray(source.join_columns) && source.join_columns.length > 0) {
        const joinCol = source.join_columns[0].split(".").pop(); // "orders.customer_id" -> "customer_id"
        const lx = parentPos.x * 0.65 + pos.x * 0.35;
        const ly = parentPos.y * 0.65 + pos.y * 0.35;

        const labelText = document.createElementNS(ns, "text");
        labelText.setAttribute("x", lx);
        labelText.setAttribute("y", ly - 4);
        labelText.setAttribute("fill", "var(--ink-3, #85857c)");
        labelText.setAttribute("font-size", "10");
        labelText.setAttribute("text-anchor", "middle");
        labelText.textContent = joinCol;
        svg.appendChild(labelText);
      }
    });

    // 2) 노드 그리기 (앵커 노드 및 일반 소스 노드)
    nodePositions.forEach((pos, tableName) => {
      const g = document.createElementNS(ns, "g");
      const isSelected = tableName === currentSelectedTable;
      g.setAttribute("class", `node-group${isSelected ? " selected" : ""}`);

      // 툴팁 작성 (<title> 요소 사용)
      const title = document.createElementNS(ns, "title");
      const fc = pos.source.field_count || (pos.source.fields ? pos.source.fields.length : 0);
      let tooltipText = `테이블: ${tableName}\n필드 수: ${fc}개`;
      if (pos.source.path) tooltipText += `\n경로: ${pos.source.path.join(" → ")}`;
      if (pos.source.join_columns) tooltipText += `\n조인: ${pos.source.join_columns.join(" = ")}`;
      title.textContent = tooltipText;
      g.appendChild(title);

      if (pos.isAnchor) {
        // 가운데 앵커 노드는 큰 사각형으로
        const w = ANCHOR_WIDTH;
        const h = 48;
        const rx = 8;

        const rect = document.createElementNS(ns, "rect");
        rect.setAttribute("x", pos.x - w / 2);
        rect.setAttribute("y", pos.y - h / 2);
        rect.setAttribute("width", w);
        rect.setAttribute("height", h);
        rect.setAttribute("rx", rx);
        rect.setAttribute("fill", "var(--accent, #2f5eaa)");
        rect.setAttribute("stroke", isSelected ? "#1c1c1a" : "#1c3d73");
        rect.setAttribute("stroke-width", isSelected ? "3" : "1.5");

        const text = document.createElementNS(ns, "text");
        text.setAttribute("x", pos.x);
        text.setAttribute("y", pos.y);
        text.setAttribute("fill", "#ffffff");
        text.setAttribute("font-size", "14");
        text.setAttribute("font-weight", "bold");
        text.setAttribute("text-anchor", "middle");
        text.setAttribute("dominant-baseline", "central");
        text.textContent = tableName;

        g.appendChild(rect);
        g.appendChild(text);
      } else {
        // 소스 노드는 원형으로
        const circle = document.createElementNS(ns, "circle");
        circle.setAttribute("cx", pos.x);
        circle.setAttribute("cy", pos.y);
        circle.setAttribute("r", pos.r);

        const role = pos.source.role;
        if (role === "aggregated" || pos.source.cardinality === "one_to_many") {
          circle.setAttribute("fill", "#ffffff");
          circle.setAttribute("stroke", "var(--warn, #8a6516)");
          circle.setAttribute("stroke-width", "2");
          circle.setAttribute("stroke-dasharray", "4 3");
        } else {
          // inlined
          circle.setAttribute("fill", "var(--accent-bg, #eef3fb)");
          circle.setAttribute("stroke", "var(--accent, #2f5eaa)");
          circle.setAttribute("stroke-width", "2");
        }

        // 원 내부 수치 (필드 수)
        const innerText = document.createElementNS(ns, "text");
        innerText.setAttribute("x", pos.x);
        innerText.setAttribute("y", pos.y);
        innerText.setAttribute("fill", role === "aggregated" ? "var(--warn, #8a6516)" : "var(--accent, #2f5eaa)");
        innerText.setAttribute("font-size", "11");
        innerText.setAttribute("font-weight", "bold");
        innerText.setAttribute("text-anchor", "middle");
        innerText.setAttribute("dominant-baseline", "central");
        innerText.textContent = `${fc}`;
        g.appendChild(circle);
        g.appendChild(innerText);

        // 라벨 텍스트 (원 외부에 각도에 알맞게 배치하여 텍스트 겹침 방지)
        const offset = pos.r + 8;
        const tx = pos.x + offset * Math.cos(pos.angle);
        const ty = pos.y + offset * Math.sin(pos.angle);

        const labelText = document.createElementNS(ns, "text");
        labelText.setAttribute("x", tx);
        labelText.setAttribute("y", ty);
        labelText.setAttribute("font-size", "12");
        labelText.setAttribute("font-weight", "600");
        labelText.setAttribute("fill", "var(--ink, #1c1c1a)");

        // 각도별 text-anchor 및 dominant-baseline 자동 조정
        const cos = Math.cos(pos.angle);
        const sin = Math.sin(pos.angle);

        let anchorAttr = "middle";
        if (cos > 0.25) anchorAttr = "start";
        else if (cos < -0.25) anchorAttr = "end";

        let baselineAttr = "central";
        if (sin > 0.6) baselineAttr = "hanging";
        else if (sin < -0.6) baselineAttr = "baseline";

        labelText.setAttribute("text-anchor", anchorAttr);
        labelText.setAttribute("dominant-baseline", baselineAttr);
        labelText.textContent = tableName;

        g.appendChild(labelText);
      }

      // 클릭 시 해당 테이블 필드 상세 보기
      g.addEventListener("click", () => {
        currentSelectedTable = tableName;
        drawRadialSVG(chartContainer, model, detailContainer);
      });

      svg.appendChild(g);
    });

    chartContainer.innerHTML = "";
    chartContainer.appendChild(svg);

    // 1-4. 선택된 테이블 필드 상세 목록 rendering
    renderLineageDetail(detailContainer, model, currentSelectedTable);
  }

  /**
   * 노드 클릭 시 그 테이블이 보탠 필드 목록을 차트 아래에 펼친다.
   */
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
   * data.fidelity 객체를 받아 게이지 바 3개와 미답변 조합, 테이블별 보존율 표를 그린다.
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
          <span class="gauge-desc">${num(counts.total_columns)}개 중 ${num(counts.exposed_columns)}개를 꺼내 쓸 수 있음</span>
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

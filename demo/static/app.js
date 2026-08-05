/**
 * tablefold — Schema Engineering Dashboard Logic (Full Feature Set)
 */

document.addEventListener('DOMContentLoaded', () => {
  let state = {
    ddl: '',
    source: 'ddl',
    anchorMode: 'auto',
    coverage: 0.90,
    minGain: 2,
    maxCost: 10.0,
    fieldBudget: 200,
    maxAreas: 6,
    activeTab: 'tab-1',
    foldResult: null
  };

  // DOM Elements
  const elSourceSelect = document.getElementById('sourceSelect');
  const elAnchorSelect = document.getElementById('anchorSelect');
  const elOpenDdlBtn = document.getElementById('openDdlBtn');
  const elOpenConfigBtn = document.getElementById('openConfigBtn');
  const elRunFoldBtn = document.getElementById('runFoldBtn');

  // Modals & Sheets
  const elDdlModal = document.getElementById('ddlModal');
  const elConfigModal = document.getElementById('configModal');
  const elSheet = document.getElementById('sheet');
  const elSheetName = document.getElementById('sheetName');
  const elSheetDesc = document.getElementById('sheetDesc');
  const elSheetBody = document.getElementById('sheetBody');
  const elSheetClose = document.getElementById('sheetClose');

  const elDdlTextarea = document.getElementById('ddlTextarea');
  const elApplyDdlBtn = document.getElementById('applyDdlBtn');
  const elResetSampleDdlBtn = document.getElementById('resetSampleDdlBtn');
  const elApplyConfigBtn = document.getElementById('applyConfigBtn');

  // Headline & Metrics
  const elBeforeJoins = document.getElementById('beforeJoins');
  const elSizeDelta = document.getElementById('sizeDelta');
  const elFactModels = document.getElementById('factModels');
  const elFactCovered = document.getElementById('factCovered');
  const elFactLinks = document.getElementById('factLinks');
  const elFactEdge = document.getElementById('factEdge');
  const elMDeclaredFks = document.getElementById('mDeclaredFks');
  const elMPromptSize = document.getElementById('mPromptSize');
  const elMSizeReduction = document.getElementById('mSizeReduction');

  // Tabs Nav
  const elPipelineNav = document.getElementById('pipelineNav');
  const tabPanes = document.querySelectorAll('.tab-pane');

  // Tab Elements
  const elTblInferredFks = document.getElementById('tblInferredFks');
  const elTblPhysicalCatalog = document.getElementById('tblPhysicalCatalog');
  const elInferredFkCount = document.getElementById('inferredFkCount');

  const elTblFactness = document.getElementById('tblFactness');
  const elTblLattice = document.getElementById('tblLattice');

  const elModelList = document.getElementById('modelList');
  const elLeftoverBox = document.getElementById('leftoverBox');
  const elLeftoverCount = document.getElementById('leftoverCount');
  const elLeftoverChips = document.getElementById('leftoverChips');
  const elModelCount = document.getElementById('modelCount');

  const elPromptCodeView = document.getElementById('promptCodeView');
  const elCopyPromptBtn = document.getElementById('copyPromptBtn');

  const elLogicalSqlInput = document.getElementById('logicalSqlInput');
  const elBtnExpandSql = document.getElementById('btnExpandSql');
  const elPhysicalSqlOutput = document.getElementById('physicalSqlOutput');
  const elExpandStatsBadge = document.getElementById('expandStatsBadge');
  const elCopySqlBtn = document.getElementById('copySqlBtn');

  // Toast
  const elToast = document.getElementById('toastNotification');

  function showToast(msg) {
    elToast.textContent = msg;
    elToast.hidden = false;
    setTimeout(() => { elToast.hidden = true; }, 4500);
  }

  // Source & Anchor Selectors
  elSourceSelect.addEventListener('change', (e) => {
    state.source = e.target.value;
    runFold();
  });

  elAnchorSelect.addEventListener('change', (e) => {
    state.anchorMode = e.target.value;
    runFold();
  });

  async function checkSources() {
    try {
      const res = await fetch('/api/sources');
      if (res.ok) {
        const data = await res.json();
        const liveOpt = elSourceSelect.querySelector('option[value="live"]');
        if (data.live_available) {
          liveOpt.textContent = `Live Database (${data.live_label || 'Connected'})`;
        } else {
          liveOpt.textContent = `Live Database Connection`;
        }
      }
    } catch (err) {
      console.error(err);
    }
  }

  // Modals Event Handling
  elOpenDdlBtn.addEventListener('click', () => {
    elDdlTextarea.value = state.ddl;
    elDdlModal.hidden = false;
  });

  elOpenConfigBtn.addEventListener('click', () => {
    document.getElementById('cfgMaxAreas').value = state.maxAreas;
    document.getElementById('cfgFieldBudget').value = state.fieldBudget;
    document.getElementById('cfgMinGain').value = state.minGain;
    document.getElementById('cfgMaxCost').value = state.maxCost;
    document.getElementById('cfgCoverage').value = state.coverage;
    elConfigModal.hidden = false;
  });

  document.querySelectorAll('.close-modal-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const modalId = e.target.getAttribute('data-modal');
      if (modalId) document.getElementById(modalId).hidden = true;
    });
  });

  [elDdlModal, elConfigModal].forEach(modal => {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) modal.hidden = true;
    });
  });

  elSheetClose.addEventListener('click', () => {
    elSheet.hidden = true;
  });

  elApplyDdlBtn.addEventListener('click', () => {
    state.ddl = elDdlTextarea.value;
    elDdlModal.hidden = true;
    runFold();
  });

  elResetSampleDdlBtn.addEventListener('click', async () => {
    await fetchSampleDdl();
    elDdlTextarea.value = state.ddl;
  });

  elApplyConfigBtn.addEventListener('click', () => {
    state.maxAreas = parseInt(document.getElementById('cfgMaxAreas').value, 10);
    state.fieldBudget = parseInt(document.getElementById('cfgFieldBudget').value, 10);
    state.minGain = parseInt(document.getElementById('cfgMinGain').value, 10);
    state.maxCost = parseFloat(document.getElementById('cfgMaxCost').value);
    state.coverage = parseFloat(document.getElementById('cfgCoverage').value);
    elConfigModal.hidden = true;
    runFold();
  });

  // Tab Navigation Handling
  elPipelineNav.addEventListener('click', (e) => {
    const btn = e.target.closest('.tab-btn');
    if (!btn) return;

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    const targetTab = btn.getAttribute('data-tab');
    state.activeTab = targetTab;

    tabPanes.forEach(pane => {
      if (pane.id === targetTab) pane.classList.add('active');
      else pane.classList.remove('active');
    });

    if (targetTab === 'tab-3' && state.foldResult) {
      if (typeof window.renderLineage === 'function') {
        window.renderLineage(state.foldResult.lineage);
      }
      if (typeof window.renderFidelity === 'function') {
        window.renderFidelity(state.foldResult.fidelity);
      }
    }
  });

  // SQL Presets
  document.querySelectorAll('.chip-btn[data-sql]').forEach(chip => {
    chip.addEventListener('click', () => {
      elLogicalSqlInput.value = chip.getAttribute('data-sql');
      runExpand();
    });
  });

  elCopyPromptBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(elPromptCodeView.textContent);
    showToast('Prompt copied to clipboard!');
  });

  elCopySqlBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(elPhysicalSqlOutput.textContent);
    showToast('SQL copied to clipboard!');
  });

  elRunFoldBtn.addEventListener('click', () => runFold());
  elBtnExpandSql.addEventListener('click', () => runExpand());

  // API Calls
  async function fetchSampleDdl() {
    try {
      const res = await fetch('/api/sample');
      if (!res.ok) throw new Error('Failed to fetch sample DDL');
      const data = await res.json();
      state.ddl = data.ddl;
    } catch (err) {
      console.error(err);
      showToast('Error loading sample DDL');
    }
  }

  async function runFold() {
    if (state.source === 'ddl' && !state.ddl.trim()) await fetchSampleDdl();

    try {
      const res = await fetch('/api/fold', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ddl: state.ddl,
          source: state.source,
          anchor_mode: state.anchorMode,
          coverage: state.coverage,
          min_gain: state.minGain,
          max_cost: state.maxCost,
          field_budget: state.fieldBudget,
          max_areas: state.maxAreas
        })
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Fold failed');
      }

      const data = await res.json();
      state.foldResult = data;
      renderAllTabs(data);
      showToast(`Fold pipeline executed successfully (${state.source.toUpperCase()}).`);
    } catch (err) {
      console.error(err);
      showToast(err.message);
    }
  }

  async function runExpand() {
    const sql = elLogicalSqlInput.value.trim();
    if (!sql) return;

    try {
      const res = await fetch('/api/expand', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ddl: state.ddl,
          sql: sql,
          source: state.source,
          anchor_mode: state.anchorMode,
          coverage: state.coverage,
          min_gain: state.minGain,
          max_cost: state.maxCost,
          field_budget: state.fieldBudget,
          max_areas: state.maxAreas
        })
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Expand failed');
      }

      const data = await res.json();
      elPhysicalSqlOutput.textContent = data.expanded_sql;

      elExpandStatsBadge.hidden = false;
      elExpandStatsBadge.innerHTML = `
        <strong>Expansion Stats:</strong> ${data.joins_emitted} Joins Emitted | 
        ${data.joins_pruned.length} Joins Pruned (${data.joins_pruned.join(', ') || 'None'}) | 
        ${data.fields_used.length} Fields Referenced
      `;
    } catch (err) {
      console.error(err);
      elPhysicalSqlOutput.textContent = `Error: ${err.message}`;
      elExpandStatsBadge.hidden = true;
    }
  }

  // Open Drawer Sheet for Model Details
  function openModelSheet(model) {
    elSheetName.textContent = `Wide Model: ${model.name}`;
    elSheetDesc.textContent = `Base Anchor: ${model.base_table} | Total Fields: ${model.field_count}`;

    const fieldsHtml = model.fields.map(f => `
      <div class="sheet-field-row">
        <span class="sheet-field-name"><strong>${f.name}</strong></span>
        <span class="sheet-field-type"><code>${f.type}</code></span>
        <span class="sheet-field-origin">${f.source_table}.${f.source_column} (${f.cardinality})</span>
      </div>
    `).join('');

    elSheetBody.innerHTML = `
      <div class="sheet-section">
        <h4>Absorbed Tables (${model.absorbed_tables.length})</h4>
        <p>${model.absorbed_tables.join(', ') || 'None (Single table model)'}</p>
      </div>
      <div class="sheet-section" style="margin-top:16px;">
        <h4>Model Fields Catalog (${model.fields.length})</h4>
        <div class="table-scroll" style="max-height:300px;">
          ${fieldsHtml}
        </div>
      </div>
    `;

    elSheet.hidden = false;
  }

  // Render Functions
  function renderAllTabs(data) {
    // 1. Headline & Summary Metrics
    const declaredFkCount = data.physical.declared_fk_count || 0;
    const inferredFkCount = data.physical.inferred_fk_count || 0;
    elBeforeJoins.textContent = declaredFkCount + inferredFkCount;

    const ddlChars = data.size.ddl_chars || 1;
    const promptChars = data.size.core_prompt_chars || 1;
    const ratio = Math.round((1 - (promptChars / ddlChars)) * 100);
    elSizeDelta.textContent = `${ratio > 0 ? ratio : 0}% Reduction`;

    elFactModels.textContent = data.tier_summary.tier1_core_models_count;
    elFactCovered.textContent = `${data.tier_summary.tier1_covered_physical_tables_count} / ${data.physical.table_count} tables covered`;

    elFactLinks.textContent = inferredFkCount;
    elMDeclaredFks.textContent = `${declaredFkCount} declared in DDL`;

    elFactEdge.textContent = data.tier_summary.tier2_edge_tables_count;
    elMPromptSize.textContent = `${promptChars.toLocaleString()} chars`;
    elMSizeReduction.textContent = `${ratio > 0 ? ratio : 0}% reduction vs physical DDL`;

    // STEP 1: Introspection & Recovery
    elInferredFkCount.textContent = inferredFkCount;
    if (data.physical.inferred_fks.length === 0) {
      elTblInferredFks.innerHTML = `<tr><td colspan="5" class="empty-cell">No inferred foreign keys (All declared in DDL).</td></tr>`;
    } else {
      elTblInferredFks.innerHTML = data.physical.inferred_fks.map(fk => `
        <tr>
          <td><strong>${fk.from_table}</strong></td>
          <td><code>${fk.from_columns.join(', ')}</code></td>
          <td><strong>${fk.to_table}</strong></td>
          <td><code>${fk.to_columns.join(', ')}</code></td>
          <td><span class="badge-tag fact">${Math.round(fk.confidence * 100)}%</span></td>
        </tr>
      `).join('');
    }

    elTblPhysicalCatalog.innerHTML = data.physical.tables.map(t => `
      <tr>
        <td><strong>${t.name}</strong></td>
        <td><code>${t.primary_key.join(', ') || '-'}</code></td>
        <td>${t.column_count}</td>
        <td><span class="badge-tag ${t.role}">${t.role.toUpperCase()}</span></td>
        <td>${t.row_estimate ? t.row_estimate.toLocaleString() : '-'}</td>
        <td><span class="badge-tag ${t.tier.includes('Core') ? 'fact' : 'dim'}">${t.tier}</span></td>
      </tr>
    `).join('');

    // STEP 2: Factness & Lattice
    elTblFactness.innerHTML = data.physical.tables.map(t => `
      <tr>
        <td><strong>${t.name}</strong></td>
        <td><span class="badge-tag ${t.role}">${t.role.toUpperCase()}</span></td>
        <td><strong>${t.fact_score}</strong></td>
        <td>${t.columns.filter(c => c.is_numeric).length} cols</td>
        <td>${t.columns.some(c => c.is_temporal) ? 'Yes' : 'No'}</td>
        <td>${t.tier.includes('Core') ? '✓ Anchor/Absorbed' : '-'}</td>
      </tr>
    `).join('');

    elTblLattice.innerHTML = data.analytics.candidates.map(c => `
      <tr>
        <td><strong>${c.name}</strong></td>
        <td><span class="badge-tag ${c.role}">${c.role.toUpperCase()}</span></td>
        <td>${c.score}</td>
        <td>${c.reach_count} tables (${c.reach_tables.join(', ')})</td>
        <td>${c.estimated_fields} fields</td>
      </tr>
    `).join('');

    // STEP 3: Wide Models Grid & Leftover Edge Tables
    const models = data.logical.models || [];
    elModelCount.textContent = models.length;

    elModelList.innerHTML = models.map((m, idx) => `
      <div class="model-card-item" data-idx="${idx}">
        <div class="model-card-head">
          <span class="model-card-title">${m.name}</span>
          <span class="badge-tag fact">${m.field_count} fields</span>
        </div>
        <div class="model-card-sub">Base Anchor: <strong>${m.base_table}</strong></div>
        <div class="model-card-body">
          <p><strong>Absorbed (${m.absorbed_tables.length}):</strong> ${m.absorbed_tables.join(', ') || 'None'}</p>
        </div>
      </div>
    `).join('');

    document.querySelectorAll('.model-card-item').forEach(card => {
      card.addEventListener('click', () => {
        const idx = parseInt(card.getAttribute('data-idx'), 10);
        openModelSheet(models[idx]);
      });
    });

    const tier2Tables = data.tier2_edge_tables || [];
    elLeftoverCount.textContent = tier2Tables.length;
    if (tier2Tables.length > 0) {
      elLeftoverBox.style.display = 'block';
      elLeftoverChips.innerHTML = tier2Tables.map(t => `<span class="chip-item">${t.name}</span>`).join('');
    } else {
      elLeftoverBox.style.display = 'none';
    }

    // Lineage Diagram & Fidelity Report
    if (typeof window.renderLineage === 'function') {
      window.renderLineage(data.lineage);
    }
    if (typeof window.renderFidelity === 'function') {
      window.renderFidelity(data.fidelity);
    }

    // STEP 4: Prompt Code View
    elPromptCodeView.textContent = data.core_prompt_text || data.prompt_text;

    // Trigger initial expand
    runExpand();
  }

  // Initialize
  checkSources();
  runFold();
});

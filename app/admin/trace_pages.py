from fastapi.responses import HTMLResponse


def build_ai_traces_html() -> HTMLResponse:
    html = """<!doctype html>
<html lang="th">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Ener-AI Trace Viewer</title>
  <style>
    :root {
      --bg: #0b0c0f;
      --card: #121317;
      --card2: #171922;
      --border: #262a36;
      --text: #e8eaf0;
      --muted: #8a90a2;
      --green: #22c55e;
      --red: #ef4444;
      --amber: #f59e0b;
      --blue: #60a5fa;
      --purple: #a78bfa;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .wrap {
      max-width: 1420px;
      margin: 0 auto;
      padding: 18px 16px 28px;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .title {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .title h1 {
      margin: 0;
      font-size: 1.15rem;
      font-weight: 700;
    }
    .badge {
      border: 1px solid var(--border);
      background: #0e1016;
      color: var(--muted);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 0.78rem;
    }
    .actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .btn, select, input {
      border: 1px solid var(--border);
      background: #0f1118;
      color: var(--text);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 0.88rem;
    }
    .btn {
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .btn:hover { border-color: #3b4254; }
    .cards {
      display: grid;
      grid-template-columns: repeat(6, minmax(130px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      min-height: 74px;
    }
    .card .k {
      color: var(--muted);
      font-size: 0.78rem;
      margin-bottom: 6px;
    }
    .card .v {
      font-size: 1.3rem;
      font-weight: 700;
    }
    .filters {
      display: grid;
      grid-template-columns: repeat(6, minmax(110px, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .filters .search {
      grid-column: span 2;
    }
    .list-card {
      background: var(--card2);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }
    .table-wrap { overflow: auto; }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1140px;
    }
    th, td {
      border-bottom: 1px solid #202432;
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
      font-size: 0.85rem;
    }
    th { color: #c8ccdb; font-weight: 600; background: #11131b; }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 0.78rem;
      word-break: break-all;
    }
    .pill {
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.74rem;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      margin-right: 4px;
      margin-bottom: 4px;
      background: #0f1118;
    }
    .ok { color: var(--green); border-color: #22583a; }
    .err { color: var(--red); border-color: #6a2b2b; }
    .warn { color: var(--amber); border-color: #6c4b0d; }
    .exec { color: var(--purple); border-color: #4b3a83; }
    .preview { color: #d7d9e1; max-width: 360px; }
    details.trace-detail {
      border-top: 1px solid #222636;
      background: #0f1118;
      margin: 0;
      padding: 0;
    }
    details.trace-detail > summary {
      list-style: none;
      cursor: pointer;
      padding: 8px 12px;
      color: #cdd1dd;
      border-bottom: 1px solid #1f2331;
    }
    details.trace-detail > summary::-webkit-details-marker { display: none; }
    .detail-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      padding: 12px;
    }
    .panel {
      border: 1px solid #262b3a;
      border-radius: 8px;
      background: #10131b;
      padding: 10px;
    }
    .panel h4 {
      margin: 0 0 8px 0;
      font-size: 0.86rem;
      color: #d8dbee;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 220px;
      overflow: auto;
      background: #0a0c12;
      border: 1px solid #252938;
      border-radius: 6px;
      padding: 8px;
      color: #cfd3e1;
      font-size: 0.77rem;
    }
    .timeline-item {
      border: 1px solid #2a2f3e;
      border-left: 4px solid #2f425f;
      border-radius: 8px;
      background: #0f131d;
      padding: 8px;
      margin-bottom: 8px;
    }
    .timeline-item.ok { border-left-color: var(--green); }
    .timeline-item.err { border-left-color: var(--red); }
    .timeline-item.warn { border-left-color: var(--amber); }
    .muted { color: var(--muted); font-size: 0.8rem; }
    .empty {
      color: var(--muted);
      text-align: center;
      padding: 18px;
    }
    .event-card {
      margin-top: 12px;
      background: var(--card2);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
    }
    .event-item {
      border: 1px solid #2a2f3e;
      border-radius: 8px;
      background: #10141f;
      padding: 8px;
      margin-bottom: 8px;
    }
    .event-item:last-child { margin-bottom: 0; }
    @media (max-width: 1200px) {
      .cards { grid-template-columns: repeat(3, minmax(130px, 1fr)); }
      .filters { grid-template-columns: repeat(3, minmax(110px, 1fr)); }
      .filters .search { grid-column: span 3; }
      .detail-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div class="title">
        <a class="btn" href="/admin">← Admin</a>
        <h1>⚡ AI Trace Viewer</h1>
        <span class="badge">Core V2 Observability</span>
      </div>
      <div class="actions">
        <button class="btn" id="refreshBtn" type="button">↻ Refresh</button>
        <label class="muted" for="autoRefresh">Auto</label>
        <select id="autoRefresh">
          <option value="0">Off</option>
          <option value="15000">15s</option>
          <option value="30000" selected>30s</option>
          <option value="60000">1m</option>
        </select>
      </div>
    </div>

    <div class="cards" id="summaryCards">
      <div class="card"><div class="k">Total Traces</div><div class="v" id="sumTotal">0</div></div>
      <div class="card"><div class="k">Tool Runs</div><div class="v" id="sumTools">0</div></div>
      <div class="card"><div class="k">Code Runs</div><div class="v" id="sumCode">0</div></div>
      <div class="card"><div class="k">Failed Tools</div><div class="v" id="sumToolFail">0</div></div>
      <div class="card"><div class="k">Failed Code Runs</div><div class="v" id="sumCodeFail">0</div></div>
      <div class="card"><div class="k">Execution-only</div><div class="v" id="sumExecOnly">0</div></div>
    </div>

    <div class="filters">
      <select id="limitFilter">
        <option value="20">Limit 20</option>
        <option value="50" selected>Limit 50</option>
        <option value="100">Limit 100</option>
      </select>
      <select id="sourceFilter"><option value="">Source: All</option></select>
      <select id="modelFilter"><option value="">Model: All</option></select>
      <select id="intentFilter"><option value="">Intent: All</option></select>
      <select id="statusFilter">
        <option value="">Status: All</option>
        <option value="success">Success</option>
        <option value="error">Has Error</option>
        <option value="execution-only">Execution-only</option>
        <option value="has-tools">Has Tools</option>
        <option value="has-code">Has Code</option>
      </select>
      <input id="searchFilter" class="search" type="text" placeholder="Search trace_id, conversation_id, preview, tool, code, model, intent" />
    </div>

    <div class="list-card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Trace ID</th>
              <th>Source</th>
              <th>Intent</th>
              <th>Model</th>
              <th>User Preview</th>
              <th>Tools</th>
              <th>Code</th>
              <th>Status</th>
              <th>View</th>
            </tr>
          </thead>
          <tbody id="traceBody"></tbody>
        </table>
      </div>
      <div id="emptyState" class="empty" style="display:none">No trace data</div>
    </div>

    <div class="event-card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
        <h3 style="margin:0;font-size:0.95rem;">Recent External Events (Ener Scan)</h3>
        <span class="muted">/admin/api/events/recent?source=ener_scan</span>
      </div>
      <div id="eventsBody"></div>
      <div id="eventsEmpty" class="empty" style="display:none">No external events</div>
    </div>

    <div class="event-card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
        <h3 style="margin:0;font-size:0.95rem;">Recent Artifacts</h3>
        <span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <a class="btn" href="/admin/ener-scan-business" style="font-size:0.82rem;">Open Ener Scan Business Dashboard</a>
          <span class="muted">/admin/api/artifacts/recent?project_slug=ener-scan</span>
        </span>
      </div>
      <div id="artifactCoverage" class="muted" style="margin-bottom:8px;font-size:0.85rem;">Coverage: loading…</div>
      <div style="margin-bottom:8px;">
        <button type="button" id="artifactBackfillBtn" class="btn" style="font-size:0.85rem;">Backfill Artifacts</button>
        <span id="artifactBackfillResult" class="muted" style="margin-left:8px;font-size:0.85rem;"></span>
      </div>
      <div id="artifactsBody"></div>
      <div id="artifactsEmpty" class="empty" style="display:none">No artifacts</div>
    </div>
  </div>

  <script>
    let allTraces = [];
    let visibleTraces = [];
    let recentEvents = [];
    let recentArtifacts = [];
    let refreshTimer = null;

    const refs = {
      limit: document.getElementById('limitFilter'),
      source: document.getElementById('sourceFilter'),
      model: document.getElementById('modelFilter'),
      intent: document.getElementById('intentFilter'),
      status: document.getElementById('statusFilter'),
      search: document.getElementById('searchFilter'),
      body: document.getElementById('traceBody'),
      empty: document.getElementById('emptyState'),
      auto: document.getElementById('autoRefresh'),
      refreshBtn: document.getElementById('refreshBtn'),
      sumTotal: document.getElementById('sumTotal'),
      sumTools: document.getElementById('sumTools'),
      sumCode: document.getElementById('sumCode'),
      sumToolFail: document.getElementById('sumToolFail'),
      sumCodeFail: document.getElementById('sumCodeFail'),
      sumExecOnly: document.getElementById('sumExecOnly'),
      eventsBody: document.getElementById('eventsBody'),
      eventsEmpty: document.getElementById('eventsEmpty'),
      artifactsBody: document.getElementById('artifactsBody'),
      artifactsEmpty: document.getElementById('artifactsEmpty'),
      artifactCoverage: document.getElementById('artifactCoverage'),
      artifactBackfillBtn: document.getElementById('artifactBackfillBtn'),
      artifactBackfillResult: document.getElementById('artifactBackfillResult'),
    };

    function safeText(v) {
      if (v === null || v === undefined) return '';
      return String(v);
    }

    function escapeHtml(text) {
      return safeText(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function short(v, n = 140) {
      const s = safeText(v).trim();
      if (s.length <= n) return s;
      return s.slice(0, n - 3).trimEnd() + '...';
    }

    function formatJson(v) {
      if (v === null || v === undefined || v === '') return '{}';
      if (typeof v === 'string') {
        try { return JSON.stringify(JSON.parse(v), null, 2); }
        catch (_) { return short(v, 1500); }
      }
      try { return JSON.stringify(v, null, 2); }
      catch (_) { return short(String(v), 1500); }
    }

    function parseMaybeArray(v) {
      if (Array.isArray(v)) return v;
      return [];
    }

    function isFailedTool(tool) {
      const s = Number(tool?.success ?? 1);
      return s !== 1;
    }

    function isFailedCode(code) {
      const st = safeText(code?.status).toLowerCase();
      return st.includes('fail') || st.includes('error');
    }

    function isExecutionOnly(trace) {
      const hasMsg = safeText(trace.user_preview) || safeText(trace.assistant_preview);
      const hasExec = parseMaybeArray(trace.tool_runs).length > 0 || parseMaybeArray(trace.code_runs).length > 0;
      return !hasMsg && hasExec;
    }

    function getTraceStatus(trace) {
      const tools = parseMaybeArray(trace.tool_runs);
      const codes = parseMaybeArray(trace.code_runs);
      const toolFail = tools.some(isFailedTool);
      const codeFail = codes.some(isFailedCode);
      if (toolFail || codeFail) return 'ERROR';
      if (isExecutionOnly(trace)) return 'EXEC ONLY';
      return 'OK';
    }

    function statusClass(status) {
      if (status === 'ERROR') return 'err';
      if (status === 'EXEC ONLY') return 'exec';
      return 'ok';
    }

    function computeSummary(traces) {
      const total = traces.length;
      let toolRuns = 0, codeRuns = 0, toolFail = 0, codeFail = 0, execOnly = 0;
      for (const t of traces) {
        const tools = parseMaybeArray(t.tool_runs);
        const codes = parseMaybeArray(t.code_runs);
        toolRuns += tools.length;
        codeRuns += codes.length;
        toolFail += tools.filter(isFailedTool).length;
        codeFail += codes.filter(isFailedCode).length;
        if (isExecutionOnly(t)) execOnly += 1;
      }
      return { total, toolRuns, codeRuns, toolFail, codeFail, execOnly };
    }

    function renderSummary() {
      const s = computeSummary(visibleTraces);
      refs.sumTotal.textContent = String(s.total);
      refs.sumTools.textContent = String(s.toolRuns);
      refs.sumCode.textContent = String(s.codeRuns);
      refs.sumToolFail.textContent = String(s.toolFail);
      refs.sumCodeFail.textContent = String(s.codeFail);
      refs.sumExecOnly.textContent = String(s.execOnly);
    }

    function fillSelect(selectEl, values, label) {
      const prev = selectEl.value;
      selectEl.innerHTML = `<option value="">${label}: All</option>`;
      for (const v of values) {
        if (!v) continue;
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        selectEl.appendChild(opt);
      }
      if ([...selectEl.options].some(o => o.value === prev)) selectEl.value = prev;
    }

    function renderFilterOptions() {
      const sources = [...new Set(allTraces.map(t => safeText(t.source)).filter(Boolean))].sort();
      const models = [...new Set(allTraces.map(t => safeText(t.model_used)).filter(Boolean))].sort();
      const intents = [...new Set(allTraces.map(t => safeText(t.intent)).filter(Boolean))].sort();
      fillSelect(refs.source, sources, 'Source');
      fillSelect(refs.model, models, 'Model');
      fillSelect(refs.intent, intents, 'Intent');
    }

    function matchSearch(trace, keyword) {
      if (!keyword) return true;
      const tools = parseMaybeArray(trace.tool_runs);
      const codes = parseMaybeArray(trace.code_runs);
      const bag = [
        trace.trace_id, trace.conversation_id, trace.user_preview, trace.assistant_preview,
        trace.model_used, trace.intent, trace.source, trace.chat_id,
        ...tools.map(t => `${safeText(t.tool_name)} ${safeText(t.error)} ${safeText(t.output_preview)}`),
        ...codes.map(c => `${safeText(c.action)} ${safeText(c.status)} ${safeText(c.error)} ${safeText(c.request_id)}`)
      ].join(' ').toLowerCase();
      return bag.includes(keyword);
    }

    function applyFilters() {
      const source = refs.source.value;
      const model = refs.model.value;
      const intent = refs.intent.value;
      const status = refs.status.value;
      const q = safeText(refs.search.value).trim().toLowerCase();

      visibleTraces = allTraces.filter(t => {
        if (source && safeText(t.source) !== source) return false;
        if (model && safeText(t.model_used) !== model) return false;
        if (intent && safeText(t.intent) !== intent) return false;

        const st = getTraceStatus(t);
        if (status === 'success' && st !== 'OK') return false;
        if (status === 'error' && st !== 'ERROR') return false;
        if (status === 'execution-only' && st !== 'EXEC ONLY') return false;
        if (status === 'has-tools' && parseMaybeArray(t.tool_runs).length === 0) return false;
        if (status === 'has-code' && parseMaybeArray(t.code_runs).length === 0) return false;
        if (!matchSearch(t, q)) return false;
        return true;
      });

      renderSummary();
      renderTraces();
    }

    function renderToolRuns(toolRuns) {
      const runs = parseMaybeArray(toolRuns);
      if (!runs.length) return '<div class="muted">No tool runs</div>';
      return runs.map(run => {
        const failed = isFailedTool(run);
        return `
          <div class="timeline-item ${failed ? 'err' : 'ok'}">
            <div><span class="pill mono">${escapeHtml(run.tool_name)}</span>
              <span class="pill ${failed ? 'err' : 'ok'}">${failed ? 'FAILED' : 'OK'}</span>
              <span class="pill">${escapeHtml(run.duration_ms)} ms</span>
              <span class="pill">${escapeHtml(run.created_at)}</span>
            </div>
            <div class="muted">input</div>
            <pre class="mono">${escapeHtml(formatJson(run.input_preview || run.tool_input_json || ''))}</pre>
            <div class="muted">output</div>
            <pre class="mono">${escapeHtml(safeText(run.output_preview || ''))}</pre>
            ${failed ? `<div class="muted">error</div><pre class="mono">${escapeHtml(safeText(run.error || ''))}</pre>` : ''}
          </div>`;
      }).join('');
    }

    function renderCodeRuns(codeRuns) {
      const runs = parseMaybeArray(codeRuns);
      if (!runs.length) return '<div class="muted">No code runs</div>';
      return runs.map(run => {
        const st = safeText(run.status).toLowerCase();
        const cls = st.includes('fail') || st.includes('error') ? 'err' : (st.includes('pending') || st.includes('apply') ? 'warn' : 'ok');
        return `
          <div class="timeline-item ${cls}">
            <div>
              <span class="pill mono">${escapeHtml(run.action)}</span>
              <span class="pill ${cls}">${escapeHtml(run.status)}</span>
              <span class="pill mono">${escapeHtml(run.request_id || '-')}</span>
            </div>
            <div class="muted">${escapeHtml(run.created_at || '')} → ${escapeHtml(run.updated_at || '')}</div>
            <pre class="mono">${escapeHtml(formatJson(run.files || run.files_json || ''))}</pre>
            <pre class="mono">${escapeHtml(formatJson(run.tests || run.tests_json || ''))}</pre>
            <pre class="mono">${escapeHtml(formatJson(run.deploy || run.deploy_json || ''))}</pre>
            ${run.error ? `<div class="muted">error</div><pre class="mono">${escapeHtml(safeText(run.error))}</pre>` : ''}
          </div>`;
      }).join('');
    }

    function renderTraces() {
      refs.body.innerHTML = '';
      refs.empty.style.display = visibleTraces.length ? 'none' : 'block';
      if (!visibleTraces.length) return;

      for (const t of visibleTraces) {
        const status = getTraceStatus(t);
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${escapeHtml(short(t.created_at, 19) || '-')}</td>
          <td class="mono">${escapeHtml(short(t.trace_id, 22))}</td>
          <td><span class="pill">${escapeHtml(t.source || '-')}</span></td>
          <td><span class="pill">${escapeHtml(t.intent || '-')}</span></td>
          <td><span class="pill">${escapeHtml(t.model_used || '-')}</span></td>
          <td class="preview">${escapeHtml(short(t.user_preview || '-', 160))}</td>
          <td>${parseMaybeArray(t.tool_runs).length}</td>
          <td>${parseMaybeArray(t.code_runs).length}</td>
          <td><span class="pill ${statusClass(status)}">${escapeHtml(status)}</span></td>
          <td><button class="btn mono" type="button" data-open="${escapeHtml(t.trace_id || '')}">View</button></td>
        `;
        refs.body.appendChild(tr);

        const detailTr = document.createElement('tr');
        detailTr.style.display = 'none';
        detailTr.dataset.detail = String(t.trace_id || '');
        detailTr.innerHTML = `
          <td colspan="10" style="padding:0;">
            <details class="trace-detail" open>
              <summary>Trace detail: <span class="mono">${escapeHtml(t.trace_id || '')}</span></summary>
              <div class="detail-grid">
                <div class="panel">
                  <h4>Trace Metadata</h4>
                  <pre>${escapeHtml(formatJson({
                    trace_id: t.trace_id || '',
                    conversation_id: t.conversation_id || '',
                    source: t.source || '',
                    chat_id: t.chat_id || '',
                    created_at: t.created_at || '',
                    intent: t.intent || '',
                    model_used: t.model_used || '',
                    context_snapshot: t.context_snapshot || ''
                  }))}</pre>
                  ${isExecutionOnly(t) ? `<span class="pill exec">Execution-only trace</span>` : ''}
                </div>
                <div class="panel">
                  <h4>User / Assistant</h4>
                  <pre>${escapeHtml("User: " + safeText(t.user_preview || "") + "\\n\\nAssistant: " + safeText(t.assistant_preview || ""))}</pre>
                </div>
                <div class="panel">
                  <h4>Route JSON</h4>
                  <pre>${escapeHtml(formatJson(t.route_json))}</pre>
                </div>
                <div class="panel">
                  <h4>Context Snapshot</h4>
                  <pre>${escapeHtml(safeText(t.context_snapshot || ''))}</pre>
                </div>
                <div class="panel">
                  <h4>Tool Runs Timeline</h4>
                  ${renderToolRuns(t.tool_runs)}
                </div>
                <div class="panel">
                  <h4>Code Runs Timeline</h4>
                  ${renderCodeRuns(t.code_runs)}
                </div>
              </div>
            </details>
          </td>`;
        refs.body.appendChild(detailTr);
      }

      refs.body.querySelectorAll('button[data-open]').forEach(btn => {
        btn.addEventListener('click', () => {
          const id = btn.getAttribute('data-open');
          const row = refs.body.querySelector(`tr[data-detail="${CSS.escape(id)}"]`);
          if (!row) return;
          row.style.display = row.style.display === 'none' ? '' : 'none';
        });
      });
    }

    function renderEvents() {
      refs.eventsBody.innerHTML = '';
      refs.eventsEmpty.style.display = recentEvents.length ? 'none' : 'block';
      if (!recentEvents.length) return;
      for (const ev of recentEvents) {
        const div = document.createElement('div');
        div.className = 'event-item';
        const tags = Array.isArray(ev.tags) ? ev.tags : [];
        div.innerHTML = `
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            <span class="pill mono">#${escapeHtml(ev.id || '')}</span>
            <span class="pill">${escapeHtml(ev.event_type || 'external_event')}</span>
            <span class="pill">${escapeHtml(ev.created_at || '')}</span>
            <span class="pill ${safeText(ev.result).toLowerCase() === 'success' ? 'ok' : 'err'}">${escapeHtml(ev.result || 'unknown')}</span>
          </div>
          <div style="margin-top:6px;">${escapeHtml(short(ev.summary || '', 240))}</div>
          <div style="margin-top:6px;">${tags.map(t => `<span class="pill mono">${escapeHtml(t)}</span>`).join('')}</div>
          <pre class="mono" style="margin-top:6px;">${escapeHtml(ev.context_preview || '')}</pre>
        `;
        refs.eventsBody.appendChild(div);
      }
    }

    async function loadTraces() {
      const limit = Number(refs.limit.value || 50);
      const res = await fetch(`/admin/api/ai-traces/recent?limit=${limit}`, {
        credentials: 'same-origin',
        cache: 'no-store'
      });
      if (res.status === 401) {
        refs.body.innerHTML = '';
        refs.empty.style.display = 'block';
        refs.empty.innerHTML = 'Session expired. <a href="/admin" class="btn" style="margin-left:8px">กลับไป login</a>';
        if (refreshTimer) clearInterval(refreshTimer);
        return;
      }
      const data = await res.json();
      allTraces = Array.isArray(data?.traces) ? data.traces : [];
      renderFilterOptions();
      applyFilters();
    }

    async function loadRecentEvents() {
      const res = await fetch('/admin/api/events/recent?source=ener_scan&limit=20', {
        credentials: 'same-origin',
        cache: 'no-store'
      });
      if (res.status === 401) return;
      const data = await res.json();
      recentEvents = Array.isArray(data?.events) ? data.events : [];
      renderEvents();
    }

    function renderArtifacts() {
      refs.artifactsBody.innerHTML = '';
      refs.artifactsEmpty.style.display = recentArtifacts.length ? 'none' : 'block';
      if (!recentArtifacts.length) return;
      for (const art of recentArtifacts) {
        const div = document.createElement('div');
        div.className = 'event-item';
        const tags = Array.isArray(art.tags) ? art.tags : [];
        div.innerHTML = `
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            <span class="pill mono">#${escapeHtml(art.id || '')}</span>
            <span class="pill">${escapeHtml(art.artifact_type || '-')}</span>
            <span class="pill">${escapeHtml(art.source || '-')}</span>
            <span class="pill">${escapeHtml(art.created_at || '')}</span>
            ${art.event_id ? `<span class="pill mono">event:${escapeHtml(art.event_id)}</span>` : ''}
          </div>
          <div style="margin-top:6px;font-weight:600;">${escapeHtml(short(art.title || '', 160))}</div>
          <div class="muted" style="margin-top:4px;">${escapeHtml(short(art.summary || '', 240))}</div>
          <div style="margin-top:6px;">${tags.map(t => `<span class="pill mono">${escapeHtml(t)}</span>`).join('')}</div>
          ${art.external_id ? `<div class="mono muted" style="margin-top:6px;">external_id: ${escapeHtml(art.external_id)}</div>` : ''}
        `;
        refs.artifactsBody.appendChild(div);
      }
    }

    async function loadArtifactCoverage() {
      const res = await fetch('/admin/api/artifacts/coverage?project_slug=ener-scan', {
        credentials: 'same-origin',
        cache: 'no-store'
      });
      if (res.status === 401) return;
      const data = await res.json();
      if (!data?.ok) {
        refs.artifactCoverage.textContent = 'Coverage: unavailable';
        return;
      }
      const evTotal = data?.events?.total ?? 0;
      const artTotal = data?.artifacts?.total ?? 0;
      const lastEv = data?.last_event_at || '-';
      const lastArt = data?.last_artifact_at || '-';
      refs.artifactCoverage.textContent =
        `Coverage: events ${evTotal} | artifacts ${artTotal} | last event ${lastEv} | last artifact ${lastArt}`;
    }

    async function runArtifactBackfill() {
      refs.artifactBackfillBtn.disabled = true;
      refs.artifactBackfillResult.textContent = 'Backfilling…';
      try {
        const res = await fetch('/admin/api/artifacts/backfill', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source: 'ener_scan',
            project_slug: 'ener-scan',
            limit: 500
          })
        });
        const data = await res.json();
        if (!res.ok || !data?.ok) {
          refs.artifactBackfillResult.textContent = 'Backfill failed';
          return;
        }
        refs.artifactBackfillResult.textContent =
          `created ${data.created || 0} | skipped ${data.skipped || 0} | failed ${data.failed || 0}`;
        await loadArtifactCoverage();
        await loadRecentArtifacts();
      } catch (_) {
        refs.artifactBackfillResult.textContent = 'Backfill error';
      } finally {
        refs.artifactBackfillBtn.disabled = false;
      }
    }

    async function loadRecentArtifacts() {
      const res = await fetch('/admin/api/artifacts/recent?project_slug=ener-scan&limit=20', {
        credentials: 'same-origin',
        cache: 'no-store'
      });
      if (res.status === 401) return;
      const data = await res.json();
      recentArtifacts = Array.isArray(data?.artifacts) ? data.artifacts : [];
      renderArtifacts();
    }

    function setupAutoRefresh() {
      if (refreshTimer) clearInterval(refreshTimer);
      const ms = Number(refs.auto.value || 0);
      if (ms > 0) refreshTimer = setInterval(loadTraces, ms);
    }

    refs.refreshBtn.addEventListener('click', () => {
      loadTraces();
      loadRecentEvents();
      loadArtifactCoverage();
      loadRecentArtifacts();
    });
    refs.artifactBackfillBtn.addEventListener('click', runArtifactBackfill);
    refs.auto.addEventListener('change', setupAutoRefresh);
    refs.limit.addEventListener('change', loadTraces);
    refs.source.addEventListener('change', applyFilters);
    refs.model.addEventListener('change', applyFilters);
    refs.intent.addEventListener('change', applyFilters);
    refs.status.addEventListener('change', applyFilters);
    refs.search.addEventListener('input', applyFilters);

    loadTraces();
    loadRecentEvents();
    loadArtifactCoverage();
    loadRecentArtifacts();
    setupAutoRefresh();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)

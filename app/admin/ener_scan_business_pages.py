from fastapi.responses import HTMLResponse


def build_ener_scan_business_html() -> HTMLResponse:
    html = """<!doctype html>
<html lang="th">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Ener Scan Business Dashboard</title>
  <style>
    :root {
      --bg: #0b0c0f;
      --card: #121317;
      --card2: #171922;
      --border: #262a36;
      --text: #e8eaf0;
      --muted: #8a90a2;
      --green: #22c55e;
      --blue: #60a5fa;
      --amber: #f59e0b;
      --purple: #a78bfa;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .wrap { max-width: 1280px; margin: 0 auto; padding: 18px 16px 32px; }
    .header {
      display: flex; justify-content: space-between; align-items: center;
      gap: 12px; flex-wrap: wrap; margin-bottom: 14px;
    }
    .title { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .title h1 { margin: 0; font-size: 1.2rem; }
    .subtitle { color: var(--muted); font-size: 0.85rem; margin: 4px 0 0; width: 100%; }
    .actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .btn, select {
      border: 1px solid var(--border); background: #0f1118; color: var(--text);
      border-radius: 8px; padding: 8px 10px; font-size: 0.88rem; cursor: pointer;
      text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
    }
    .btn:hover { border-color: #3b4254; }
    .mini-nav { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; font-size: 0.85rem; }
    .mini-nav a { color: var(--blue); text-decoration: none; }
    .mini-nav a:hover { text-decoration: underline; }
    .cards {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px; margin-bottom: 12px;
    }
    .card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 12px; min-height: 78px;
    }
    .card .k { color: var(--muted); font-size: 0.78rem; margin-bottom: 6px; }
    .card .v { font-size: 1.35rem; font-weight: 700; }
    .panel {
      background: var(--card2); border: 1px solid var(--border);
      border-radius: 10px; padding: 12px; margin-bottom: 12px;
    }
    .panel h3 { margin: 0 0 10px; font-size: 0.95rem; }
    .funnel { display: flex; gap: 8px; flex-wrap: wrap; align-items: stretch; }
    .funnel-step {
      flex: 1; min-width: 140px; background: #10141f; border: 1px solid #2a2f3e;
      border-radius: 8px; padding: 10px; text-align: center;
    }
    .funnel-step .n { font-size: 1.4rem; font-weight: 700; }
    .funnel-step .l { color: var(--muted); font-size: 0.8rem; margin-top: 4px; }
    .funnel-arrow { color: var(--muted); align-self: center; font-size: 1.2rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th, td { border-bottom: 1px solid #2a2f3e; padding: 8px 6px; text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    .bar-row { display: grid; grid-template-columns: 90px 1fr 48px; gap: 8px; align-items: center; margin-bottom: 6px; }
    .bar-track { background: #0f1118; border-radius: 4px; height: 10px; overflow: hidden; }
    .bar-fill { background: linear-gradient(90deg, #2563eb, #60a5fa); height: 100%; border-radius: 4px; }
    .pill {
      display: inline-block; border: 1px solid var(--border); border-radius: 999px;
      padding: 2px 8px; font-size: 0.75rem; margin-right: 4px;
    }
    .activity-item {
      border: 1px solid #2a2f3e; border-radius: 8px; background: #10141f;
      padding: 8px; margin-bottom: 8px;
    }
    .muted { color: var(--muted); font-size: 0.8rem; }
    .err { color: #f87171; padding: 12px; }
    .warn-box {
      background: #1c1408; border: 1px solid #b4530955; border-left: 4px solid var(--amber);
      border-radius: 10px; padding: 12px; margin-bottom: 12px; font-size: 0.85rem;
    }
    .warn-box h4 { margin: 0 0 8px; color: var(--amber); font-size: 0.9rem; }
    .warn-box ul { margin: 8px 0 0 18px; padding: 0; }
    .warn-box li { margin-bottom: 4px; }
    .dq-ok {
      background: #0f1a12; border: 1px solid #22c55e33; border-left: 4px solid var(--green);
      border-radius: 10px; padding: 10px 12px; margin-bottom: 12px; font-size: 0.85rem;
      color: var(--muted);
    }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    @media (max-width: 960px) {
      .cards { grid-template-columns: repeat(2, 1fr); }
      .grid-2 { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="mini-nav">
      <a href="/admin">← Admin</a>
      <span>·</span>
      <a href="/admin/ai-traces">Trace</a>
      <span>·</span>
      <strong>Ener Scan</strong>
    </div>
    <div class="header">
      <div class="title">
        <h1>Ener Scan Business Dashboard</h1>
        <p class="subtitle">ภาพรวม scan, report, payment และ activity จาก project_artifacts</p>
      </div>
      <div class="actions">
        <select id="rangeSelect" title="Range">
          <option value="today">Today</option>
          <option value="7d" selected>7d</option>
          <option value="30d">30d</option>
        </select>
        <button class="btn" id="refreshBtn" type="button">↻ Refresh</button>
        <label class="muted" for="autoRefresh">Auto</label>
        <select id="autoRefresh">
          <option value="0" selected>Off</option>
          <option value="30000">30s</option>
          <option value="60000">1m</option>
        </select>
      </div>
    </div>
    <p class="muted" style="margin:0 0 10px;font-size:0.85rem;">
      Business metrics exclude runtime/smoke diagnostics by default.
      <label style="margin-left:10px;cursor:pointer;">
        <input type="checkbox" id="includeDiagnostics" />
        Include diagnostics / smoke events
      </label>
    </p>
    <div id="diagnosticsNote" class="muted" style="display:none;margin:0 0 10px;font-size:0.85rem;"></div>

    <div id="sessionErr" class="err" style="display:none">Session expired — please log in to Admin again.</div>
    <div id="loadErr" class="err" style="display:none"></div>
    <div id="dataQualityBox" style="display:none"></div>

    <div class="cards" id="kpiCards">
      <div class="card"><div class="k">Scan Completed</div><div class="v" id="kpiScan">0</div></div>
      <div class="card"><div class="k">Reports Created</div><div class="v" id="kpiReport">0</div></div>
      <div class="card"><div class="k">Payments Approved</div><div class="v" id="kpiPay">0</div></div>
      <div class="card"><div class="k">Unique Users</div><div class="v" id="kpiUsers">0</div></div>
      <div class="card"><div class="k">Estimated Revenue</div><div class="v" id="kpiRev">฿0</div></div>
      <div class="card"><div class="k">Report → Payment %</div><div class="v" id="kpiConv">0%</div></div>
    </div>

    <div class="panel">
      <h3>Funnel</h3>
      <div class="funnel">
        <div class="funnel-step"><div class="n" id="fnScan">0</div><div class="l">Scan Completed</div></div>
        <div class="funnel-arrow">→</div>
        <div class="funnel-step"><div class="n" id="fnReport">0</div><div class="l">Report Created</div><div class="muted" id="fnScanReport">0%</div></div>
        <div class="funnel-arrow">→</div>
        <div class="funnel-step"><div class="n" id="fnPay">0</div><div class="l">Payment Approved</div><div class="muted" id="fnReportPay">0%</div></div>
      </div>
    </div>

    <div class="panel">
      <h3 id="trendTitle">Trend</h3>
      <div id="trendBars"></div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h3>Recent Activity</h3>
        <div id="recentBody"></div>
        <div id="recentEmpty" class="muted" style="display:none">No activity in range</div>
      </div>
      <div class="panel">
        <h3>Event / Artifact Breakdown</h3>
        <table>
          <thead><tr><th>Type</th><th>Count</th></tr></thead>
          <tbody id="breakdownBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    let refreshTimer = null;
    const refs = {
      range: document.getElementById('rangeSelect'),
      refreshBtn: document.getElementById('refreshBtn'),
      auto: document.getElementById('autoRefresh'),
      sessionErr: document.getElementById('sessionErr'),
      loadErr: document.getElementById('loadErr'),
      dataQualityBox: document.getElementById('dataQualityBox'),
      kpiScan: document.getElementById('kpiScan'),
      kpiReport: document.getElementById('kpiReport'),
      kpiPay: document.getElementById('kpiPay'),
      kpiUsers: document.getElementById('kpiUsers'),
      kpiRev: document.getElementById('kpiRev'),
      kpiConv: document.getElementById('kpiConv'),
      fnScan: document.getElementById('fnScan'),
      fnReport: document.getElementById('fnReport'),
      fnPay: document.getElementById('fnPay'),
      fnScanReport: document.getElementById('fnScanReport'),
      fnReportPay: document.getElementById('fnReportPay'),
      trendTitle: document.getElementById('trendTitle'),
      trendBars: document.getElementById('trendBars'),
      recentBody: document.getElementById('recentBody'),
      recentEmpty: document.getElementById('recentEmpty'),
      breakdownBody: document.getElementById('breakdownBody'),
      includeDiagnostics: document.getElementById('includeDiagnostics'),
      diagnosticsNote: document.getElementById('diagnosticsNote'),
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

    function fmtMoney(n) {
      const v = Number(n || 0);
      return '฿' + v.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
    }

    function fmtFunnelRate(raw, capped) {
      const r = Number(raw || 0);
      const c = Number(capped ?? raw ?? 0);
      if (r > 100) return r + '% raw · capped ' + c + '%';
      return r + '%';
    }

    function renderDataQuality(dq, coverage) {
      const box = refs.dataQualityBox;
      if (!dq) {
        box.style.display = 'none';
        box.innerHTML = '';
        return;
      }
      if (dq.status === 'warning') {
        const items = (dq.warnings || []).map(w => '<li>' + escapeHtml(w) + '</li>').join('');
        const note = 'ตัวเลข funnel ใช้ raw event counts; หาก event บางชนิดยังส่งไม่ครบ conversion อาจเกิน 100% ได้';
        const cov = coverage ? (
          '<div class="muted" style="margin-top:8px">coverage: scan_report=' +
          escapeHtml(coverage.scan_report_balance || '-') +
          ', payment_report=' + escapeHtml(coverage.payment_report_balance || '-') + '</div>'
        ) : '';
        box.className = 'warn-box';
        box.innerHTML = '<h4>⚠ Data quality warning</h4><ul>' + items + '</ul>' +
          '<div class="muted" style="margin-top:8px">' + escapeHtml(note) + '</div>' + cov;
        box.style.display = 'block';
        return;
      }
      box.className = 'dq-ok';
      box.textContent = 'Data quality: OK — event coverage looks consistent for this range.';
      box.style.display = 'block';
    }

    function badgeType(t) {
      const s = safeText(t).toLowerCase();
      if (s.includes('payment')) return 'pill' + ' style="border-color:#22c55e55"';
      if (s.includes('report') || s.includes('scan_report')) return 'pill' + ' style="border-color:#60a5fa55"';
      if (s.includes('scan') || s.includes('activity')) return 'pill' + ' style="border-color:#a78bfa55"';
      return 'pill';
    }

    function renderTrend(trend, range) {
      refs.trendTitle.textContent = range === 'today' ? 'Today' : ('Trend (' + range + ')');
      if (!Array.isArray(trend) || !trend.length) {
        refs.trendBars.innerHTML = '<div class="muted">No trend data</div>';
        return;
      }
      const maxTotal = Math.max(1, ...trend.map(d => Number(d.total || 0)));
      refs.trendBars.innerHTML = trend.map(d => {
        const total = Number(d.total || 0);
        const pct = Math.round((total / maxTotal) * 100);
        return `
          <div class="bar-row">
            <span class="muted mono">${escapeHtml(d.date || '')}</span>
            <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
            <span class="muted">${total}</span>
          </div>
          <div class="muted" style="margin:-2px 0 8px 98px;font-size:0.75rem">
            scan ${Number(d.scan_completed||0)} · report ${Number(d.report_created||0)} · pay ${Number(d.payment_approved||0)}
          </div>`;
      }).join('');
    }

    function renderRecent(items) {
      refs.recentBody.innerHTML = '';
      refs.recentEmpty.style.display = items.length ? 'none' : 'block';
      for (const it of items) {
        const div = document.createElement('div');
        div.className = 'activity-item';
        const amt = Number(it.amount || 0) > 0 ? `<span class="pill">${fmtMoney(it.amount)}</span>` : '';
        div.innerHTML = `
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
            <span class="muted">${escapeHtml(it.created_at || '')}</span>
            <span class="${badgeType(it.artifact_type)}">${escapeHtml(it.artifact_type || '-')}</span>
            ${amt}
          </div>
          <div style="margin-top:6px;font-weight:600">${escapeHtml(it.title || '')}</div>
          <div class="muted" style="margin-top:4px">${escapeHtml(it.summary || '')}</div>
          ${it.external_user_id ? `<div class="muted" style="margin-top:4px">user: ${escapeHtml(it.external_user_id)}</div>` : ''}
          ${it.external_id ? `<div class="muted mono" style="margin-top:2px">id: ${escapeHtml(it.external_id)}</div>` : ''}
        `;
        refs.recentBody.appendChild(div);
      }
    }

    function renderBreakdown(byArtifact, byEvent) {
      const rows = [];
      const seen = new Set();
      for (const row of (byArtifact || [])) {
        const k = 'artifact:' + row.artifact_type;
        if (seen.has(k)) continue;
        seen.add(k);
        rows.push({ label: row.artifact_type, count: row.count });
      }
      for (const row of (byEvent || [])) {
        const k = 'event:' + row.event_type;
        if (seen.has(k)) continue;
        seen.add(k);
        rows.push({ label: row.event_type + ' (event)', count: row.count });
      }
      refs.breakdownBody.innerHTML = rows.map(r =>
        `<tr><td>${escapeHtml(r.label)}</td><td>${escapeHtml(r.count)}</td></tr>`
      ).join('') || '<tr><td colspan="2" class="muted">—</td></tr>';
    }

    function applyData(data) {
      const s = data.summary || {};
      const dq = s.data_quality || {};
      refs.kpiScan.textContent = s.scan_completed ?? 0;
      refs.kpiReport.textContent = s.report_created ?? 0;
      refs.kpiPay.textContent = s.payment_approved ?? 0;
      refs.kpiUsers.textContent = s.unique_users ?? 0;
      refs.kpiRev.textContent = fmtMoney(s.estimated_revenue);
      const payRaw = s.report_to_payment_rate_raw ?? s.report_to_payment_rate ?? 0;
      const payCap = s.report_to_payment_rate_capped ?? payRaw;
      refs.kpiConv.textContent = fmtFunnelRate(payRaw, payCap);
      refs.fnScan.textContent = s.scan_completed ?? 0;
      refs.fnReport.textContent = s.report_created ?? 0;
      refs.fnPay.textContent = s.payment_approved ?? 0;
      const scanReportRaw = s.scan_to_report_rate_raw ?? s.scan_to_report_rate ?? 0;
      const scanReportCap = s.scan_to_report_rate_capped ?? scanReportRaw;
      const reportPayRaw = s.report_to_payment_rate_raw ?? s.report_to_payment_rate ?? 0;
      const reportPayCap = s.report_to_payment_rate_capped ?? reportPayRaw;
      refs.fnScanReport.textContent = 'scan→report ' + fmtFunnelRate(scanReportRaw, scanReportCap);
      refs.fnScanReport.title = 'Raw rate from artifact counts; capped at 100% for funnel display';
      refs.fnReportPay.textContent = 'report→pay ' + fmtFunnelRate(reportPayRaw, reportPayCap);
      refs.fnReportPay.title = 'Raw rate from artifact counts; capped at 100% for funnel display';
      renderDataQuality(dq, data.coverage || {});
      renderTrend(data.trend || [], data.range || '7d');
      renderRecent(data.recent || []);
      renderBreakdown(data.by_artifact_type || [], data.by_event_type || []);
      const diag = data.diagnostics || {};
      const excluded = Number(diag.excluded || 0);
      if (excluded > 0 && !diag.included) {
        refs.diagnosticsNote.textContent =
          'Excluded ' + excluded + ' diagnostic artifacts from business metrics.';
        refs.diagnosticsNote.style.display = 'block';
      } else {
        refs.diagnosticsNote.style.display = 'none';
        refs.diagnosticsNote.textContent = '';
      }
    }

    async function loadSummary() {
      refs.loadErr.style.display = 'none';
      const range = refs.range.value || '7d';
      const includeDiag = refs.includeDiagnostics.checked ? 'true' : 'false';
      try {
        const res = await fetch(
          '/admin/api/business/ener-scan/summary?range=' + encodeURIComponent(range) +
          '&include_diagnostics=' + includeDiag,
          {
          credentials: 'same-origin',
          cache: 'no-store'
        });
        if (res.status === 401) {
          refs.sessionErr.style.display = 'block';
          if (refreshTimer) clearInterval(refreshTimer);
          return;
        }
        refs.sessionErr.style.display = 'none';
        const data = await res.json();
        if (!data.ok) {
          refs.loadErr.textContent = 'Failed to load summary';
          refs.loadErr.style.display = 'block';
          return;
        }
        applyData(data);
      } catch (e) {
        refs.loadErr.textContent = 'Load error';
        refs.loadErr.style.display = 'block';
      }
    }

    function setupAutoRefresh() {
      if (refreshTimer) clearInterval(refreshTimer);
      const ms = Number(refs.auto.value || 0);
      if (ms > 0) refreshTimer = setInterval(loadSummary, ms);
    }

    refs.refreshBtn.addEventListener('click', loadSummary);
    refs.range.addEventListener('change', loadSummary);
    refs.includeDiagnostics.addEventListener('change', loadSummary);
    refs.auto.addEventListener('change', setupAutoRefresh);
    loadSummary();
    setupAutoRefresh();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)

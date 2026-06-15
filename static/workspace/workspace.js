document.addEventListener('DOMContentLoaded', function() {
  try {
  const richMarkdown = window.renderMarkdown;

  const state = {
    streaming: false,
    currentProject: null,
    projectName: 'All Chats',
    toastTimer: null,
    pendingImageFile: null,
    pendingPreviewUrl: '',
    secretaryHistoryLoaded: false,
    officeActivityTimer: null,
    officeEventSource: null,
    officeEventReconnectTimer: null,
    pbClockTimer: null,
  };

  const _AGENT_EMOJI = {
    MainChatAgent: '💬',
    CodeAgent: '💻',
    NewsAgent: '📰',
    GmailAgent: '📧',
    MemoryAgent: '🧠',
    MonitorAgent: '🖥️',
    EnerAgent: '⚡',
    ContentAgent: '✍️',
    DigestAgent: '📋',
    TarotAgent: '🔮',
    TaskAgent: '✅',
    SessionAgent: '📅',
    BriefingAgent: '📊',
    GithubAgent: '🐙',
    LogKeeper: '📝',
    ThinkTeam: '🧩',
    SecretaryAgent: '👩‍💼',
  };

  const chatMessages = document.getElementById('chat-messages-inner') || document.getElementById('chat-messages');
  const chatInput = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');
  const projectNav = document.getElementById('project-nav');
  const activeModelBadge = document.getElementById('active-model-badge');
  const dropZone = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');
  const slashMenu = document.getElementById('slash-menu');
  const SLASH_COMMANDS = [
    { cmd: '/note', desc: 'บันทึกความคิด → BrainAgent' },
    { cmd: '/task', desc: 'สร้าง task ใหม่' },
    { cmd: '/tasks', desc: 'ดู task ทั้งหมด' },
    { cmd: '/standup', desc: 'สร้าง daily standup report' },
    { cmd: '/remember', desc: 'บันทึก long-term memory' },
    { cmd: '/memory', desc: 'ดู memory ทั้งหมด' },
    { cmd: '/think', desc: 'ถกไอเดีย 3 รอบ (brainstorm)' },
    { cmd: '/news', desc: 'ดูข่าว AI/Tech วันนี้' },
    { cmd: '/today', desc: 'สรุปวันนี้' },
    { cmd: '/tarot', desc: 'ดูดวงไพ่ทาโรต์' },
    { cmd: '/code', desc: 'เขียน/review code' },
    { cmd: '/content', desc: 'สร้าง caption/script' },
    { cmd: '/ener', desc: 'วิเคราะห์พระเครื่อง' },
    { cmd: '/learn', desc: 'บันทึกบทเรียน' },
    { cmd: '/help', desc: 'ดูคำสั่งทั้งหมด' },
  ];

  function escapeHtml(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function getModelLabelFromSelect(modelId) {
    const sel = document.getElementById('model-select');
    if (!sel || !modelId) return '';
    for (const opt of sel.options) {
      if (opt.value === modelId) return String(opt.textContent || '').trim();
    }
    const tail = String(modelId).split('/').pop() || '';
    return tail.replace(/:free$/i, ' (free)');
  }

  function formatAiMeta(modelId, modelLabel) {
    const label = String(modelLabel || '').trim() || getModelLabelFromSelect(modelId);
    return label ? `Ener-AI · ${label}` : 'Ener-AI';
  }

  function renderMarkdown(text, options) {
    if (typeof richMarkdown === 'function') {
      return richMarkdown(text, options);
    }
    return escapeHtml(text || '').replace(/\n/g, '<br>');
  }

  function renderAiMessageContent(textEl, rawText) {
    if (typeof window.renderMarkdownInto === 'function') {
      window.renderMarkdownInto(textEl, rawText);
      if (typeof window.bindCodeCopyButtons === 'function') {
        window.bindCodeCopyButtons(textEl);
      }
      return;
    }
    if (!textEl) return;
    const cleaned = typeof window.sanitizeAiContent === 'function'
      ? window.sanitizeAiContent(rawText)
      : rawText;
    textEl.dataset.raw = cleaned;
    textEl.classList.add('markdown-body');
    textEl.innerHTML = renderMarkdown(cleaned);
    if (typeof window.bindCodeCopyButtons === 'function') {
      window.bindCodeCopyButtons(textEl);
    }
  }

  function getMessagePlainText(textEl) {
    if (!textEl) return '';
    let raw = textEl.dataset.raw;
    if (raw && typeof window.sanitizeAiContent === 'function') {
      raw = window.sanitizeAiContent(raw);
    }
    if (raw) return raw;
    const codes = textEl.querySelectorAll('pre code, pre');
    if (codes.length > 0) {
      return Array.from(codes).map((el) => el.textContent || '').join('\n\n').trim();
    }
    return textEl.textContent || '';
  }

  async function copyAiMessage(btn) {
    const wrap = btn.closest('.ai-bubble-wrap');
    const textEl = wrap?.querySelector('.msg-text');
    const textToCopy = getMessagePlainText(textEl);
    if (!textToCopy) {
      showToast('ไม่มีข้อความให้ copy');
      return;
    }
    try {
      await navigator.clipboard.writeText(textToCopy);
      btn.textContent = '✓ Copied';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.textContent = 'Copy';
        btn.classList.remove('copied');
      }, 2000);
    } catch (error) {
      showToast('Copy failed');
    }
  }

  function attachMessageToolbar(wrap) {
    if (!wrap || wrap.dataset.toolbarBound === '1') return;
    const textEl = wrap.querySelector('.msg-text');
    let actions = wrap.querySelector('.msg-actions');
    if (!actions) {
      actions = document.createElement('div');
      actions.className = 'msg-actions';
      wrap.appendChild(actions);
    }
    wrap.querySelectorAll(':scope > .copy-btn').forEach((btn) => btn.remove());
    actions.querySelectorAll('.copy-btn').forEach((btn) => btn.remove());

    const toolbar = document.createElement('div');
    toolbar.className = 'msg-toolbar';
    toolbar.innerHTML = `
      <button type="button" class="msg-btn msg-btn-md" data-mode="markdown">MD</button>
      <button type="button" class="msg-btn msg-btn-plain" data-mode="plain">Plain</button>
      <button type="button" class="msg-btn msg-btn-raw">Raw</button>
      <button type="button" class="copy-btn" aria-label="Copy message">Copy</button>
    `;
    actions.appendChild(toolbar);

    const setActive = (mode) => {
      toolbar.querySelectorAll('.msg-btn[data-mode]').forEach((b) => {
        b.classList.toggle('active', b.dataset.mode === mode);
      });
    };

    const rerender = (mode) => {
      const raw = textEl?.dataset.raw || textEl?.getAttribute('data-raw') || '';
      if (!textEl || !raw) return;
      if (typeof window.renderMarkdownInto === 'function') {
        window.renderMarkdownInto(textEl, raw, mode);
      }
      setActive(mode);
      try {
        localStorage.setItem('ws_render_mode', mode);
      } catch (e) {
        /* ignore */
      }
    };

    toolbar.querySelector('.msg-btn-md').addEventListener('click', () => rerender('markdown'));
    toolbar.querySelector('.msg-btn-plain').addEventListener('click', () => rerender('plain'));
    toolbar.querySelector('.msg-btn-raw').addEventListener('click', () => {
      if (typeof window.showPlainInto === 'function') window.showPlainInto(textEl);
    });
    toolbar.querySelector('.copy-btn').addEventListener('click', () => copyAiMessage(toolbar.querySelector('.copy-btn')));

    const mode = textEl?.dataset.renderMode
      || (typeof window.getRenderMode === 'function' ? window.getRenderMode() : 'markdown');
    setActive(mode);
    wrap.dataset.toolbarBound = '1';
  }

  function attachCopyButton(wrap) {
    if (!wrap || wrap.dataset.toolbarBound === '1') return;
    attachMessageToolbar(wrap);
  }

  function renderChatMessage(msg) {
    const role = msg.role === 'user' ? 'user' : 'ai';
    const row = document.createElement('div');
    row.className = `msg-row ${role}-row`;
    const content = msg.content || '';
    if (role === 'user') {
      row.innerHTML = `
        <div class="msg-bubble user-bubble">
          <div class="msg-text">${escapeHtml(content)}</div>
        </div>
      `;
      return row;
    }
    const meta = msg.model_label
      ? formatAiMeta(msg.model_used, msg.model_label)
      : (msg.source === 'web' ? 'Ener-AI' : 'Telegram');
    row.innerHTML = `
      <div class="ws-ai-avatar" aria-hidden="true">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 11-12h-9l1-8z"/></svg>
      </div>
      <div class="ai-bubble-wrap">
        <div class="msg-bubble ai-bubble">
          <div class="msg-text markdown-body"></div>
          <div class="msg-meta">${escapeHtml(meta)}</div>
        </div>
        <div class="msg-actions">
          <button type="button" class="copy-btn" aria-label="Copy message">Copy</button>
        </div>
      </div>
    `;
    const textEl = row.querySelector('.msg-text');
    if (textEl) renderAiMessageContent(textEl, content);
    attachCopyButton(row.querySelector('.ai-bubble-wrap'));
    return row;
  }

  function enhanceSsrChatMessages() {
    if (!chatMessages) return;
    chatMessages.querySelectorAll('.msg-row.ai-row').forEach((row) => {
      const bubble = row.querySelector('.msg-bubble.ai-bubble');
      const textEl = row.querySelector('.msg-text');
      if (!bubble || !textEl) return;

      if (!row.querySelector('.ws-ai-avatar')) {
        const avatar = document.createElement('div');
        avatar.className = 'ws-ai-avatar';
        avatar.setAttribute('aria-hidden', 'true');
        avatar.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 11-12h-9l1-8z"/></svg>';
        row.insertBefore(avatar, row.querySelector('.ai-bubble-wrap') || bubble);
      }

      let wrap = row.querySelector('.ai-bubble-wrap');
      if (!wrap) {
        wrap = document.createElement('div');
        wrap.className = 'ai-bubble-wrap';
        bubble.parentNode.insertBefore(wrap, bubble);
        wrap.appendChild(bubble);
        const actions = document.createElement('div');
        actions.className = 'msg-actions';
        wrap.appendChild(actions);
      }

      let raw = textEl.getAttribute('data-raw') || textEl.dataset.raw || textEl.textContent || '';
      if (raw.startsWith('"') && raw.endsWith('"')) {
        try { raw = JSON.parse(raw); } catch (e) { /* keep as-is */ }
      }
      renderAiMessageContent(textEl, raw);
      textEl.dataset.md = '1';
      attachCopyButton(wrap);
    });
  }

  function showToast(msg) {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.style.display = 'block';
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      toast.style.display = 'none';
    }, 3000);
  }

  function scrollToBottom() {
    const container = document.getElementById('chat-messages') || chatMessages;
    if (container) container.scrollTop = container.scrollHeight;
  }

  function dayKeyFromCreatedAt(createdAt) {
    if (!createdAt) return '';
    return String(createdAt).slice(0, 10);
  }

  function formatDayLabel(dayKey) {
    if (!dayKey) return '';
    const parts = dayKey.split('-');
    if (parts.length !== 3) return dayKey;
    const months = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
    const month = months[Number(parts[1]) - 1] || parts[1];
    return `${parts[2]}-${month}-${parts[0]}`.toLowerCase();
  }

  function ensureDayMarker(dayKey) {
    if (!dayKey || document.getElementById('chat-day-' + dayKey)) return;
    const marker = document.createElement('div');
    marker.id = 'chat-day-' + dayKey;
    marker.className = 'ws-chat-day-marker';
    marker.textContent = formatDayLabel(dayKey);
    chatMessages.appendChild(marker);
  }

  function getWorkspaceDateFilter() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('date') === 'all') return 'all';
    const fromUrl = params.get('date') || params.get('scroll');
    if (fromUrl) return fromUrl.slice(0, 10);
    if (window.__WORKSPACE_SHOW_ALL__) return 'all';
    return window.__WORKSPACE_CHAT_DATE__ || window.__WORKSPACE_TODAY__ || '';
  }

  function workspaceHistoryQuery() {
    const params = new URLSearchParams();
    if (window._currentProject) params.set('project_id', String(window._currentProject));
    const dateFilter = getWorkspaceDateFilter();
    if (dateFilter === 'all') {
      params.set('date', 'all');
      params.set('limit', '500');
    } else {
      params.set('date', dateFilter);
      params.set('limit', '300');
    }
    return params.toString() ? `?${params.toString()}` : '';
  }

  function setSendButtonState(loading) {
    if (!sendBtn) return;
    sendBtn.disabled = loading;
    sendBtn.setAttribute('aria-busy', loading ? 'true' : 'false');
  }

  function currentTimeLabel() {
    return new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  }

  function updateSlashMenu(value) {
    if (!value.startsWith('/')) {
      slashMenu.style.display = 'none';
      return;
    }
    const q = value.toLowerCase();
    const matches = SLASH_COMMANDS.filter((c) => c.cmd.startsWith(q));
    if (matches.length === 0) {
      slashMenu.style.display = 'none';
      return;
    }
    slashMenu.innerHTML = matches.map((c, i) => `
      <div class="slash-item ${i === 0 ? 'selected' : ''}" onclick="selectSlash('${c.cmd}')">
        <span class="slash-cmd">${c.cmd}</span>
        <span class="slash-desc">${c.desc}</span>
      </div>
    `).join('');
    slashMenu.style.display = 'block';
    window._slashIndex = 0;
  }

  function selectSlash(cmd) {
    chatInput.value = cmd + ' ';
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + 'px';
    chatInput.focus();
    slashMenu.style.display = 'none';
  }

  function appendUserBubble(text, meta='', imageUrl='') {
    const row = renderChatMessage({role: 'user', content: text, source: 'web'});
    const bubble = row.querySelector('.user-bubble');
    if (bubble && imageUrl) {
      const img = document.createElement('img');
      img.className = 'msg-user-image';
      img.src = imageUrl;
      img.alt = 'Screenshot';
      bubble.insertBefore(img, bubble.firstChild);
    }
    if (meta && bubble) {
      bubble.insertAdjacentHTML('beforeend', `<div class="msg-meta">${escapeHtml(meta)}</div>`);
    }
    chatMessages.appendChild(row);
    updateChatWelcome();
    scrollToBottom();
    return row;
  }

  function appendAiBubble(text, meta='Ener-AI') {
    const row = renderChatMessage({role: 'assistant', content: text, source: 'web'});
    const metaEl = row.querySelector('.msg-meta');
    if (metaEl) metaEl.textContent = meta;
    chatMessages.appendChild(row);
    updateChatWelcome();
    scrollToBottom();
    return row.querySelector('.ai-bubble');
  }

  function appendThinkingBubble(id) {
    const row = document.createElement('div');
    row.id = id;
    row.className = 'msg-row ai-row';
    row.innerHTML = `
      <div class="ws-ai-avatar" aria-hidden="true">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 11-12h-9l1-8z"/></svg>
      </div>
      <div class="msg-bubble ai-bubble thinking">
        <span class="dot"></span><span class="dot"></span><span class="dot"></span>
        <div class="thinking-status">กำลังส่งคำขอ...</div>
      </div>
    `;
    chatMessages.appendChild(row);
    scrollToBottom();
    return row;
  }

  async function streamWorkspaceChat(msg, model, thinkingId) {
    let meta = formatAiMeta(model, getModelLabelFromSelect(model));
    const response = await fetch('/workspace/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        text: msg,
        message: msg,
        project_id: window._currentProject || null,
        model,
      }),
    });

    if (!response.ok) {
      const errBody = await response.text().catch(() => '');
      let detail = errBody;
      try {
        const parsed = JSON.parse(errBody);
        detail = parsed.detail || parsed.error || errBody;
      } catch (e) {
        /* keep raw body */
      }
      throw new Error(detail || `Request failed (${response.status})`);
    }
    if (!response.body) {
      throw new Error('Streaming not supported');
    }

    document.getElementById(thinkingId)?.remove();
    const aiBubble = appendAiBubble('', meta);
    const wrap = aiBubble?.closest('.ai-bubble-wrap');
    const textEl = wrap?.querySelector('.msg-text') || aiBubble?.querySelector('.msg-text');
    if (textEl) {
      textEl.classList.add('plain-text', 'streaming-live');
      textEl.classList.remove('markdown-body');
      textEl.dataset.raw = '';
      textEl.textContent = '';
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let sseBuf = '';
    let accumulated = '';
    let scrollQueued = false;

    const queueScroll = () => {
      if (scrollQueued) return;
      scrollQueued = true;
      requestAnimationFrame(() => {
        scrollQueued = false;
        scrollToBottom();
      });
    };

    const handlePayload = (payload) => {
      if (!payload || !payload.type) return;
      if (payload.type === 'token' && payload.text) {
        accumulated += payload.text;
        if (textEl) {
          textEl.dataset.raw = accumulated;
          textEl.textContent = accumulated;
        }
        queueScroll();
        return;
      }
      if (payload.type === 'done') {
        meta = formatAiMeta(payload.model || model, payload.model_label);
        const metaEl = wrap?.querySelector('.msg-meta') || aiBubble?.querySelector('.msg-meta');
        if (metaEl) metaEl.textContent = meta;
        return;
      }
      if (payload.type === 'error') {
        throw new Error(payload.text || 'Stream failed');
      }
    };

    const parseSseChunk = (chunk) => {
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const jsonText = line.replace(/^data:\s*/, '').trim();
        if (!jsonText) continue;
        try {
          handlePayload(JSON.parse(jsonText));
        } catch (err) {
          if (err instanceof SyntaxError) continue;
          throw err;
        }
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      sseBuf += decoder.decode(value, { stream: true });
      const parts = sseBuf.split('\n\n');
      sseBuf = parts.pop() || '';
      parts.forEach(parseSseChunk);
    }
    if (sseBuf.trim()) parseSseChunk(sseBuf);

    if (textEl) textEl.classList.remove('streaming-live');
    const finalText = accumulated.trim() || 'ยังไม่มีคำตอบตอนนี้';
    if (textEl) renderAiMessageContent(textEl, finalText);
    loadProjects().catch(() => {});
    if (typeof window.refreshSidebarStats === 'function') window.refreshSidebarStats();
    scrollToBottom();
  }

  function getOfficeSecMessages() {
    return document.getElementById('office-secretary-messages');
  }

  function scrollSecretaryToBottom() {
    const container = getOfficeSecMessages();
    if (container) container.scrollTop = container.scrollHeight;
  }

  function updateSecretaryWelcome() {
    const welcome = document.getElementById('office-sec-welcome');
    const container = getOfficeSecMessages();
    if (!welcome || !container) return;
    const hasMessages = Boolean(container.querySelector('.office-sec-msg'));
    welcome.style.display = hasMessages ? 'none' : '';
  }

  function focusOfficeSecretary(el) {
    if (el) el.classList.add('office-agent-card--active');
    const section = document.getElementById('office-right');
    section?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    document.getElementById('office-sec-input')?.focus();
    return false;
  }

  function stopOfficeActivityRefresh() {
    if (state.officeActivityTimer) {
      clearInterval(state.officeActivityTimer);
      state.officeActivityTimer = null;
    }
  }

  async function loadOfficeActivity() {
    const feed = document.getElementById('office-activity-feed');
    if (!feed) return;
    try {
      const data = await api('/workspace/office/activity');
      feed.innerHTML = '';
      const items = data.items || [];
      if (!items.length) {
        feed.innerHTML =
          '<div style="color:oklch(0.40 0.01 250);text-align:center;padding:8px;">ยังไม่มี activity</div>';
        return;
      }
      for (const item of items) {
        const emoji = _AGENT_EMOJI[item.agent] || '🤖';
        const t =
          item.mins_ago < 60
            ? `${item.mins_ago}m`
            : `${Math.floor(item.mins_ago / 60)}h`;
        const color = item.success
          ? 'oklch(0.60 0.15 150)'
          : 'oklch(0.60 0.15 30)';
        const icon = item.success ? '✓' : '✗';
        const label = String(item.agent || '').replace(/Agent$/, '');
        const div = document.createElement('div');
        div.className = 'office-activity-row';
        div.innerHTML = `
          <span style="font-size:11px;flex-shrink:0;">${emoji}</span>
          <span style="flex:1;color:oklch(0.65 0.02 250);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(label)}</span>
          <span style="color:${color};flex-shrink:0;">${icon}</span>
          <span style="color:oklch(0.40 0.01 250);flex-shrink:0;font-size:9px;">${t}</span>`;
        feed.appendChild(div);
      }
    } catch (error) {
      console.warn('office activity load failed', error);
    }
  }

  function _agentShortName(agentName) {
    return String(agentName || '').replace(/Agent$/, '');
  }

  function _getBuildingDesk(agentName) {
    return document.querySelector(
      `#pixel-building .pb-mini-desk[data-agent-name="${agentName}"], #pixel-building .pb-desk[data-agent-name="${agentName}"]`
    );
  }

  function _getBuildingSvg() {
    return document.getElementById('building-svg-lines');
  }

  function _deskCenter(agentName) {
    const desk = _getBuildingDesk(agentName);
    const svg = _getBuildingSvg();
    if (!desk || !svg) return null;
    const dr = desk.getBoundingClientRect();
    const sr = svg.getBoundingClientRect();
    return {
      x: dr.left - sr.left + dr.width / 2,
      y: dr.top - sr.top + dr.height / 2,
    };
  }

  function drawOfficeConnection(fromAgent, toAgent, type) {
    const svg = _getBuildingSvg();
    if (!svg) return;
    const A = _deskCenter(fromAgent);
    const B = _deskCenter(toAgent);
    if (!A || !B) return;

    const color = type === 'complete' ? '#0f6' : '#fa0';
    const dur = type === 'complete' ? 2500 : 3500;

    const cx = (A.x + B.x) / 2;
    const cy = Math.min(A.y, B.y) - 24;
    const d = `M${A.x},${A.y} Q${cx},${cy} ${B.x},${B.y}`;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', color);
    path.setAttribute('stroke-width', '1.5');
    path.setAttribute('stroke-dasharray', '5 3');
    path.setAttribute('filter', 'url(#b-glow)');
    path.setAttribute('opacity', '0.9');
    path.style.animation = 'dash-travel 0.35s linear infinite';
    svg.appendChild(path);

    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('r', '3');
    dot.setAttribute('fill', color);
    dot.setAttribute('filter', 'url(#b-glow)');
    const am = document.createElementNS('http://www.w3.org/2000/svg', 'animateMotion');
    am.setAttribute('dur', '0.7s');
    am.setAttribute('repeatCount', type === 'route' ? '3' : '2');
    am.setAttribute('fill', 'freeze');
    am.setAttribute('path', d);
    dot.appendChild(am);
    svg.appendChild(dot);

    setTimeout(() => {
      path.remove();
      dot.remove();
    }, dur);
  }

  function _showBuildingBubble(agentName, message, type) {
    const desk = _getBuildingDesk(agentName);
    if (!desk) return;

    desk.querySelectorAll('.pb-speech').forEach((b) => b.remove());
    desk.classList.add('routing');
    setTimeout(() => desk.classList.remove('routing'), 4000);

    const bubble = document.createElement('div');
    bubble.className = 'pb-speech';
    bubble.textContent = message;
    const stroke = type === 'complete' ? '#0f6' : '#fa0';
    bubble.style.borderColor = stroke;
    bubble.style.color = stroke;
    desk.appendChild(bubble);
    setTimeout(() => bubble.remove(), 3500);
  }

  function _showOfficeBubble(agentName, message, type) {
    _showBuildingBubble(agentName, message, type);
  }

  function stopPBClock() {
    if (state.pbClockTimer) {
      clearInterval(state.pbClockTimer);
      state.pbClockTimer = null;
    }
  }

  function startPBClock() {
    const el = document.getElementById('pb-clock');
    if (!el) return;
    stopPBClock();
    const tick = () => {
      const now = new Date();
      el.textContent = now.toLocaleTimeString('th-TH', { hour12: false });
    };
    tick();
    state.pbClockTimer = setInterval(tick, 1000);
  }

  function startBuildingClock() {
    const el = document.getElementById('building-clock');
    if (!el) return;
    if (state.buildingClockTimer) {
      clearInterval(state.buildingClockTimer);
    }
    const tick = () => {
      el.textContent = new Date().toLocaleTimeString('th-TH', { hour12: false });
    };
    tick();
    state.buildingClockTimer = setInterval(tick, 1000);
  }

  function openBuildingFloor(floorKey) {
    const cmds = {
      hq: '',
      ener: '/ener ',
      tech: '/code ',
      intel: '/news ',
      ops: '/tasks ',
      exec: '',
    };
    const cmd = cmds[floorKey] || '';

    if (typeof showPanel === 'function') {
      showPanel('office');
    }

    const input =
      document.getElementById('office-sec-input') ||
      document.getElementById('chat-input');
    if (input) {
      input.value = cmd;
      input.dispatchEvent(new Event('input'));
      input.focus();
    }
  }

  function _updateActivityFeedItem(fromAgent, toAgent, msg, type) {
    const feed = document.getElementById('office-activity-feed');
    if (!feed) return;
    const placeholder = feed.querySelector('[data-office-feed-placeholder]');
    if (placeholder) placeholder.remove();

    const fromEmoji = _AGENT_EMOJI[fromAgent] || '🤖';
    const toEmoji = _AGENT_EMOJI[toAgent] || '🤖';
    const isRoute = type === 'route';
    const arrowColor = isRoute ? 'oklch(0.65 0.15 60)' : 'oklch(0.60 0.18 150)';
    const arrow = isRoute ? '→' : '✓';
    const arrowClass = isRoute ? 'activity-arrow-route' : 'activity-arrow-complete';
    const fromName = _agentShortName(fromAgent);
    const toName = _agentShortName(toAgent);
    const direction = `${fromName}→${toName}`;

    const div = document.createElement('div');
    div.className = 'office-activity-row';
    div.style.cssText =
      'animation:office-feed-fade-in 0.3s ease;display:flex;align-items:center;gap:3px;padding:3px 4px;border-radius:3px;';
    div.innerHTML = `
      <span style="font-size:12px;flex-shrink:0;">${fromEmoji}</span>
      <span class="${arrowClass}" style="font-size:9px;flex-shrink:0;font-weight:700;color:${arrowColor};">${arrow}</span>
      <span style="font-size:12px;flex-shrink:0;">${toEmoji}</span>
      <span style="flex:1;font-size:9px;color:oklch(0.60 0.02 250);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
            title="${escapeHtml(direction)} — ${escapeHtml(msg)}">${escapeHtml(direction)}</span>
      <span style="font-size:8px;color:oklch(0.38 0.01 250);flex-shrink:0;">now</span>`;
    feed.insertBefore(div, feed.firstChild);
    while (feed.children.length > 25) feed.removeChild(feed.lastChild);
  }

  // ── Office Map iframe bridge ──────────────────────────────────
  const _OFFICE_AGENT_MAP_ID = {
    SecretaryAgent: 'secretary',
    MainChatAgent: 'chat',
    MemoryAgent: 'memory',
    EnerAgent: 'ener',
    ContentAgent: 'content',
    TarotAgent: 'tarot',
    CodeAgent: 'code',
    MonitorAgent: 'monitor',
    GithubAgent: 'github',
    NewsAgent: 'news',
    DigestAgent: 'digest',
    ThinkTeam: 'think',
    GmailAgent: 'gmail',
    TaskAgent: 'tasks',
    LogKeeper: 'logs',
    SessionAgent: 'session',
    BriefingAgent: 'briefing',
  };

  // Valid lowercase map IDs used inside office_map.html
  const _MAP_VALID_IDS = new Set([
    'secretary','chat','memory','ener','content','tarot',
    'code','monitor','github','news','digest','think',
    'gmail','tasks','logs','session','briefing',
  ]);

  function _mapFrame() {
    const f = document.getElementById('office-map-frame');
    return f ? f.contentWindow : null;
  }

  function _agentNameToMapId(name) {
    if (!name) return '';
    // If already a valid lowercase map ID (from secretary route event), return as-is
    const lower = (name || '').toLowerCase().trim();
    if (_MAP_VALID_IDS.has(lower)) return lower;
    // Otherwise translate from agent class name (e.g. NewsAgent → news)
    return _OFFICE_AGENT_MAP_ID[name] || '';
  }

  function notifyMapRoute(fromId, toId, msg) {
    const w = _mapFrame();
    if (w && w.triggerRoute) w.triggerRoute(fromId, toId, msg || '');
  }

  function pillClickAgent(agentName, shortName) {
    const inp = document.getElementById('office-sec-input');
    if (inp) {
      inp.value = '@' + shortName + ' ';
      inp.dispatchEvent(new Event('input'));
      inp.focus();
    }
  }

  function _handleOfficeMapEvent(evt) {
    if (!evt || !evt.from || !evt.to) return;
    const fromId = _agentNameToMapId(evt.from);
    const toId = _agentNameToMapId(evt.to);
    if (!fromId || !toId) return;
    // Only animate routes that originate from secretary (user-triggered)
    // Background scheduler events (monitor/metrics/health) are shown in activity feed only
    const SCHEDULER_SOURCES = new Set(['scheduler','metrics','monitor','health','logkeeper','log_keeper','backup','session_scheduler']);
    const fromLower = (evt.from || '').toLowerCase();
    if (SCHEDULER_SOURCES.has(fromLower)) return;
    notifyMapRoute(fromId, toId, evt.msg || evt.message || '');
  }

  function _handleSecSSEEvent(evt) {
    if (!evt || evt.type !== 'route') return;
    _handleOfficeMapEvent(evt);
    // highlight pill of routed agent
    const toId = _agentNameToMapId(evt.to || '');
    if (toId) {
      document.querySelectorAll('.agent-pill').forEach((p) => {
        const pid = _agentNameToMapId(p.dataset.agentId || '');
        if (pid === toId) {
          p.classList.remove('agent-pill--idle', 'agent-pill--offline');
          p.classList.add('agent-pill--active');
          setTimeout(() => {
            p.classList.remove('agent-pill--active');
            p.classList.add('agent-pill--idle');
          }, 5000);
        }
      });
    }
  }
  window._handleSecSSEEvent = _handleSecSSEEvent;

  async function syncMapAgentStatus() {
    try {
      const r = await fetch('/workspace/office/activity');
      if (!r.ok) return;
      const data = await r.json();
      const activeSet = new Set();
      (data.items || []).forEach((e) => {
        if (e.mins_ago <= 5 && e.success) {
          const id = _agentNameToMapId(e.agent);
          if (id) activeSet.add(id);
        }
      });

      document.querySelectorAll('.agent-pill').forEach((p) => {
        const mapId = _agentNameToMapId(p.dataset.agentId || '');
        if (mapId && activeSet.has(mapId)) {
          p.className = 'agent-pill agent-pill--active';
        }
      });

      const w = _mapFrame();
      if (w && w.setAgentStatus) {
        activeSet.forEach((id) => w.setAgentStatus(id, 'active'));
      }
    } catch (err) {
      console.debug('map agent sync failed', err);
    }
  }

  function stopOfficeMapSync() {
    if (state.officeMapSyncTimer) {
      clearInterval(state.officeMapSyncTimer);
      state.officeMapSyncTimer = null;
    }
  }

  function startOfficeMapSync() {
    stopOfficeMapSync();
    syncMapAgentStatus();
    state.officeMapSyncTimer = setInterval(syncMapAgentStatus, 30000);
    const frame = document.getElementById('office-map-frame');
    if (frame && !frame.dataset.mapBridgeBound) {
      frame.dataset.mapBridgeBound = '1';
      frame.addEventListener('load', () => setTimeout(syncMapAgentStatus, 500));
    }
  }

  function stopOfficeEventStream() {
    if (state.officeEventReconnectTimer) {
      clearTimeout(state.officeEventReconnectTimer);
      state.officeEventReconnectTimer = null;
    }
    if (state.officeEventSource) {
      state.officeEventSource.close();
      state.officeEventSource = null;
    }
  }

  function startOfficeEventStream() {
    if (!document.getElementById('office-activity-feed')) return;
    stopOfficeEventStream();

    const es = new EventSource('/workspace/office/stream');
    state.officeEventSource = es;

    es.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data);
        const fromA = evt.from || '';
        const toA = evt.to || '';
        const msg = evt.msg || '';
        const type = evt.type || 'route';

        if (type === 'route') {
          _showBuildingBubble(fromA, `→ ${_agentShortName(toA)}`, 'route');
          const taskMsg = msg.replace(/^ส่งงาน:\s*/u, '').trim() || msg;
          setTimeout(() => _showBuildingBubble(toA, taskMsg, 'route'), 300);
        } else if (type === 'complete') {
          _showBuildingBubble(fromA, '✓ done', 'complete');
        }

        drawOfficeConnection(fromA, toA, type);
        _updateActivityFeedItem(fromA, toA, msg, type);
        _handleOfficeMapEvent(evt);
      } catch (err) {
        console.warn('office event parse failed', err);
      }
    };

    es.onerror = () => {
      stopOfficeEventStream();
      state.officeEventReconnectTimer = setTimeout(() => {
        const panel = document.getElementById('panel-office');
        if (panel && panel.classList.contains('active-panel')) {
          startOfficeEventStream();
        }
      }, 3000);
    };
  }

  function initOfficeRightPanel() {
    loadOfficeActivity();
    stopOfficeActivityRefresh();
    state.officeActivityTimer = setInterval(loadOfficeActivity, 15000);
    loadSecretaryHistory();
    startOfficeEventStream();
    startOfficeMapSync();
    startPBClock();
  }

  function openOfficePixelDesk(el) {
    const agentName = el && el.getAttribute ? (el.getAttribute('data-agent-name') || '') : '';
    if (agentName === 'SecretaryAgent') return focusOfficeSecretary(el);
    return openOfficeAgentChat(el);
  }

  function _finishSecretaryAiBubble(textEl, full) {
    if (!textEl) return;
    textEl.classList.remove('streaming-live', 'plain-text');
    if (typeof window.renderMarkdownInto === 'function') {
      window.renderMarkdownInto(textEl, full);
      if (typeof window.bindCodeCopyButtons === 'function') {
        window.bindCodeCopyButtons(textEl);
      }
      return;
    }
    textEl.classList.add('markdown-body');
    textEl.innerHTML = String(full || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(
        /(https?:\/\/[^\s<]+)/g,
        '<a href="$1" target="_blank" rel="noopener">$1</a>'
      )
      .replace(/\n/g, '<br>');
  }

  function appendSecretaryUserBubble(text) {
    const container = getOfficeSecMessages();
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'office-sec-msg office-sec-msg--user';
    div.innerHTML = `<div class="user-bubble-text">${escapeHtml(text)}</div>`;
    container.appendChild(div);
  }

  function appendSecretaryAiBubble(textElId, content, streaming) {
    const container = getOfficeSecMessages();
    if (!container) return null;
    const div = document.createElement('div');
    div.className = 'office-sec-msg office-sec-msg--ai';
    const streamingClass = streaming ? ' plain-text streaming-live' : '';
    div.innerHTML = `<span class="office-sec-avatar" aria-hidden="true">👩‍💼</span>
      <div class="ai-bubble-wrap">
        <div class="msg-text markdown-body${streamingClass}" id="${textElId}">${streaming ? '...' : ''}</div>
      </div>`;
    container.appendChild(div);
    const textEl = document.getElementById(textElId);
    if (textEl && content && !streaming) {
      _finishSecretaryAiBubble(textEl, content);
    }
    return textEl;
  }

  async function loadSecretaryHistory() {
    const container = getOfficeSecMessages();
    if (!container || state.secretaryHistoryLoaded) return;
    try {
      const data = await api('/workspace/secretary/history');
      const messages = data.messages || [];
      if (!messages.length) {
        updateSecretaryWelcome();
        return;
      }
      state.secretaryHistoryLoaded = true;
      container.querySelectorAll('.office-sec-msg').forEach((el) => el.remove());
      for (const msg of messages) {
        if (msg.role === 'user') {
          appendSecretaryUserBubble(msg.content || '');
        } else {
          const id = 'sec-h-' + Math.random().toString(36).slice(2);
          appendSecretaryAiBubble(id, msg.content || '', false);
        }
      }
      updateSecretaryWelcome();
      scrollSecretaryToBottom();
    } catch (error) {
      console.warn('secretary history load failed', error);
      updateSecretaryWelcome();
    }
  }

  async function sendOfficeSecretary() {
    const input = document.getElementById('office-sec-input');
    const sendBtn = document.querySelector('.office-sec-send-btn');
    if (!input) return;
    const msg = (input.value || '').trim();
    if (!msg) return;

    input.value = '';
    input.style.height = 'auto';
    if (sendBtn) sendBtn.disabled = true;

    appendSecretaryUserBubble(msg);
    updateSecretaryWelcome();

    const aiId = 'sec-' + Date.now();
    const textEl = appendSecretaryAiBubble(aiId + '-text', '', true);
    scrollSecretaryToBottom();
    let accumulated = '';

    try {
      const response = await fetch('/workspace/secretary/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ message: msg }),
      });
      if (!response.ok) {
        const errBody = await response.text().catch(() => '');
        throw new Error(errBody || `Request failed (${response.status})`);
      }
      if (!response.body) throw new Error('Streaming not supported');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let sseBuf = '';

      const handlePayload = (payload) => {
        if (!payload || !payload.type) return;
        if (window._handleSecSSEEvent) window._handleSecSSEEvent(payload);
        if (payload.type === 'token' && payload.text) {
          accumulated += payload.text;
          if (textEl) textEl.textContent = accumulated;
          scrollSecretaryToBottom();
        }
        if (payload.type === 'error') {
          throw new Error(payload.text || payload.message || 'Stream failed');
        }
      };

      const parseSseChunk = (chunk) => {
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const jsonText = line.replace(/^data:\s*/, '').trim();
          if (!jsonText) continue;
          try {
            handlePayload(JSON.parse(jsonText));
          } catch (err) {
            if (err instanceof SyntaxError) continue;
            throw err;
          }
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        sseBuf += decoder.decode(value, { stream: true });
        const parts = sseBuf.split('\n\n');
        sseBuf = parts.pop() || '';
        parts.forEach(parseSseChunk);
      }
      if (sseBuf.trim()) parseSseChunk(sseBuf);

      const finalText = accumulated.trim() || 'เอรับทราบแล้วค่ะ';
      _finishSecretaryAiBubble(textEl, finalText);
      state.secretaryHistoryLoaded = true;
      setTimeout(loadOfficeActivity, 1000);
    } catch (error) {
      if (textEl) {
        textEl.classList.remove('streaming-live');
        textEl.textContent = '⚠️ ' + (error.message || 'เกิดข้อผิดพลาด');
      }
    } finally {
      if (sendBtn) sendBtn.disabled = false;
      updateSecretaryWelcome();
      scrollSecretaryToBottom();
    }
  }

  const sendToSecretary = sendOfficeSecretary;

  function startThinkingStatus(thinkingId) {
    const steps = [
      'กำลังส่งคำขอ...',
      'กำลังรอโมเดลตอบกลับ...',
      'กำลังประมวลผลคำตอบ...',
      'ใกล้เสร็จแล้ว...'
    ];
    let idx = 0;
    const timer = setInterval(() => {
      const el = document.querySelector(`#${thinkingId} .thinking-status`);
      if (!el) return;
      idx = (idx + 1) % steps.length;
      el.textContent = steps[idx];
    }, 1800);
    return () => clearInterval(timer);
  }

  async function api(url, options={}) {
    const response = await fetch(url, Object.assign({
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin'
    }, options));

    if (response.status === 307 || response.redirected) {
      window.location.href = '/admin/otp';
      throw new Error('Session expired');
    }

    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const data = await response.json();
        detail = data.detail || detail;
      } catch (error) {}
      throw new Error(detail);
    }

    const contentType = response.headers.get('content-type') || '';
    return contentType.includes('application/json') ? response.json() : response.text();
  }

  async function loadActiveModelBadge() {
    if (!activeModelBadge) return;
    try {
      const data = await api('/admin/api/status');
      activeModelBadge.textContent = data.active_model_label || 'Auto / Active';
    } catch (error) {
      activeModelBadge.textContent = 'Auto / Active';
    }
  }

  function updateChatWelcome() {
    const welcome = document.getElementById('chat-welcome');
    const messagesWrap = document.getElementById('chat-messages');
    const panel = document.getElementById('panel-chat');
    if (!welcome || !messagesWrap || !chatMessages) return;
    const hasMessages = Boolean(chatMessages.querySelector('.msg-row'));
    welcome.classList.toggle('hidden', hasMessages);
    messagesWrap.classList.toggle('hidden', !hasMessages);
    if (panel) {
      panel.classList.toggle('ws-chat-empty', !hasMessages);
      panel.classList.toggle('ws-has-messages', hasMessages);
    }
  }

  function showPanel(name, options = {}) {
    document.querySelectorAll('.panel').forEach((panel) => {
      panel.classList.remove('active-panel');
      panel.style.display = 'none';
    });
    const target = document.getElementById('panel-' + name);
    if (target) {
      target.classList.add('active-panel');
      target.style.display = 'flex';
    }

    document.querySelectorAll('#workspace-tool-nav .tool-link').forEach((item) => {
      item.classList.toggle('active', item.dataset.panel === name);
    });

    const url = new URL(window.location.href);
    url.searchParams.set('tool', name);
    window.history.replaceState({}, '', url.toString());

    if (name === 'chat' && !options.skipHistoryLoad) loadChatHistory();
    if (name === 'notes') loadNotes();
    if (name === 'tasks') loadTasks();
    if (name === 'standup') {
      loadStandupProjects();
      generateStandup();
    }
    if (name === 'news') loadNews();
    if (name === 'memory') loadMemory();
    if (name === 'files') loadFiles();
    if (name === 'system') loadSystem();
    if (name === 'benchmark') loadBenchmark();
    if (name === 'autopost') loadAutopost();
    if (name === 'code' && typeof initCodeAssistantPanel === 'function') initCodeAssistantPanel();
    if (name === 'office') {
      initOfficeRightPanel();
      document.getElementById('office-sec-input')?.focus();
    } else {
      stopOfficeActivityRefresh();
      stopOfficeEventStream();
      stopOfficeMapSync();
      stopPBClock();
    }
  }

  function openOfficeAgentChat(el) {
    const cmd = el && el.getAttribute ? (el.getAttribute('data-chat-cmd') || '') : '';
    if (cmd) sessionStorage.setItem('ws_office_prefill', cmd);
    window.location.href = '/workspace?tool=chat';
    return false;
  }

  function applyOfficeChatPrefill() {
    const prefill = sessionStorage.getItem('ws_office_prefill');
    if (!prefill || !chatInput) return;
    chatInput.value = prefill;
    chatInput.dispatchEvent(new Event('input'));
    chatInput.focus();
    sessionStorage.removeItem('ws_office_prefill');
  }

  function startOfficeAutoRefresh() {
    stopOfficeAutoRefresh();
  }

  function stopOfficeAutoRefresh() {
    if (window.__officeRefreshTimer) {
      clearInterval(window.__officeRefreshTimer);
      window.__officeRefreshTimer = null;
    }
  }

  function newChat() {
    chatMessages.innerHTML = '';
    state.currentProject = null;
    state.projectName = 'All Chats';
    window._currentProject = null;
    highlightProjectLink();
    updateChatWelcome();
    showPanel('chat');
  }

  function setPendingImage(file) {
    if (!file || !String(file.type || '').startsWith('image/')) {
      showToast('รองรับเฉพาะไฟล์รูปภาพ');
      return;
    }
    state.pendingImageFile = file;
    const reader = new FileReader();
    reader.onload = (e) => {
      state.pendingPreviewUrl = e.target.result || '';
      const previewImg = document.getElementById('preview-img');
      const imagePreview = document.getElementById('image-preview');
      if (previewImg) previewImg.src = state.pendingPreviewUrl;
      imagePreview?.classList.remove('hidden');
    };
    reader.readAsDataURL(file);
  }

  function clearPendingImage() {
    state.pendingImageFile = null;
    state.pendingPreviewUrl = '';
    const imageUpload = document.getElementById('image-upload');
    const imagePreview = document.getElementById('image-preview');
    const previewImg = document.getElementById('preview-img');
    if (imageUpload) imageUpload.value = '';
    imagePreview?.classList.add('hidden');
    if (previewImg) previewImg.src = '';
  }

  function extractClipboardImageFile(clipboardData) {
    if (!clipboardData) return null;
    const items = clipboardData.items;
    if (items) {
      for (let i = 0; i < items.length; i += 1) {
        const item = items[i];
        if (item.kind === 'file' && String(item.type || '').startsWith('image/')) {
          return item.getAsFile();
        }
      }
    }
    const files = clipboardData.files;
    if (files && files.length) {
      for (let i = 0; i < files.length; i += 1) {
        if (String(files[i].type || '').startsWith('image/')) return files[i];
      }
    }
    return null;
  }

  function attachClipboardImagePaste(el) {
    if (!el) return;
    el.addEventListener('paste', (event) => {
      const file = extractClipboardImageFile(event.clipboardData);
      if (!file) return;
      event.preventDefault();
      setPendingImage(file);
    });
  }

  async function sendMessage() {
    if (!chatInput) return;
    const msg = chatInput.value.trim();
    const imageFile = state.pendingImageFile;
    if ((!msg && !imageFile) || state.streaming) return;

    const previewUrl = state.pendingPreviewUrl || '';
    chatInput.value = '';
    chatInput.style.height = 'auto';
    slashMenu.style.display = 'none';
    appendUserBubble(msg || '📷 Screenshot', '', previewUrl);
    clearPendingImage();

    const thinkingId = 'thinking-' + Date.now();
    appendThinkingBubble(thinkingId);
    const stopThinkingStatus = startThinkingStatus(thinkingId);

    state.streaming = true;
    setSendButtonState(true);

    if (imageFile) {
      try {
        const formData = new FormData();
        formData.append('message', msg);
        if (window._currentProject) formData.append('project_id', String(window._currentProject));
        formData.append('image', imageFile, imageFile.name || 'screenshot.png');
        const response = await fetch('/workspace/chat/vision', {
          method: 'POST',
          body: formData,
          credentials: 'same-origin',
        });
        const data = await response.json().catch(() => ({}));
        document.getElementById(thinkingId)?.remove();
        if (!response.ok) {
          throw new Error(data.detail || data.error || `Request failed (${response.status})`);
        }
        const reply = String(data.reply || '').trim() || 'ยังไม่มีคำตอบตอนนี้';
        const aiBubble = appendAiBubble('', 'Ener-AI · Vision');
        renderAiMessageContent(aiBubble.querySelector('.msg-text'), reply);
        loadProjects().catch(() => {});
        scrollToBottom();
      } catch (error) {
        document.getElementById(thinkingId)?.remove();
        appendAiBubble('ไม่สามารถวิเคราะห์รูปได้ กรุณาลองใหม่', 'Ener-AI');
        showToast(error.message || 'Vision request failed');
      } finally {
        stopThinkingStatus();
        state.streaming = false;
        setSendButtonState(false);
        chatInput.focus();
      }
      return;
    }

    const model = (document.getElementById('model-select') || {}).value || 'deepseek/deepseek-v4-flash';
    try {
      await streamWorkspaceChat(msg, model, thinkingId);
    } catch (error) {
      document.getElementById(thinkingId)?.remove();
      const errMsg = error.message || 'OpenRouter request failed';
      const failBubble = appendAiBubble(errMsg, 'Ener-AI');
      const textEl = failBubble?.closest('.ai-bubble-wrap')?.querySelector('.msg-text')
        || failBubble?.querySelector('.msg-text');
      renderAiMessageContent(textEl, errMsg);
      showToast(errMsg);
    } finally {
      stopThinkingStatus();
      state.streaming = false;
      setSendButtonState(false);
      chatInput.focus();
    }
    return;
  }

  async function loadChatHistory() {
    const dateFilter = getWorkspaceDateFilter();
    const showAll = dateFilter === 'all';
    const data = await api(`/workspace/chat/history${workspaceHistoryQuery()}`);
    chatMessages.innerHTML = '';
    const messages = data.messages || [];
    if (!messages.length) {
      chatMessages.innerHTML = '';
      updateChatWelcome();
      return;
    }
    if (showAll) {
      const title = document.createElement('div');
      title.className = 'ws-chat-view-title';
      title.textContent = 'ประวัติทั้งหมด';
      chatMessages.appendChild(title);
    } else if (dateFilter) {
      const title = document.createElement('div');
      title.className = 'ws-chat-view-title';
      title.textContent = 'chat ' + formatDayLabel(dateFilter);
      chatMessages.appendChild(title);
    }
    let lastDay = '';
    messages.forEach((msg) => {
      if (showAll) {
        const day = dayKeyFromCreatedAt(msg.created_at);
        if (day && day !== lastDay) {
          ensureDayMarker(day);
          lastDay = day;
        }
      }
      chatMessages.appendChild(renderChatMessage(msg));
    });
    updateChatWelcome();
    scrollToBottom();
  }

  function highlightProjectLink() {
    document.querySelectorAll('#project-nav .project-link').forEach((link) => {
      const projectId = link.dataset.projectId ? Number(link.dataset.projectId) : null;
      const active = projectId === state.currentProject;
      link.classList.toggle('active', active);
      link.classList.toggle('active-project', active);
    });
  }

  async function loadProjects() {
    const data = await api('/workspace/projects');
    const projects = data.projects || [];
    const items = [
      {
        id: null,
        name: 'All Chats',
        count: data.total_messages || 0,
        lastActive: ''
      },
      ...projects.map((project) => ({
        id: project.id,
        name: project.name,
        count: project.message_count || 0,
        lastActive: project.last_active || ''
      }))
    ];

    projectNav.innerHTML = items.map((project) => `
      <a href="/workspace?tool=chat" class="ws-project-link project-link" data-project-id="${project.id ?? ''}">
        <div class="flex-1 text-left">
          <p class="font-medium">${escapeHtml(project.name)}</p>
          <p class="text-xs" style="color: var(--muted-foreground);">${project.count} messages${project.lastActive ? ' • ' + escapeHtml(project.lastActive) : ''}</p>
        </div>
      </a>
    `).join('');

    projectNav.querySelectorAll('.project-link').forEach((link) => {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        const rawId = link.dataset.projectId;
        const projectId = rawId ? Number(rawId) : null;
        const nameEl = link.querySelector('.font-medium');
        const projectName = nameEl?.textContent?.trim() || 'All Chats';
        selectProject(projectId, projectName);
      });
    });

    highlightProjectLink();
  }

  function selectProject(id, name) {
    state.currentProject = id;
    state.projectName = name || 'All Chats';
    window._currentProject = id;
    highlightProjectLink();
    chatMessages.innerHTML = '';
    loadChatHistory();
    showPanel('chat');
  }

  function showNewProjectModal() {
    document.getElementById('modal-overlay').style.display = 'flex';
    document.getElementById('proj-name-input').focus();
  }

  function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none';
    document.getElementById('proj-name-input').value = '';
  }

  async function createProject() {
    const input = document.getElementById('proj-name-input');
    const name = input.value.trim();
    if (!name) {
      showToast('Project name required');
      return;
    }

    try {
      await api('/workspace/projects/create', {
        method: 'POST',
        body: JSON.stringify({name})
      });
      await loadProjects();
      closeModal();
      showToast('Project created');
    } catch (error) {
      showToast(error.message || 'Create project failed');
    }
  }

  async function loadNotes() {
    const data = await api('/workspace/notes');
    const notes = data.notes || [];
    const grouped = {};
    notes.forEach((note) => {
      const category = note.category || 'note';
      if (!grouped[category]) grouped[category] = [];
      grouped[category].push(note);
    });

    const list = document.getElementById('notes-list');
    if (!notes.length) {
      list.innerHTML = '<div class="empty-state">No notes yet.</div>';
      return;
    }

    list.innerHTML = Object.entries(grouped).map(([category, items]) => `
      <div class="notes-group">
        <h3>${escapeHtml(category)}</h3>
        ${items.map((note) => `
          <details class="note-card">
            <summary>${escapeHtml(note.ai_summary || (note.content || '').slice(0, 120))}</summary>
            <div class="note-meta">${escapeHtml(note.created_at || '')}</div>
            <div style="margin-top:10px;">${renderMarkdown(note.content || '')}</div>
          </details>
        `).join('')}
      </div>
    `).join('');
  }

  async function saveNote() {
    const input = document.getElementById('note-input');
    const value = input.value.trim();
    if (!value) return;

    try {
      await api('/workspace/notes/save', {
        method: 'POST',
        body: JSON.stringify({text: value})
      });
      input.value = '';
      await loadNotes();
      showToast('Note saved');
    } catch (error) {
      showToast(error.message || 'Save note failed');
    }
  }

  async function loadTasks() {
    const data = await api('/workspace/tasks');
    const tasks = data.tasks || [];
    const grouped = {open: [], in_progress: [], done: []};
    tasks.forEach((task) => {
      const status = task.status || 'open';
      if (!grouped[status]) grouped[status] = [];
      grouped[status].push(task);
    });

    const labels = {
      open: 'Open',
      in_progress: 'In Progress',
      done: 'Done'
    };

    const list = document.getElementById('tasks-list');
    list.innerHTML = Object.entries(grouped).map(([status, items]) => `
      <div class="task-group">
        <h3>${labels[status] || status}</h3>
        <div class="surface">
          ${items.length ? items.map((task) => `
            <label class="task-item">
              <input type="checkbox" ${task.status === 'done' ? 'checked' : ''} data-task-id="${task.id}">
              <div>
                <div>${escapeHtml(task.title || '')}</div>
                <div class="task-meta">${escapeHtml(task.deadline_hint || '')}</div>
                <div class="priority-badge priority-${escapeHtml(task.priority || 'medium')}">${escapeHtml(task.priority_badge || '')} ${escapeHtml(task.priority || 'medium')}</div>
              </div>
            </label>
          `).join('') : '<div class="empty-state">No tasks in this group.</div>'}
        </div>
      </div>
    `).join('');

    list.querySelectorAll('input[type="checkbox"][data-task-id]').forEach((checkbox) => {
      checkbox.addEventListener('change', async () => {
        const taskId = checkbox.dataset.taskId;
        try {
          await api(`/workspace/tasks/${taskId}/done`, {method: 'POST'});
          await loadTasks();
        } catch (error) {
          showToast(error.message || 'Update task failed');
        }
      });
    });
  }

  async function createTask() {
    const input = document.getElementById('task-input');
    const priority = document.getElementById('task-priority');
    const title = input.value.trim();
    if (!title) return;

    try {
      await api('/workspace/tasks/create', {
        method: 'POST',
        body: JSON.stringify({
          title,
          priority: priority.value || 'medium'
        })
      });
      input.value = '';
      priority.value = 'medium';
      await loadTasks();
      showToast('Task created');
    } catch (error) {
      showToast(error.message || 'Create task failed');
    }
  }

  async function generateStandup() {
    const preview = document.getElementById('standup-preview');
    if (!preview) return;
    try {
      const data = await api('/workspace/standup/preview');
      preview.textContent = data.report || '-';
      showToast('Report generated! ✅');
    } catch (error) {
      showToast(error.message || 'Generate standup failed');
    }
  }

  async function copyStandupReport() {
    const preview = document.getElementById('standup-preview');
    const text = (preview?.textContent || '').trim();
    if (!text) {
      showToast('ยังไม่มี report ให้ copy');
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      showToast('Copied ✅');
    } catch (error) {
      showToast('Copy failed');
    }
  }

  async function loadStandupProjects() {
    const data = await api('/workspace/standup/projects');
    const projects = data.projects || data || [];
    const el = document.getElementById('standup-projects');
    el.innerHTML = '<h3 style="margin:0 0 12px">📊 Projects</h3>' + projects.map((p) => `
      <div class="standup-project-card">
        <div class="sp-name">${escapeHtml(p.name || '')}</div>
        <div class="sp-row">
          <label>% เสร็จ</label>
          <input type="number" value="${Number(p.percent_complete || 0)}" min="0" max="100"
            onchange="updateProject(${Number(p.id)}, 'percent_complete', this.value)">
        </div>
        <div class="sp-row">
          <label>Status</label>
          <input type="text" value="${escapeHtml(p.current_status || '')}"
            onblur="updateProject(${Number(p.id)}, 'current_status', this.value)">
        </div>
        <div class="sp-row">
          <label>Due</label>
          <input type="text" value="${escapeHtml(p.due_date || '')}"
            onblur="updateProject(${Number(p.id)}, 'due_date', this.value)">
        </div>
        <div class="sp-row">
          <label>วันนี้ทำ</label>
          <textarea rows="2"
            onblur="updateProject(${Number(p.id)}, 'today_tasks', this.value)">${escapeHtml(p.today_tasks || '')}</textarea>
        </div>
      </div>
    `).join('');
  }

  async function updateProject(id, field, value) {
    try {
      await api('/workspace/standup/projects/' + id + '/update', {
        method: 'POST',
        body: JSON.stringify({field, value})
      });
      showToast('Saved ✅');
    } catch (error) {
      showToast(error.message || 'Save failed');
    }
  }

  async function runBrainstorm() {
    const input = document.getElementById('brainstorm-input');
    const topic = input.value.trim();
    if (!topic) return;

    const result = document.getElementById('brainstorm-result');
    result.innerHTML = '<div class="surface">🧠 AI Council กำลังประชุม — 4 โมเดลถกกัน 2 รอบ + สังเคราะห์เป็น spec (อาจใช้ 1-2 นาที)…</div>';

    try {
      const data = await api('/workspace/brainstorm', {
        method: 'POST',
        body: JSON.stringify({topic})
      });

      const rounds = data.rounds || [];
      const spec = data.spec || {};
      const shortModel = m => String(m || '').split('/').pop();
      let html = '';
      if (data.research) {
        html += `<div class="surface" style="margin-bottom:12px"><h4>🔎 Research</h4><div>${renderMarkdown(data.research)}</div></div>`;
      }
      rounds.forEach(rd => {
        html += `<h4 style="margin:14px 0 6px">รอบ ${rd.round}</h4><div class="brain-grid">`;
        (rd.seats || []).forEach(s => {
          html += `<div class="brain-card"><h4>${s.emoji || ''} ${escapeHtml(s.name || '')} <span style="font-size:10px;color:#888;font-weight:400">${escapeHtml(shortModel(s.model))}</span></h4><div>${renderMarkdown(s.text || '')}</div></div>`;
        });
        html += `</div>`;
      });
      const feats = (spec.features || []).map(f => `<li>${escapeHtml(f)}</li>`).join('');
      const cuts = (spec.cut || []).map(f => escapeHtml(f)).join(', ');
      const conf = spec.confidence || '';
      const confColor = conf === 'go' ? '#10b981' : conf === 'risky' ? '#ef4444' : '#f59e0b';
      if (spec._fallback) {
        // JSON synth failed → backend returned a plain-text Thai summary. Show it readable.
        html += `<div class="verdict-card" style="margin-top:14px">
          <h4>📋 สรุปวง Council</h4>
          <div style="color:#cbd5e1;margin:4px 0">${renderMarkdown(spec.one_liner || '')}</div>
          <button id="council-build-btn" class="primary-btn" style="margin-top:12px">🚀 สร้างเป็น project</button>
        </div>`;
      } else {
        const meta = [];
        if (spec.users) meta.push(`<div class="task-meta">👥 ${escapeHtml(spec.users)}</div>`);
        if (spec.tech || spec.ui) meta.push(`<div class="task-meta" style="margin-top:6px">🧱 ${escapeHtml(spec.tech || '-')} · 🎨 ${escapeHtml(spec.ui || '-')}</div>`);
        if (cuts) meta.push(`<div class="task-meta">✂️ v1 ตัด: ${cuts}</div>`);
        html += `<div class="verdict-card" style="margin-top:14px">
          <h4>🎯 Project Spec — <span style="color:${confColor}">${escapeHtml(conf || '-')}</span></h4>
          <div style="font-size:16px;font-weight:700">${escapeHtml(spec.name || '(ไม่มีชื่อ)')}</div>
          <div style="color:#cbd5e1;margin:4px 0">${renderMarkdown(spec.one_liner || '')}</div>
          ${meta.join('')}
          ${feats ? `<div style="margin-top:8px">ฟีเจอร์ MVP:<ul style="margin:4px 0 0 18px">${feats}</ul></div>` : ''}
          <button id="council-build-btn" class="primary-btn" style="margin-top:12px">🚀 สร้างเป็น project</button>
        </div>`;
      }
      result.innerHTML = html;

      const btn = document.getElementById('council-build-btn');
      if (btn) btn.addEventListener('click', () => {
        const q = 'สร้างเว็บแอปใหม่ตาม spec นี้ ให้ครบ รันได้จริง หน้าตาระดับพรีเมียม:\n'
          + 'ชื่อ: ' + (spec.name || topic) + '\n' + (spec.one_liner || '') + '\n'
          + 'ผู้ใช้: ' + (spec.users || '') + '\n'
          + 'ฟีเจอร์ MVP:\n' + (spec.features || []).map(f => '- ' + f).join('\n') + '\n'
          + 'Tech: ' + (spec.tech || 'FastAPI + HTML/Tailwind') + '\nทิศทาง UI: ' + (spec.ui || '');
        try { localStorage.setItem('ener_council_build', JSON.stringify({ name: spec.name || topic, question: q })); } catch (e) {}
        window.location.href = '/workspace?tool=code';
      });
    } catch (error) {
      result.innerHTML = '<div class="surface">AI Council failed.</div>';
      showToast((error && error.message) || 'Council failed');
    }
  }

  async function loadNews() {
    const data = await api('/workspace/news');
    const items = data.news || [];
    const list = document.getElementById('news-list');
    if (!items.length) {
      list.innerHTML = '<div class="empty-state">No news loaded yet.</div>';
      return;
    }
    list.innerHTML = items.map((item) => `
      <div class="news-card">
        <div><strong>${escapeHtml(item.title || '')}</strong></div>
        <div class="news-meta">${escapeHtml(item.source || '')} ${item.fetched_at ? '• ' + escapeHtml(item.fetched_at) : ''}</div>
        <div style="margin-top:10px;">${escapeHtml(item.summary || '')}</div>
        <div class="row-actions">
          ${item.url ? `<a class="secondary-btn" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Open</a>` : ''}
          <button class="secondary-btn vdo-btn" data-title="${escapeHtml(item.title || '')}" data-summary="${escapeHtml(item.summary || '')}">🎬 ทำคลิป</button>
        </div>
      </div>
    `).join('');
    list.querySelectorAll('.vdo-btn').forEach((b) => {
      b.addEventListener('click', () => makeVdo(b.dataset.title || '', b.dataset.summary || '', b));
    });
  }

  async function makeVdo(title, summary, btn) {
    if (!title) return;
    if (btn) { btn.disabled = true; btn.textContent = '⏳ กำลังทำคลิป…'; }
    showToast('🎬 กำลังทำคลิป (บท→เสียง→วิดีโอ) ~30-60 วิ…');
    try {
      const data = await api('/workspace/vdo/make', { method: 'POST', body: JSON.stringify({ title, summary }) });
      if (data.ok && data.telegram) showToast('✅ ส่งคลิปเข้า Telegram แล้ว (' + (data.duration || '?') + ' วิ)');
      else if (data.ok) showToast('⚠️ render ได้ แต่ส่ง Telegram ไม่ได้: ' + (data.error || ''));
      else showToast('❌ ' + (data.error || 'ทำคลิปไม่สำเร็จ'));
    } catch (e) {
      showToast((e && e.message) || 'ทำคลิปไม่สำเร็จ');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '🎬 ทำคลิป'; }
    }
  }

  async function makeMysteryVdo() {
    const topic = (prompt('หัวข้อสายมู/ลึกลับ (เว้นว่าง = ให้ AI เลือกเอง)\nเช่น: ตะกรุดมหาอุด, Omamori ญี่ปุ่น, UFO Roswell, ตำนานกุมารทอง', '') || '').trim();
    showToast('🔮 กำลังทำคลิปสายมู (AI เขียนบท→เสียง→วิดีโอ) ~30-60 วิ…');
    try {
      const data = await api('/workspace/vdo/mystery', { method: 'POST', body: JSON.stringify({ topic }) });
      if (!data.ok) { showToast('❌ ' + (data.error || 'ทำคลิปไม่สำเร็จ')); return; }
      showToast('✅ คลิปสายมูเข้า Telegram แล้ว: ' + (data.title || '') + ' (' + (data.duration || '?') + ' วิ)');
      // offer to publish to the connected FB page (after reviewing in Telegram)
      if (data.video_url) {
        const fn = String(data.video_url).split('/').pop();
        const yes = confirm('คลิปอยู่ใน Telegram แล้ว 👇\n"' + (data.title || '') + '"\n\nโพสต์ขึ้นเพจ FB (Ener Scan) เลยไหม?\n(OK = โพสต์เลย / Cancel = ไม่โพสต์)');
        if (yes) {
          showToast('📤 กำลังโพสต์ขึ้น FB…');
          try {
            const p = await api('/workspace/vdo/post', { method: 'POST', body: JSON.stringify({ filename: fn, caption: data.caption || '', when: 'now' }) });
            showToast(p.ok ? '✅ ' + (p.message || 'โพสต์ขึ้น FB แล้ว!') : '❌ ' + (p.message || p.error || 'โพสต์ไม่สำเร็จ'));
          } catch (e2) { showToast('❌ ' + ((e2 && e2.message) || 'โพสต์ไม่สำเร็จ')); }
        }
      }
    } catch (e) {
      showToast((e && e.message) || 'ทำคลิปไม่สำเร็จ');
    }
  }

  async function fetchNews() {
    try {
      showToast('Fetching latest news...');
      await api('/workspace/news/fetch', {method: 'POST'});
      await loadNews();
      showToast('News updated');
    } catch (error) {
      showToast(error.message || 'Fetch news failed');
    }
  }

  async function loadMemory() {
    const data = await api('/workspace/memory');
    const items = data.memories || [];
    const list = document.getElementById('memory-list');
    if (!items.length) {
      list.innerHTML = '<div class="empty-state">No long-term memories yet.</div>';
      return;
    }
    list.innerHTML = items.map((item) => `
      <div class="memory-card">
        <div>${renderMarkdown(item.content || '')}</div>
        <div class="memory-meta">${escapeHtml(item.memory_type || 'general')} ${item.created_at ? '• ' + escapeHtml(item.created_at) : ''}</div>
      </div>
    `).join('');
  }

  async function uploadFile(file) {
    const formData = new FormData();
    formData.append('file', file);
    await fetch('/workspace/files/upload', {
      method: 'POST',
      body: formData,
      credentials: 'same-origin'
    }).then((response) => {
      if (!response.ok) throw new Error('Upload failed');
      return response.json();
    });
  }

  async function summarizeFile(fileId) {
    try {
      await api(`/workspace/files/${fileId}/summarize`, {method: 'POST'});
      await loadFiles();
      showToast('Summary ready');
    } catch (error) {
      showToast(error.message || 'Summarize failed');
    }
  }

  async function askFile(fileId) {
    const question = window.prompt('Ask about this file');
    if (!question) return;
    try {
      const data = await api(`/workspace/files/${fileId}/ask`, {
        method: 'POST',
        body: JSON.stringify({question})
      });
      showPanel('chat');
      appendAiBubble(data.answer || '', 'Ener-AI • file answer');
      showToast('Answer added to chat');
    } catch (error) {
      showToast(error.message || 'Ask file failed');
    }
  }

  async function loadFiles() {
    const data = await api('/workspace/files');
    const files = data.files || [];
    const list = document.getElementById('files-list');
    if (!files.length) {
      list.innerHTML = '<div class="empty-state">No uploaded files yet.</div>';
      return;
    }
    list.innerHTML = files.map((file) => `
      <div class="file-card">
        <div><strong>${escapeHtml(file.filename || '')}</strong></div>
        <div class="file-meta">${escapeHtml(String(file.size_bytes || 0))} bytes ${file.created_at ? '• ' + escapeHtml(file.created_at) : ''}</div>
        ${file.summary ? `<div style="margin-top:10px;">${renderMarkdown(file.summary)}</div>` : ''}
        <div class="file-actions">
          <button class="file-action" onclick="summarizeFile(${file.id})">Summarize</button>
          <button class="secondary-btn" onclick="askFile(${file.id})">Ask</button>
        </div>
      </div>
    `).join('');
  }

  async function loadSystem() {
    const container = document.getElementById('system-content');
    if (!container) return;
    container.innerHTML = '<div class="empty-state">Loading system info...</div>';
    try {
      const data = await api('/workspace/system/info');
      const pipelineData = await api('/admin/pipeline-metrics');
      const stats = data.stats || {};
      const agents = data.agents || [];
      const scheduler = data.scheduler || [];
      const averages = pipelineData.averages || [];
      const recent = pipelineData.recent || [];
      const statsHtml = averages.length
        ? averages.map((item) => `
            <div class="sys-card">
              <div class="sys-label">🤖 ${escapeHtml(item.model_used || '-')}</div>
              <div class="sys-value">${Math.round(Number(item.avg_total || 0))}ms</div>
              <div style="font-size:12px;color:#666;margin-top:6px">
                Router: ${Math.round(Number(item.avg_router || 0))}ms |
                Reason: ${Math.round(Number(item.avg_reasoner || 0))}ms |
                Check: ${Math.round(Number(item.avg_checker || 0))}ms
              </div>
              <div style="font-size:11px;color:#888">${Number(item.count || 0).toLocaleString()} requests</div>
            </div>
          `).join('')
        : '<div class="empty-state">ยังไม่มี pipeline metrics ใน 24 ชั่วโมงล่าสุด</div>';
      const rows = recent.length
        ? recent.map((item) => {
            const totalMs = Number(item.total_ms || 0);
            const totalColor = totalMs > 3000 ? '#ef4444' : totalMs > 1500 ? '#f59e0b' : '#22c55e';
            const timeLabel = item.created_at ? escapeHtml(String(item.created_at).split(' ')[1] || String(item.created_at)) : '-';
            return `<tr style="border-bottom:1px solid #222;font-size:13px">
              <td style="padding:8px;color:#888">${timeLabel}</td>
              <td style="padding:8px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                ${escapeHtml(item.question_preview || '-')}
              </td>
              <td style="padding:8px">
                <span style="background:#2a2a2a;padding:2px 8px;border-radius:12px;font-size:11px">
                  ${escapeHtml(item.model_used || '-')}
                </span>
              </td>
              <td style="padding:8px;text-align:right;color:#888">${Number(item.router_ms || 0)}ms</td>
              <td style="padding:8px;text-align:right">${Number(item.reasoner_ms || 0)}ms</td>
              <td style="padding:8px;text-align:right;color:#888">${Number(item.checker_ms || 0)}ms</td>
              <td style="padding:8px;text-align:right;font-weight:600;color:${totalColor}">
                ${totalMs}ms
              </td>
              <td style="padding:8px;text-align:center">
                ${item.was_fixed ? '🔧' : '✅'}
              </td>
            </tr>`;
          }).join('')
        : '<tr><td colspan="8" class="empty-state" style="padding:12px 8px;">ยังไม่มี recent requests</td></tr>';
      container.innerHTML = `
        <div class="sys-grid">
          <div class="sys-card">
            <div class="sys-label">🤖 Active Model</div>
            <div class="sys-value">${escapeHtml(data.model || '-')}</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">📦 Agents</div>
            <div class="sys-value">${Number(data.agent_count || 0).toLocaleString()} ตัว</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">💬 Messages</div>
            <div class="sys-value">${Number(stats.messages || 0).toLocaleString()}</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">✅ Tasks (open)</div>
            <div class="sys-value">${Number(stats.open_tasks || 0).toLocaleString()} / ${Number(stats.tasks || 0).toLocaleString()}</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">🧠 Memories</div>
            <div class="sys-value">${Number(stats.memories || 0).toLocaleString()} + ${Number(stats.long_term_memories || 0).toLocaleString()} LT</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">📝 Notes</div>
            <div class="sys-value">${Number(stats.notes || 0).toLocaleString()}</div>
          </div>
        </div>
        <h3 style="margin:24px 0 12px">⏰ Scheduler</h3>
        <div class="sched-list">
          ${scheduler.map((item) => `
            <div class="sched-item">
              <span class="sched-time">${escapeHtml(item.time || '')}</span>
              <span class="sched-job">${escapeHtml(item.job || '')}</span>
            </div>
          `).join('')}
        </div>
        <h3 style="margin:24px 0 12px">⚡ Pipeline Response Times (24h)</h3>
        <div id="pipeline-stats">${statsHtml}</div>
        <h3 style="margin:24px 0 12px">📋 Recent Requests</h3>
        <table id="pipeline-table" style="width:100%;border-collapse:collapse">
          <thead>
            <tr style="font-size:11px;color:#888;text-transform:uppercase">
              <th style="padding:8px;text-align:left">Time</th>
              <th style="padding:8px;text-align:left">Question</th>
              <th style="padding:8px;text-align:left">Model</th>
              <th style="padding:8px;text-align:right">Router</th>
              <th style="padding:8px;text-align:right">Reasoner</th>
              <th style="padding:8px;text-align:right">Checker</th>
              <th style="padding:8px;text-align:right">Total</th>
              <th style="padding:8px;text-align:center">Fixed?</th>
            </tr>
          </thead>
          <tbody id="pipeline-tbody">${rows}</tbody>
        </table>
        <h3 style="margin:24px 0 12px">📦 Agents (${Number(data.agent_count || 0).toLocaleString()})</h3>
        <div class="agent-chips">
          ${agents.map((agent) => `<span class="agent-chip">${escapeHtml(agent || '')}</span>`).join('')}
        </div>
      `;
    } catch (error) {
      container.innerHTML = '<div class="empty-state">โหลด system info ไม่สำเร็จ</div>';
      showToast(error.message || 'Load system info failed');
    }
  }

  function dropZoneClick() {
    fileInput.click();
  }

  dropZone.addEventListener('click', dropZoneClick);
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    if (!file) return;
    try {
      await uploadFile(file);
      fileInput.value = '';
      await loadFiles();
      showToast('File uploaded');
    } catch (error) {
      showToast(error.message || 'Upload failed');
    }
  });

  dropZone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropZone.classList.add('dragover');
  });

  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
  });

  dropZone.addEventListener('drop', async (event) => {
    event.preventDefault();
    dropZone.classList.remove('dragover');
    const file = event.dataTransfer.files[0];
    if (!file) return;
    try {
      await uploadFile(file);
      await loadFiles();
      showToast('File uploaded');
    } catch (error) {
      showToast(error.message || 'Upload failed');
    }
  });

  if (chatInput) {
  const imageUpload = document.getElementById('image-upload');
  const clearImageBtn = document.getElementById('clear-image-btn');
  const composerWrap = document.getElementById('chat-input-wrap');

  if (imageUpload) {
    imageUpload.addEventListener('change', function() {
      if (this.files && this.files[0]) setPendingImage(this.files[0]);
    });
  }
  if (clearImageBtn) {
    clearImageBtn.addEventListener('click', clearPendingImage);
  }
  if (composerWrap) {
    composerWrap.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
    composerWrap.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) setPendingImage(file);
    });
  }

  attachClipboardImagePaste(chatInput);

  window.clearImage = clearPendingImage;
  window.previewImage = function(input) {
    if (input && input.files && input.files[0]) setPendingImage(input.files[0]);
  };

  const officeSecInput = document.getElementById('office-sec-input');
  if (officeSecInput) {
    officeSecInput.addEventListener('input', function() {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 80) + 'px';
    });
    officeSecInput.addEventListener('keydown', function(event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendOfficeSecretary();
      }
    });
  }

  chatInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 200) + 'px';
    updateSlashMenu(this.value);
  });

  chatInput.addEventListener('keydown', function(e) {
    const items = slashMenu.querySelectorAll('.slash-item');
    if (slashMenu.style.display !== 'none' && items.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        window._slashIndex = Math.min((window._slashIndex || 0) + 1, items.length - 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        window._slashIndex = Math.max((window._slashIndex || 0) - 1, 0);
      } else if (e.key === 'Tab' || (e.key === 'Enter' && slashMenu.style.display !== 'none' && this.value.startsWith('/'))) {
        e.preventDefault();
        const selected = items[window._slashIndex || 0];
        if (selected) selected.click();
        return;
      } else if (e.key === 'Escape') {
        slashMenu.style.display = 'none';
        return;
      }
      items.forEach((el, i) => el.classList.toggle('selected', i === (window._slashIndex || 0)));
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  }

  document.addEventListener('click', function(e) {
    if (!e.target.closest('#slash-menu') && !e.target.closest('#chat-input')) {
      slashMenu.style.display = 'none';
    }
  });

  document.getElementById('proj-name-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      createProject();
    }
  });

  // ===== Auto Post (pipeline) =====
  const AP_DAYS = ['จ', 'อ', 'พ', 'พฤ', 'ศ', 'ส', 'อา']; // 0=Mon..6=Sun
  const AP_PLATS = [
    { name: 'facebook', label: '📘 Facebook', def: '18:00' },
    { name: 'youtube', label: '▶️ YouTube', def: '19:00' },
    { name: 'tiktok', label: '🎵 TikTok', def: '20:00' },
  ];
  let _apSchedules = [];
  let _apPlatStatus = {};
  let _apStatusTimer = null;

  async function loadAutopost() {
    const schDiv = document.getElementById('autopost-schedules');
    if (schDiv) schDiv.innerHTML = '<div style="color:var(--muted-foreground);font-size:13px">กำลังโหลด…</div>';
    try {
      const data = await api('/workspace/autopost/data');
      _apSchedules = data.schedules || [];
      _apPlatStatus = data.platforms || {};
      renderApPlatforms();
      renderApDays();
      renderApSchedules(_apSchedules);
      renderApLog(data.log || []);
      renderApStatus(data.status);
    } catch (e) {
      if (schDiv) schDiv.innerHTML = '<div style="color:#f87171">โหลดไม่สำเร็จ: ' + escapeHtml(e.message) + '</div>';
    }
    loadAutopostClips();
    startApStatusPoll();
  }

  async function loadAutopostClips() {
    const div = document.getElementById('autopost-clips');
    if (!div) return;
    div.innerHTML = '<div style="color:var(--muted-foreground);font-size:13px">กำลังโหลด…</div>';
    try {
      const d = await api('/workspace/vdo/list');
      const clips = d.clips || [];
      if (!clips.length) { div.innerHTML = '<div style="color:var(--muted-foreground);font-size:13px">ยังไม่มีคลิป</div>'; return; }
      div.innerHTML = clips.map(c => {
        const mb = (c.size / 1048576).toFixed(1);
        const dt = new Date(c.mtime * 1000).toLocaleString('th-TH', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
        const nm = escapeHtml(c.name);
        return '<div data-clip="' + nm + '" style="background:#1f2430;border:1px solid var(--border);border-radius:10px;overflow:hidden">' +
          '<video src="' + escapeHtml(c.url) + '" controls preload="metadata" style="width:100%;aspect-ratio:9/16;background:#000;display:block"></video>' +
          '<div style="padding:6px 8px;font-size:11px;color:var(--muted-foreground);display:flex;justify-content:space-between;align-items:center;gap:6px">' +
            '<span>' + dt + ' · ' + mb + 'MB</span>' +
            '<span style="display:flex;gap:8px">' +
              '<a href="' + escapeHtml(c.url) + '" download style="color:#22c55e;text-decoration:none;font-weight:700">⬇</a>' +
              '<button onclick="deleteClip(\'' + nm + '\',this)" style="background:none;border:none;color:#f87171;cursor:pointer;font-size:13px;padding:0" title="ลบ">🗑</button>' +
            '</span>' +
          '</div>' +
        '</div>';
      }).join('');
    } catch (e) {
      div.innerHTML = '<div style="color:#f87171;font-size:13px">โหลดไม่สำเร็จ</div>';
    }
  }

  function renderApPlatforms(schedPlatforms) {
    const div = document.getElementById('ap-platforms');
    if (!div) return;
    div.innerHTML = AP_PLATS.map(m => {
      const sp = (schedPlatforms || []).find(p => p.name === m.name);
      const connected = !!_apPlatStatus[m.name];
      const enabled = sp ? sp.enabled : (m.name === 'facebook');
      const time = (sp && sp.time) || m.def;
      const status = connected
        ? '<span style="color:#22c55e;white-space:nowrap">🟢 พร้อม</span>'
        : '<span style="color:var(--muted-foreground);white-space:nowrap">⚪ ยังไม่เชื่อม</span>';
      return '<div style="display:flex;align-items:center;gap:10px;background:#1f2430;border:1px solid var(--border);border-radius:10px;padding:8px 12px;font-size:13px;flex-wrap:wrap' + (connected ? '' : ';opacity:.65') + '">' +
        '<label style="display:flex;align-items:center;gap:6px;flex:1;min-width:120px;cursor:pointer">' +
          '<input type="checkbox" class="ap-plat" data-name="' + m.name + '"' + (enabled ? ' checked' : '') + (connected ? '' : ' disabled') + '> ' + m.label +
        '</label>' +
        '<span style="color:var(--muted-foreground)">⏰</span>' +
        '<input type="time" class="ap-plat-time" data-name="' + m.name + '" value="' + time + '" style="background:#141821;border:1px solid var(--border);border-radius:6px;padding:4px 6px;color:var(--foreground)">' +
        status +
      '</div>';
    }).join('');
  }

  function renderApDays(selected) {
    const div = document.getElementById('ap-days');
    if (!div) return;
    const sel = selected || [0, 1, 2, 3, 4, 5, 6];
    div.innerHTML = AP_DAYS.map((d, i) =>
      '<label style="display:flex;align-items:center;gap:4px;font-size:13px;background:#1f2430;border:1px solid var(--border);border-radius:8px;padding:5px 9px;cursor:pointer">' +
      '<input type="checkbox" class="ap-day" value="' + i + '"' + (sel.includes(i) ? ' checked' : '') + '> ' + d + '</label>').join('');
  }

  const AP_STAGES = [
    { key: 'script', label: '📝 สคิป' },
    { key: 'media', label: '🎨 VDO' },
    { key: 'render', label: '🎬 ตัดต่อ' },
    { key: 'posting', label: '📤 โพสต์' },
    { key: 'done', label: '✅ Done' },
  ];
  function renderApStatus(s) {
    const bar = document.getElementById('ap-status');
    if (!bar) return;
    const stage = (s && s.stage) || 'idle';
    if (stage === 'idle') { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    const isErr = stage === 'error';
    let curIdx = AP_STAGES.findIndex(x => x.key === stage);
    if (stage === 'done') curIdx = AP_STAGES.length;
    if (isErr) curIdx = Math.max(0, AP_STAGES.findIndex(x => x.key === 'posting'));
    const steps = document.getElementById('ap-status-steps');
    if (steps) {
      steps.innerHTML = AP_STAGES.map((st, i) => {
        let col = '#3a4150', glow = '';
        if (isErr && i >= curIdx) col = '#ef4444';
        else if (i < curIdx) col = '#22c55e';
        else if (i === curIdx) { col = '#f59e0b'; glow = 'box-shadow:0 0 10px #f59e0b;animation:apPulse 1s infinite'; }
        const chip = '<span style="background:#1f2430;border:1px solid ' + col + ';color:' + col + ';border-radius:8px;padding:3px 9px;font-size:12px;font-weight:600;' + glow + '">' + st.label + '</span>';
        return chip + (i < AP_STAGES.length - 1 ? '<span style="color:var(--muted-foreground)">→</span>' : '');
      }).join('');
    }
    const pct = (s && s.pct != null) ? s.pct : 0;
    const barEl = document.getElementById('ap-status-bar');
    if (barEl) {
      barEl.style.width = pct + '%';
      barEl.style.background = isErr ? '#ef4444' : 'linear-gradient(90deg,#3b82f6,#22c55e)';
      barEl.style.boxShadow = '0 0 10px ' + (isErr ? '#ef4444' : '#22c55e');
    }
    const t = document.getElementById('ap-status-text'); if (t) t.textContent = (s && s.detail) || stage;
    const ti = document.getElementById('ap-status-title'); if (ti) ti.textContent = (s && s.title) ? ('· ' + s.title) : '';
    const p = document.getElementById('ap-status-pct'); if (p) { p.textContent = pct + '%'; p.style.color = isErr ? '#ef4444' : '#22c55e'; }
    const at = document.getElementById('ap-status-at'); if (at) at.textContent = (s && s.at) || '';
  }

  function startApStatusPoll() {
    stopApStatusPoll();
    _apStatusTimer = setInterval(async () => {
      const panel = document.getElementById('panel-autopost');
      if (!panel || panel.style.display === 'none') { stopApStatusPoll(); return; }
      try {
        const d = await api('/workspace/autopost/data');
        renderApStatus(d.status);
        renderApLog(d.log || []);
      } catch (e) {}
    }, 3000);
  }
  function stopApStatusPoll() { if (_apStatusTimer) { clearInterval(_apStatusTimer); _apStatusTimer = null; } }

  function _apFormBody() {
    return {
      id: document.getElementById('ap-id').value || '',
      label: document.getElementById('ap-label').value || '',
      content_type: document.getElementById('ap-content').value || 'mystery',
      tone: document.getElementById('ap-tone').value || 'evidence',
      topic: document.getElementById('ap-topic').value || '',
      platforms: AP_PLATS.map(m => {
        const cb = document.querySelector('.ap-plat[data-name="' + m.name + '"]');
        const tm = document.querySelector('.ap-plat-time[data-name="' + m.name + '"]');
        return { name: m.name, enabled: cb ? cb.checked : false, time: tm ? tm.value : m.def };
      }),
      days: Array.from(document.querySelectorAll('.ap-day:checked')).map(x => parseInt(x.value, 10)),
      enabled: document.getElementById('ap-enabled').checked,
    };
  }

  function resetAutopostForm() {
    document.getElementById('ap-id').value = '';
    document.getElementById('ap-label').value = '';
    document.getElementById('ap-content').value = 'mystery';
    document.getElementById('ap-tone').value = 'evidence';
    document.getElementById('ap-topic').value = '';
    document.getElementById('ap-enabled').checked = true;
    document.getElementById('autopost-form-title').textContent = '➕ ตั้งตารางโพสต์ใหม่';
    renderApPlatforms();
    renderApDays();
    const m = document.getElementById('ap-form-msg'); if (m) m.textContent = '';
  }

  async function saveAutopost() {
    const body = _apFormBody();
    if (!body.platforms.some(p => p.enabled)) { showToast('เปิดอย่างน้อย 1 ช่องทาง'); return; }
    try {
      await api('/workspace/autopost/save', { method: 'POST', body: JSON.stringify(body) });
      showToast('บันทึกตารางแล้ว ✅');
      resetAutopostForm();
      loadAutopost();
    } catch (e) { showToast('บันทึกไม่สำเร็จ: ' + e.message); }
  }

  async function runAutopostNow() {
    const body = _apFormBody();
    if (!body.platforms.some(p => p.enabled)) { showToast('เปิดอย่างน้อย 1 ช่องทาง'); return; }
    const m = document.getElementById('ap-form-msg');
    if (m) m.textContent = '⏳ กำลังสร้างคลิป + โพสต์… ดูไฟสถานะด้านบน';
    try {
      await api('/workspace/autopost/run', { method: 'POST', body: JSON.stringify(body) });
      showToast('เริ่มสร้างคลิปแล้ว — ดูไฟสถานะ');
      startApStatusPoll();
    } catch (e) { showToast('สั่งรันไม่สำเร็จ: ' + e.message); }
  }

  async function runAutopostPreview() {
    const body = _apFormBody();
    body.preview = true;
    const m = document.getElementById('ap-form-msg');
    if (m) m.textContent = '⏳ กำลังสร้างคลิปทดสอบ… จะส่งเข้า Telegram (ไม่โพสต์)';
    try {
      await api('/workspace/autopost/run', { method: 'POST', body: JSON.stringify(body) });
      showToast('เริ่มสร้างคลิปทดสอบ — ดูไฟสถานะ + Telegram');
      startApStatusPoll();
      setTimeout(loadAutopostClips, 90000);
    } catch (e) { showToast('สั่งรันไม่สำเร็จ: ' + e.message); }
  }

  async function deleteClip(name, btn) {
    if (!confirm('ลบคลิปนี้ออกจาก server?')) return;
    try {
      await api('/workspace/vdo/delete', { method: 'POST', body: JSON.stringify({ name }) });
      const card = btn.closest('[data-clip]'); if (card) card.remove();
      showToast('ลบคลิปแล้ว 🗑');
    } catch (e) { showToast('ลบไม่สำเร็จ: ' + e.message); }
  }

  function editAutopost(id) {
    const j = _apSchedules.find(s => s.id === id);
    if (!j) return;
    document.getElementById('ap-id').value = j.id;
    document.getElementById('ap-label').value = j.label || '';
    document.getElementById('ap-content').value = j.content_type || 'mystery';
    document.getElementById('ap-tone').value = j.tone || 'evidence';
    document.getElementById('ap-topic').value = j.topic || '';
    document.getElementById('ap-enabled').checked = j.enabled !== false;
    document.getElementById('autopost-form-title').textContent = '✏️ แก้ไข: ' + (j.label || '');
    renderApPlatforms(j.platforms || []);
    renderApDays(j.days || [0, 1, 2, 3, 4, 5, 6]);
    document.getElementById('panel-autopost').scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function deleteAutopost(id) {
    if (!confirm('ลบตารางนี้?')) return;
    try {
      await api('/workspace/autopost/delete', { method: 'POST', body: JSON.stringify({ id }) });
      loadAutopost();
    } catch (e) { showToast('ลบไม่สำเร็จ: ' + e.message); }
  }

  async function runAutopostId(id) {
    try {
      await api('/workspace/autopost/run', { method: 'POST', body: JSON.stringify({ id }) });
      showToast('เริ่มสร้าง+โพสต์แล้ว — ดูไฟสถานะ');
      startApStatusPoll();
    } catch (e) { showToast('รันไม่สำเร็จ: ' + e.message); }
  }

  const AP_TONE_LABEL = { evidence: '📜 จริงจัง', cheeky: '😏 กวนๆ', twist: '🔄 หักมุม', academic: '🎓 วิชาการ', creepy: '👻 ขนลุก' };
  function apPlatIcon(n) { return n === 'facebook' ? '📘' : n === 'youtube' ? '▶️' : '🎵'; }

  function renderApSchedules(list) {
    const div = document.getElementById('autopost-schedules');
    if (!div) return;
    if (!list.length) {
      div.innerHTML = '<div style="color:var(--muted-foreground);font-size:13px">ยังไม่มีตาราง — ตั้งด้านบนได้เลย</div>';
      return;
    }
    div.innerHTML = list.map(j => {
      const plats = (j.platforms || []).filter(p => p.enabled)
        .map(p => apPlatIcon(p.name) + ' ' + escapeHtml(p.time || '')).join('  ') || '—';
      const days = (j.days && j.days.length === 7) ? 'ทุกวัน' : (j.days || []).map(d => AP_DAYS[d]).join(',');
      const ctype = j.content_type === 'news' ? '📰 ข่าว' : '🔮 สายมู';
      const tone = AP_TONE_LABEL[j.tone || 'evidence'] || '';
      const topic = j.topic ? escapeHtml(j.topic) : '(AI สุ่มเอง)';
      const onoff = j.enabled !== false;
      const id = escapeHtml(j.id);
      const last = (j._state && j._state.gen_date) ? j._state.gen_date : '';
      return '<div class="surface" style="border:1px solid var(--border);border-radius:10px;padding:12px;display:flex;justify-content:space-between;align-items:center;gap:12px;' + (onoff ? '' : 'opacity:.5') + '">' +
        '<div style="font-size:13px;line-height:1.6">' +
          '<div style="font-weight:600">' + (onoff ? '🟢' : '⚪') + ' ' + escapeHtml(j.label || '(ไม่มีชื่อ)') + '</div>' +
          '<div style="color:var(--muted-foreground)">' + ctype + ' · ' + tone + ' · ' + topic + '</div>' +
          '<div style="color:var(--muted-foreground)">📅 ' + days + ' · ' + plats + '</div>' +
          (last ? '<div style="color:var(--muted-foreground);font-size:12px">สร้างล่าสุด: ' + escapeHtml(last) + '</div>' : '') +
        '</div>' +
        '<div style="display:flex;gap:6px;flex-shrink:0">' +
          '<button class="panel-action" onclick="runAutopostId(\'' + id + '\')" title="สร้าง+โพสต์เดี๋ยวนี้">▶</button>' +
          '<button class="panel-action" onclick="editAutopost(\'' + id + '\')" title="แก้ไข">✏️</button>' +
          '<button class="panel-action" onclick="deleteAutopost(\'' + id + '\')" title="ลบ">🗑️</button>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  function renderApLog(log) {
    const div = document.getElementById('autopost-log');
    if (!div) return;
    if (!log.length) { div.innerHTML = '<div style="color:var(--muted-foreground);font-size:13px">ยังไม่มีประวัติ</div>'; return; }
    div.innerHTML = log.map(e =>
      '<div style="font-size:12.5px;background:#1f2430;border:1px solid var(--border);border-radius:8px;padding:8px 10px">' +
        (e.ok ? '✅' : '❌') + ' <b>' + escapeHtml(e.label || '') + '</b> · ' + escapeHtml(e.at || '') +
        (e.src === 'manual' ? ' <span style="color:#a78bfa">(ทดสอบ)</span>' : '') +
        '<div style="color:var(--muted-foreground);margin-top:2px">' + escapeHtml((e.title ? ('📿 ' + e.title + ' — ') : '') + (e.msg || '')) + '</div>' +
      '</div>').join('');
  }

  async function uploadFace(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const msg = document.getElementById('ap-face-msg');
    if (msg) msg.textContent = '⏳ กำลังอัปโหลด…';
    try {
      const fd = new FormData();
      fd.append('image', file);
      const res = await fetch('/workspace/autopost/face', {
        method: 'POST', body: fd, credentials: 'same-origin',
      });
      if (!res.ok) throw new Error('upload failed');
      if (msg) msg.textContent = '✅ อัปโหลดแล้ว';
      const img = document.getElementById('ap-face-preview');
      if (img) { img.style.display = ''; img.src = '/avatar/face.jpg?t=' + Date.now(); }
      showToast('อัปโหลดรูปหน้าแล้ว ✅');
    } catch (e) {
      if (msg) msg.textContent = '❌ ' + e.message;
      showToast('อัปโหลดไม่สำเร็จ: ' + e.message);
    }
    input.value = '';
  }

  window.uploadFace = uploadFace;
  window.loadAutopost = loadAutopost;
  window.loadAutopostClips = loadAutopostClips;
  window.runAutopostPreview = runAutopostPreview;
  window.deleteClip = deleteClip;
  window.saveAutopost = saveAutopost;
  window.runAutopostNow = runAutopostNow;
  window.resetAutopostForm = resetAutopostForm;
  window.editAutopost = editAutopost;
  window.deleteAutopost = deleteAutopost;
  window.runAutopostId = runAutopostId;

  window.showPanel = showPanel;
  window.openBuildingFloor = openBuildingFloor;
  window.openOfficeAgentChat = openOfficeAgentChat;
  window.openOfficePixelDesk = openOfficePixelDesk;
  window.newChat = newChat;
  window.sendMessage = sendMessage;
  window.clearPendingImage = clearPendingImage;
  window.handleChatFormSubmit = function(event) {
    if (typeof sendMessage === 'function') {
      event.preventDefault();
      sendMessage();
      return false;
    }
    return true;
  };
  window.showNewProjectModal = showNewProjectModal;
  window.closeModal = closeModal;
  window.createProject = createProject;
  window.selectProject = selectProject;
  window.saveNote = saveNote;
  window.createTask = createTask;
  window.generateStandup = generateStandup;
  window.copyStandupReport = copyStandupReport;
  window.copyAiMessage = copyAiMessage;
  window.updateProject = updateProject;
  window.runBrainstorm = runBrainstorm;
  window.fetchNews = fetchNews;
  window.makeMysteryVdo = makeMysteryVdo;
  window.summarizeFile = summarizeFile;
  window.askFile = askFile;
  window.dropZoneClick = dropZoneClick;
  window.selectSlash = selectSlash;
  window.sendToSecretary = sendOfficeSecretary;
  window.sendOfficeSecretary = sendOfficeSecretary;
  window.loadOfficeActivity = loadOfficeActivity;
  window.startOfficeEventStream = startOfficeEventStream;
  window.stopOfficeEventStream = stopOfficeEventStream;
  window.pillClickAgent = pillClickAgent;
  window.notifyMapRoute = notifyMapRoute;
  window.syncMapAgentStatus = syncMapAgentStatus;
  window.focusOfficeSecretary = focusOfficeSecretary;
  window.showToast = showToast;
  window.api = api;
  window.escapeHtml = escapeHtml;
  if (typeof richMarkdown === 'function') {
    window.renderMarkdown = richMarkdown;
  }

  window._currentProject = window.__WORKSPACE_PROJECT_ID__ ?? null;
  state.currentProject = window._currentProject;
  loadActiveModelBadge();
  const initialTool = window.__WORKSPACE_TOOL__ || 'chat';
  const hasSsrMessages =
    initialTool === 'chat' && chatMessages && chatMessages.querySelector('.msg-row');
  showPanel(initialTool, {skipHistoryLoad: Boolean(hasSsrMessages)});
  applyOfficeChatPrefill();
  loadProjects();
  if (hasSsrMessages) {
    enhanceSsrChatMessages();
    updateChatWelcome();
    scrollToBottom();
  }

  document.addEventListener('visibilitychange', () => {
    const onOffice =
      window.__WORKSPACE_TOOL__ === 'office' ||
      window.__WORKSPACE_TOOL__ === 'secretary';
    const panel = document.getElementById('panel-office');
    const officeActive = panel && panel.classList.contains('active-panel');
    if (document.hidden) {
      stopOfficeEventStream();
    } else if (onOffice && officeActive) {
      startOfficeEventStream();
      startPBClock();
    }
  });

  if (document.getElementById('building-clock')) {
    startBuildingClock();
  }
  if (document.getElementById('pb-clock')) {
    startPBClock();
  }
  } catch(err) {
    console.error('WORKSPACE JS ERROR:', err);
    const contentEl = document.getElementById('content');
    if (contentEl) {
      contentEl.innerHTML =
        '<div style="color:#ef4444;padding:32px;font-family:monospace">' +
        '<h2>⚠️ JavaScript Error</h2>' +
        '<pre style="background:#1a1a1a;padding:16px;border-radius:8px;' +
        'overflow:auto;font-size:13px">' +
        (err.stack || String(err)).replace(/</g, '&lt;') + '</pre>' +
        '<p style="color:#888">แจ้ง admin เพื่อแก้ไข</p></div>';
    }
  }
});

const TEST_QUESTION_IDS = {
  it: ['it_01', 'it_02', 'it_03'],
  en: ['en_01', 'en_02', 'en_03'],
  hal: ['hal_01', 'hal_02', 'hal_03'],
  ch: ['ch_01', 'ch_02', 'ch_03'],
};

function _benchEscapeHtml(text) {
  if (typeof window.escapeHtml === 'function') return window.escapeHtml(text);
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function _benchRenderMarkdown(text) {
  if (typeof window.renderMarkdown === 'function') return window.renderMarkdown(text);
  return _benchEscapeHtml(text || '').replace(/\n/g, '<br>');
}

function renderBenchSummary(stats) {
  const COLORS = {groq: '#22c55e', gemini: '#3b82f6', haiku: '#a855f7', 'deepseek-r1': '#f59e0b'};
  const container = document.getElementById('bench-summary');
  if (!container) return;
  if (!stats.length) {
    container.innerHTML = '<div class="empty-state">No benchmark data yet.</div>';
    return;
  }
  const html = stats.map((s) => `
    <div class="sys-card" style="min-width:160px">
      <div class="sys-label" style="color:${COLORS[s.model] || '#888'}">
        ${_benchEscapeHtml(String(s.model || '').toUpperCase())}
      </div>
      <div class="sys-value">${Math.round(Number(s.avg_ms || 0))}ms</div>
      <div style="font-size:12px;color:#888;margin-top:4px">
        ${Number(s.runs || 0)} runs
        ${s.avg_rating ? ` · ⭐ ${parseFloat(s.avg_rating).toFixed(1)}` : ''}
      </div>
    </div>
  `).join('');
  container.innerHTML = html;
}

function groupByQuestion(rows) {
  const groups = {};
  for (const row of rows) {
    if (!groups[row.question_id]) {
      groups[row.question_id] = {
        question_id: row.question_id,
        category: row.category,
        question: row.question,
        models: {},
      };
    }
    const current = groups[row.question_id].models[row.model];
    if (!current || Number(row.id || row.db_id || 0) > Number(current.id || current.db_id || 0)) {
      groups[row.question_id].models[row.model] = row;
    }
  }
  return Object.values(groups);
}

function renderBenchResults(groups) {
  const MODELS = ['groq', 'gemini', 'haiku'];
  const container = document.getElementById('bench-results');
  if (!container) return;
  const html = groups.map((g) => `
    <div style="background:#1a1a1a;border-radius:10px;margin-bottom:16px;overflow:hidden">
      <div style="padding:12px 16px;background:#222;border-bottom:1px solid #333">
        <span style="font-size:11px;color:#888;margin-right:8px">
          ${_benchEscapeHtml(g.category || '')}
        </span>
        <span style="font-size:14px;font-weight:500">${_benchEscapeHtml(g.question || '')}</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(${MODELS.length},1fr);gap:1px;background:#333">
        ${MODELS.map((m) => {
          const r = g.models[m];
          if (!r) return `<div style="padding:12px;background:#1a1a1a;color:#555;font-size:13px">-</div>`;
          const latencyMs = Number(r.latency_ms || 0);
          const color = latencyMs > 3000 ? '#ef4444' : latencyMs > 1500 ? '#f59e0b' : '#22c55e';
          const resultId = Number(r.id || r.db_id || 0);
          const stars = [1, 2, 3, 4, 5].map((n) =>
            `<span onclick="rateBenchmark(${resultId}, ${n})"
                   style="cursor:pointer;font-size:16px;color:${(Number(r.rating || 0) >= n) ? '#f59e0b' : '#444'}">★</span>`
          ).join('');
          return `
            <div style="padding:12px 16px;background:#1a1a1a">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-size:12px;font-weight:600;color:${color}">${_benchEscapeHtml(m)}</span>
                <span style="font-size:11px;color:${color}">
                  ${r.error ? '❌ error' : latencyMs + 'ms'}
                </span>
              </div>
              <div style="font-size:13px;line-height:1.6;color:#ccc;max-height:120px;overflow-y:auto">
                ${_benchRenderMarkdown(r.error || r.answer || '-')}
              </div>
              <div style="margin-top:8px">${stars}</div>
            </div>`;
        }).join('')}
      </div>
    </div>
  `).join('');
  container.innerHTML = html || '<p style="color:#888;padding:16px">No results yet. Click Run Benchmark.</p>';
}

async function loadBenchmark() {
  try {
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 5000);
    const res = await fetch('/workspace/benchmark/summary', {
      signal: ctrl.signal,
      credentials: 'same-origin',
    });
    clearTimeout(timeout);
    if (!res.ok) return;
    const data = await res.json();
    if (data.model_stats && data.model_stats.length > 0) {
      renderBenchSummary(data.model_stats);
    }
    if (data.recent && data.recent.length > 0) {
      renderBenchResults(groupByQuestion(data.recent));
    }
  } catch (error) {
    console.log('benchmark summary load failed:', error.message);
  }
}

async function runBenchmark() {
  const btn = document.getElementById('bench-run-btn');
  if (!btn || btn.disabled) return;

  btn.disabled = true;
  btn.textContent = '⏳ Running...';

  const prog = document.getElementById('bench-progress');
  if (prog) prog.style.display = 'block';

  const resultsEl = document.getElementById('bench-results');
  if (resultsEl) {
    resultsEl.innerHTML = '<p style="color:#888;padding:24px">⏳ Running benchmark — may take 60-120 seconds...</p>';
  }

  try {
    const cat = document.getElementById('bench-category')?.value || '';
    const ids = cat ? TEST_QUESTION_IDS[cat] : null;

    const res = await fetch('/workspace/benchmark/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({question_ids: ids}),
    });

    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();

    const flat = [];
    for (const q of data.results || []) {
      for (const r of q.results || []) {
        flat.push({
          question_id: q.question_id,
          category: q.category,
          question: q.question,
          id: r.db_id || r.id || (Date.now() + Math.random()),
          db_id: r.db_id || null,
          model: r.model,
          answer: r.answer,
          latency_ms: r.latency_ms,
          rating: r.rating || 0,
          error: r.error,
        });
      }
    }
    renderBenchResults(groupByQuestion(flat));
    await loadBenchmark();
    if (typeof window.showToast === 'function') window.showToast('✅ Benchmark complete!');
  } catch (error) {
    if (typeof window.showToast === 'function') window.showToast('❌ Error: ' + error.message);
    if (resultsEl) {
      resultsEl.innerHTML = '<p style="color:#ef4444;padding:24px">❌ ' + _benchEscapeHtml(error.message) + '</p>';
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Run Benchmark';
    if (prog) prog.style.display = 'none';
  }
}

async function rateBenchmark(id, rating) {
  const apiFn = typeof window.api === 'function' ? window.api : null;
  if (apiFn) {
    await apiFn('/workspace/benchmark/rate', {
      method: 'POST',
      body: JSON.stringify({id, rating}),
    });
  } else {
    await fetch('/workspace/benchmark/rate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({id, rating}),
    });
  }
  if (typeof window.showToast === 'function') window.showToast(`⭐ Rated ${rating}/5`);
  await loadBenchmark();
}

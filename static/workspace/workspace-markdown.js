/**
 * Markdown + syntax highlight for workspace AI messages (marked + highlight.js).
 */
(function() {
  'use strict';

  const COPY_ICON =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
    '<rect x="9" y="9" width="13" height="13" rx="2"/>' +
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>' +
    '</svg>';

  let _initialized = false;

  function escapeHtml(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function sanitizeAiContent(text) {
    let raw = String(text || '');
    raw = raw.replace(/<function_calls>[\s\S]*?<\/function_calls>/gi, '');
    raw = raw.replace(/<invoke\b[^>]*>[\s\S]*?<\/invoke>/gi, '');
    raw = raw.replace(/<\/?parameter\b[^>]*>/gi, '');
    return raw.trim();
  }

  function initMarkdownRenderer() {
    if (_initialized) return true;
    if (typeof marked === 'undefined') return false;

    marked.setOptions({ breaks: true, gfm: true });

    const renderer = {
      code(tokenOrText, maybeLang) {
        // marked v9 may pass a token object, older versions pass (code, infostring).
        const token = (tokenOrText && typeof tokenOrText === 'object')
          ? tokenOrText
          : { text: tokenOrText, lang: maybeLang };
        let code = String(token?.text || '');
        const rawLang = String(token?.lang || maybeLang || '');
        const language = (rawLang || 'text').toLowerCase() || 'text';

        // Some model outputs arrive as fenced raw block where token.text becomes empty.
        if (!code.trim()) {
          const raw = String(token?.raw || '');
          const m = raw.match(/```[^\n\r]*\r?\n?([\s\S]*?)```/);
          if (m && m[1]) code = String(m[1]);
        }
        code = code.replace(/\r\n/g, '\n');
        if (!code.trim()) {
          code = '// (empty code block from model)';
        }

        let highlighted = escapeHtml(code);
        if (typeof hljs !== 'undefined') {
          try {
            if (rawLang && hljs.getLanguage(rawLang)) {
              highlighted = hljs.highlight(code, { language: rawLang }).value;
            } else {
              highlighted = hljs.highlightAuto(code).value;
            }
          } catch (e) {
            highlighted = escapeHtml(code);
          }
        }
        const id = 'code-' + Math.random().toString(36).slice(2);
        const label = language === 'text' ? 'code' : language;
        return (
          '<div class="code-block">' +
          '<div class="code-header">' +
          '<span class="code-lang">' + escapeHtml(label) + '</span>' +
          '<button type="button" class="code-copy-btn" data-code-id="' + id + '" aria-label="Copy code">' +
          COPY_ICON + ' Copy</button>' +
          '</div>' +
          '<pre><code id="' + id + '" class="hljs language-' + escapeHtml(label) + '">' +
          highlighted +
          '</code></pre></div>'
        );
      },
    };

    marked.use({ renderer });
    _initialized = true;
    return true;
  }

  function renderMarkdownSimple(text) {
    let html = escapeHtml(text || '');
    html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    html = html.replace(/\n/g, '<br>');
    return html;
  }

  function renderMarkdown(text, options) {
    const opts = options || {};
    const cleaned = opts.skipSanitize ? String(text || '') : sanitizeAiContent(text);

    if (initMarkdownRenderer()) {
      try {
        return marked.parse(cleaned || '');
      } catch (e) {
        console.warn('marked.parse failed', e);
      }
    }
    return renderMarkdownSimple(cleaned);
  }

  function bindCodeCopyButtons(root) {
    const scope = root || document;
    scope.querySelectorAll('.code-copy-btn').forEach((btn) => {
      if (btn.dataset.copyBound === '1') return;
      btn.dataset.copyBound = '1';
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-code-id');
        const el = id ? document.getElementById(id) : btn.closest('.code-block')?.querySelector('code');
        if (!el) return;
        const plain = el.textContent || '';
        navigator.clipboard.writeText(plain).then(() => {
          btn.innerHTML = '✓ Copied';
          btn.classList.add('copied');
          setTimeout(() => {
            btn.innerHTML = COPY_ICON + ' Copy';
            btn.classList.remove('copied');
          }, 2000);
        }).catch(() => {
          if (typeof window.showToast === 'function') window.showToast('Copy failed');
        });
      });
    });
  }

  function extractFencedBlocks(text) {
    const out = [];
    const raw = String(text || '');
    const re = /```[^\n\r]*\r?\n?([\s\S]*?)```/g;
    let m;
    while ((m = re.exec(raw)) !== null) {
      out.push(String(m[1] || '').replace(/\r\n/g, '\n'));
    }
    return out;
  }

  function hydrateEmptyCodeBlocks(root, sourceText) {
    if (!root) return;
    const blocks = extractFencedBlocks(sourceText);
    const codeEls = root.querySelectorAll('.code-block code, pre code');
    codeEls.forEach((el, idx) => {
      const current = String(el.textContent || '').trim();
      const fallback = (blocks[idx] || '').trim() || '// (empty code block from model)';
      if (current && current.toLowerCase() !== 'code') return;
      el.textContent = fallback;
      if (typeof hljs !== 'undefined') {
        try { hljs.highlightElement(el); } catch (e) {}
      }
    });
  }

  function getRenderMode() {
    try {
      return localStorage.getItem('ws_render_mode') === 'plain' ? 'plain' : 'markdown';
    } catch (e) {
      return 'markdown';
    }
  }

  function setRenderMode(mode) {
    const m = mode === 'plain' ? 'plain' : 'markdown';
    try {
      localStorage.setItem('ws_render_mode', m);
    } catch (e) {
      /* ignore */
    }
    document.querySelectorAll('.msg-text.markdown-body').forEach((el) => {
      const raw = el.dataset.raw || el.getAttribute('data-raw') || '';
      if (!raw) return;
      if (m === 'plain') {
        el.classList.remove('markdown-body');
        el.innerHTML = escapeHtml(raw).replace(/\n/g, '<br>');
      } else {
        renderMarkdownInto(el, raw);
      }
    });
  }

  function showPlainInto(el) {
    if (!el) return;
    const raw = el.dataset.raw || el.getAttribute('data-raw') || el.textContent || '';
    const overlay = document.createElement('div');
    overlay.className = 'raw-modal-overlay';
    overlay.innerHTML = `
      <div class="raw-modal">
        <div class="raw-modal-header">
          <strong>Raw message</strong>
          <button type="button" class="raw-modal-close" aria-label="Close">×</button>
        </div>
        <pre class="raw-modal-body"></pre>
        <div class="raw-modal-actions">
          <button type="button" class="raw-copy-btn">Copy raw</button>
        </div>
      </div>
    `;
    overlay.querySelector('.raw-modal-close').addEventListener('click', () => overlay.remove());
    overlay.querySelector('.raw-copy-btn').addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(raw);
        if (typeof window.showToast === 'function') window.showToast('Copied raw');
      } catch (e) {
        if (typeof window.showToast === 'function') window.showToast('Copy failed');
      }
    });
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.remove();
    });
    document.body.appendChild(overlay);
    overlay.querySelector('.raw-modal-body').textContent = raw;
  }

  function renderMarkdownInto(el, rawText) {
    if (!el) return;
    const cleaned = sanitizeAiContent(rawText);
    el.dataset.raw = cleaned;
    if (getRenderMode() === 'plain') {
      el.classList.remove('markdown-body');
      el.innerHTML = escapeHtml(cleaned).replace(/\n/g, '<br>');
      return;
    }
    el.classList.add('markdown-body');
    el.innerHTML = renderMarkdown(cleaned);
    hydrateEmptyCodeBlocks(el, cleaned);
    bindCodeCopyButtons(el);
  }

  function renderAllMarkdownBodies(root) {
    const scope = root || document;
    scope.querySelectorAll('.markdown-body[data-raw], .msg-text.markdown-body').forEach((el) => {
      const raw = el.getAttribute('data-raw') ?? el.dataset.raw ?? el.textContent ?? '';
      if (raw) renderMarkdownInto(el, raw);
    });
  }

  window.sanitizeAiContent = sanitizeAiContent;
  window.renderMarkdown = renderMarkdown;
  window.bindCodeCopyButtons = bindCodeCopyButtons;
  window.renderMarkdownInto = renderMarkdownInto;
  window.renderAllMarkdownBodies = renderAllMarkdownBodies;
  window.getRenderMode = getRenderMode;
  window.setRenderMode = setRenderMode;
  window.showPlainInto = showPlainInto;
  window.copyCode = function(id) {
    const el = document.getElementById(id);
    if (!el) return;
    const btn = el.closest('.code-block')?.querySelector('.code-copy-btn');
    if (btn) btn.click();
  };
})();

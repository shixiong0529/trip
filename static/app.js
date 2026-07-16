/**
 * AI 旅行攻略生成器 v2.0 · 前端交互逻辑
 * 功能：SSE 流式生成、实时数据进度、行程管理、下载、配置
 */

(function() {
  'use strict';

  // ====== DOM 引用 ======
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const els = {
    inputSection: $('#input-section'),
    generatingSection: $('#generating-section'),
    resultSection: $('#result-section'),
    queryInput: $('#query-input'),
    charCount: $('#char-count'),
    btnGenerate: $('#btn-generate'),
    btnCancel: $('#btn-cancel'),
    btnRegenerate: $('#btn-regenerate'),
    btnSaveTrip: $('#btn-save-trip'),
    tripsSection: $('#trips-section'),
    tripsList: $('#trips-list'),
    btnRefreshTrips: $('#btn-refresh-trips'),
    navTabs: $$('.nav-tab'),
    btnDownloadHtml: $('#btn-download-html'),
    btnDownloadPdf: $('#btn-download-pdf'),
    btnDownloadDocx: $('#btn-download-docx'),
    streamOutput: $('#stream-output'),
    guidePreview: $('#guide-preview'),
    guideIdLabel: $('#guide-id-label'),
    statusBar: $('#status-bar'),
    statusDot: $('#status-dot'),
    statusText: $('#status-text'),
    toastContainer: $('#toast-container'),
  };

  // ====== 状态 ======
  const state = {
    mode: 'idle',        // idle | generating | done
    guideId: null,
    guideHtml: null,
    guideMarkdown: null,
    progressSteps: [],
    abortController: null,
    generationRunId: 0,
    activeTab: 'generate', // generate | trips；切换页面时保留真实生成状态
    pdfReady: true,      // 由 /api/health 的 pdf_ready 更新，决定 PDF 下载按钮是否可用
  };

  // 旧版本曾把 API Key 保存在浏览器中；现在统一使用服务端配置并清理遗留敏感数据。
  function clearLegacyConfig() {
    try {
      localStorage.removeItem('travel_guide_config');
    } catch (e) { /* ignore */ }
  }

  // ====== 健康检查 ======
  async function checkHealth() {
    try {
      const res = await fetch('/api/health');
      const data = await res.json();
      const hasServerKey = data.llm_configured;

      state.pdfReady = data.pdf_ready !== false;
      updatePdfButtonState();

      if (els.statusDot && els.statusText) {
        if (hasServerKey) {
          els.statusDot.className = 'status-dot connected';
          els.statusText.textContent = 'LLM 已配置（服务端 API Key）';
        } else {
          els.statusDot.className = 'status-dot disconnected';
          els.statusText.textContent = '未配置 API Key，请在服务端 .env 中设置 LLM_API_KEY';
        }
      }
    } catch (e) {
      if (els.statusDot && els.statusText) {
        els.statusDot.className = 'status-dot error';
        els.statusText.textContent = '服务连接失败，请确认后端已启动';
      }
    }
  }

  // ====== PDF 可用性 ======
  function updatePdfButtonState() {
    if (!els.btnDownloadPdf) return;
    if (state.pdfReady === false) {
      els.btnDownloadPdf.disabled = true;
      els.btnDownloadPdf.title = 'PDF 需要安装系统依赖（brew install pango cairo glib）';
    } else {
      els.btnDownloadPdf.disabled = false;
      els.btnDownloadPdf.title = '';
    }
  }

  // ====== 模式切换 ======
  function setMode(mode) {
    state.mode = mode;
    if (state.activeTab === 'trips') {
      els.inputSection.style.display = 'none';
      els.generatingSection.style.display = 'none';
      els.resultSection.style.display = 'none';
      els.tripsSection.style.display = 'block';
      return;
    }
    els.inputSection.style.display = mode === 'idle' ? 'flex' : 'none';
    els.generatingSection.style.display = mode === 'generating' ? 'block' : 'none';
    els.resultSection.style.display = mode === 'done' ? 'block' : 'none';
    els.tripsSection.style.display = 'none';
    if (mode === 'done') {
      updatePdfButtonState();
    }
  }

  function setActiveTab(tabName) {
    state.activeTab = tabName;
    els.navTabs.forEach(function(tab) {
      tab.classList.toggle('active', tab.dataset.tab === tabName);
    });
    setMode(state.mode);
    if (tabName === 'trips') {
      loadTrips();
    }
  }

  // ====== Toast 通知 ======
  function showToast(msg, type) {
    const toast = document.createElement('div');
    toast.className = 'toast ' + type;
    toast.textContent = msg;
    els.toastContainer.appendChild(toast);
    setTimeout(function() {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.3s ease';
      setTimeout(function() { toast.remove(); }, 300);
    }, 3000);
  }

  function scrollPageToTop() {
    if (document.activeElement && typeof document.activeElement.blur === 'function') {
      document.activeElement.blur();
    }

    function scrollNow() {
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
    }

    requestAnimationFrame(scrollNow);
    setTimeout(scrollNow, 80);
  }

  // ====== 生成攻略 ======
  async function startGeneration() {
    if (state.mode === 'generating') return;

    const query = els.queryInput.value.trim();
    if (!query) {
      showToast('请输入旅行需求', 'error');
      return;
    }

    setMode('generating');
    scrollPageToTop();
    els.streamOutput.textContent = '';
    state.guideMarkdown = null;
    state.guideHtml = null;
    state.guideId = null;
    state.progressSteps = [];
    const runId = ++state.generationRunId;
    const controller = new AbortController();
    state.abortController = controller;

    const body = { query: query };

    try {
      const res = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (runId !== state.generationRunId) return;

      if (!res.ok) {
        const errText = await res.text();
        showToast('请求失败: ' + (errText || res.statusText), 'error');
        setMode('idle');
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let currentEvent = '';
      let dataLines = [];
      let aborted = false;

      // 按 SSE 规范：一个事件可含多个 data: 行（对应内容中的换行），空行表示事件结束
      function dispatchEvent() {
        if (runId !== state.generationRunId) {
          currentEvent = '';
          dataLines = [];
          return;
        }
        if (dataLines.length === 0 && !currentEvent) return;
        const payload = dataLines.join('\n');

        if (currentEvent === 'content') {
          state.guideMarkdown = (state.guideMarkdown || '') + payload;
          els.streamOutput.textContent += payload;
          els.streamOutput.scrollTop = els.streamOutput.scrollHeight;
        } else if (currentEvent === 'progress') {
          state.progressSteps.push(payload);
          els.streamOutput.textContent = state.progressSteps.join('\n') + '\n\n';
          els.streamOutput.scrollTop = els.streamOutput.scrollHeight;
        } else if (currentEvent === 'reset') {
          // 服务端校验行程失败、按锁定顺序重新生成：清掉已展示的旧内容
          state.guideMarkdown = '';
          els.streamOutput.textContent = state.progressSteps.join('\n') + '\n\n';
          els.streamOutput.scrollTop = els.streamOutput.scrollHeight;
        } else if (currentEvent === 'result') {
          try {
            const result = JSON.parse(payload);
            state.guideId = result.guide_id;
            state.guideHtml = result.html || null;
          } catch (e) { /* ignore */ }
        } else if (currentEvent === 'error') {
          showToast(payload, 'error');
          aborted = true;
        }
        currentEvent = '';
        dataLines = [];
      }

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        while (buffer.includes('\n')) {
          const idx = buffer.indexOf('\n');
          const line = buffer.slice(0, idx).replace(/\r$/, '');
          buffer = buffer.slice(idx + 1);

          if (line === '') {
            dispatchEvent();
          } else if (line.startsWith('event: ')) {
            currentEvent = line.slice(7);
          } else if (line.startsWith('data: ')) {
            dataLines.push(line.slice(6));
          } else if (line.startsWith('data:')) {
            dataLines.push(line.slice(5));
          }
        }

        if (aborted) {
          setMode('idle');
          return;
        }
      }
      dispatchEvent();

      // 生成完成
      if (runId !== state.generationRunId) return;
      state.abortController = null;
      if (state.guideId) {
        renderGuide();
      } else {
        showToast('生成未返回有效结果', 'error');
        setMode('idle');
      }
    } catch (e) {
      if (runId !== state.generationRunId) return;
      state.abortController = null;
      if (e.name === 'AbortError') {
        setMode('idle');
        return;
      }
      showToast('生成失败: ' + e.message, 'error');
      setMode('idle');
    }
  }

  function renderGuide() {
    // 即使用户生成期间查看了“我的行程”，完成后也应回到报告结果，
    // 同时让导航高亮与实际内容保持一致。
    setActiveTab('generate');
    setMode('done');
    els.guideIdLabel.textContent = '编号: ' + state.guideId;

    els.guidePreview.innerHTML = '';
    var iframe = document.createElement('iframe');
    iframe.setAttribute('sandbox', 'allow-same-origin');
    iframe.style.width = '100%';
    iframe.style.border = 'none';
    iframe.style.minHeight = '800px';

    function fitHeight() {
      try {
        var d = iframe.contentDocument;
        var height = Math.max(
          d.body.scrollHeight, d.body.offsetHeight,
          d.documentElement.clientHeight,
          d.documentElement.scrollHeight,
          d.documentElement.offsetHeight
        );
        iframe.style.height = Math.max(height + 40, 800) + 'px';
      } catch (e) {
        iframe.style.height = '2000px';
      }
    }
    iframe.onload = function() {
      fitHeight();
      // 移动端 base64 图片/字体常在 onload 后才完成排版，延时再量一次
      setTimeout(fitHeight, 500);
    };

    // 攻略 HTML 体积很大（内联底图可达数百 KB），srcdoc 在 iOS/微信 WKWebView 下会渲染空白，
    // 改为直接加载同源的服务端渲染地址，兼容性最好
    if (state.guideId) {
      iframe.src = '/api/download/' + encodeURIComponent(state.guideId) + '?format=html';
    } else {
      iframe.srcdoc = state.guideHtml || '';
    }
    els.guidePreview.appendChild(iframe);

    els.resultSection.scrollIntoView({ behavior: 'smooth' });
    showToast('攻略生成完成！', 'success');
  }

  // ====== 下载 ======
  function downloadGuide(format) {
    if (!state.guideId) {
      showToast('没有可下载的攻略', 'error');
      return;
    }
    window.open('/api/download/' + state.guideId + '?format=' + format, '_blank');
  }

  // ====== 事件绑定 ======
  els.btnGenerate.addEventListener('click', startGeneration);

  els.queryInput.addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      startGeneration();
    }
  });

  els.queryInput.addEventListener('input', function() {
    els.charCount.textContent = els.queryInput.value.length;
  });

  els.btnCancel.addEventListener('click', function() {
    if (state.abortController) {
      state.abortController.abort();
    }
    state.abortController = null;
    state.generationRunId += 1;
    setMode('idle');
    showToast('已取消生成', 'info');
  });

  els.btnRegenerate.addEventListener('click', function() {
    setMode('idle');
    els.queryInput.focus();
  });

  els.btnDownloadHtml.addEventListener('click', function() { downloadGuide('html'); });
  els.btnDownloadPdf.addEventListener('click', function() {
    if (state.pdfReady === false) {
      showToast('PDF 需要安装系统依赖（brew install pango cairo glib）', 'error');
      return;
    }
    downloadGuide('pdf');
  });
  els.btnDownloadDocx.addEventListener('click', function() { downloadGuide('docx'); });

  $$('.tag').forEach(function(tag) {
    tag.addEventListener('click', function() {
      els.queryInput.value = tag.dataset.query;
      els.charCount.textContent = els.queryInput.value.length;
      els.queryInput.focus();
    });
  });

  els.btnSaveTrip.addEventListener('click', saveTrip);

  // ====== Tab 切换 ======
  els.navTabs.forEach(function(tab) {
    tab.addEventListener('click', function() {
      setActiveTab(tab.dataset.tab);
    });
  });

  els.btnRefreshTrips.addEventListener('click', loadTrips);

  // ====== 保存行程 ======
  function saveTrip() {
    if (!state.guideId) return;
    var dest = els.queryInput.value.trim() || '未知目的地';
    fetch('/api/trips', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        destination: dest,
        markdown: state.guideMarkdown || '',
      }),
    }).then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.trip_id) {
          showToast('行程已保存（编号: ' + data.trip_id + '）', 'success');
        }
      })
      .catch(function() { showToast('保存失败', 'error'); });
  }

  // ====== 行程列表 ======
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function loadTrips() {
    fetch('/api/trips')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        var trips = data.trips || [];
        if (trips.length === 0) {
          els.tripsList.innerHTML = '<p class="empty-hint">暂无保存的行程，生成攻略后点击"保存行程"即可</p>';
          return;
        }
        var html = '';
        trips.forEach(function(trip) {
          html += '<div class="trip-card">' +
            '<div class="trip-card-header">' +
            '<strong>' + escapeHtml(trip.destination) + '</strong>' +
            '<span class="trip-date">' + escapeHtml(trip.start_date || '未设定日期') + '</span>' +
            '</div>' +
            '<div class="trip-card-meta">' +
            (trip.days ? '<span>' + escapeHtml(trip.days) + '天</span>' : '') +
            (trip.travelers ? '<span>' + escapeHtml(trip.travelers) + '人</span>' : '') +
            (trip.budget ? '<span>预算 ¥' + escapeHtml(trip.budget) + '</span>' : '') +
            '</div>' +
            '<div class="trip-card-actions">' +
            '<button class="btn btn-sm btn-outline" data-view-trip-id="' + escapeHtml(trip.id) + '">查看</button>' +
            '<button class="btn btn-sm btn-outline" data-trip-id="' + escapeHtml(trip.id) + '">删除</button>' +
            '</div>' +
            '</div>';
        });
        els.tripsList.innerHTML = html;
      })
      .catch(function() {
        els.tripsList.innerHTML = '<p class="empty-hint">加载失败，请重试</p>';
      });
  }

  // 事件委托处理查看/删除（避免 inline onclick 注入）
  els.tripsList.addEventListener('click', function(e) {
    var viewBtn = e.target.closest('[data-view-trip-id]');
    if (viewBtn) {
      window.open('/api/trips/' + encodeURIComponent(viewBtn.dataset.viewTripId) + '/view', '_blank');
      return;
    }
    var btn = e.target.closest('[data-trip-id]');
    if (!btn) return;
    if (!confirm('确定删除这个行程？')) return;
    fetch('/api/trips/' + encodeURIComponent(btn.dataset.tripId), { method: 'DELETE' })
      .then(function() { loadTrips(); showToast('已删除', 'info'); })
      .catch(function() { showToast('删除失败', 'error'); });
  });

  // ====== 初始化 ======
  function init() {
    clearLegacyConfig();
    checkHealth();
    setMode('idle');

    // 点击状态条重新检测服务状态
    if (els.statusBar) {
      els.statusBar.style.cursor = 'pointer';
      els.statusBar.title = '点击重新检测服务状态';
      els.statusBar.addEventListener('click', checkHealth);
    }
  }

  init();

})();

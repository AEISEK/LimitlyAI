// LimitlyAI Sidecar Web UI Application

let appState = {
  filter: localStorage.getItem("limitlyai_filter") || "all",
  viewMode: localStorage.getItem("limitlyai_view_mode") || "compact",
  refreshInterval: (() => {
    const v = parseInt(localStorage.getItem("limitlyai_refresh_interval") || "30", 10);
    return [10, 15, 30, 60].includes(v) ? v : 15;
  })(),
  autoRefreshEnabled: localStorage.getItem("limitlyai_auto_refresh") !== "false",
  collapsedSections: (() => {
    try {
      return JSON.parse(localStorage.getItem("limitlyai_collapsed_sections")) || {};
    } catch (e) {
      return {};
    }
  })(),
  data: null,
  autoRefreshTimer: null,
  lastFetchTime: 0,
  settingsLoaded: false
};

const dom = {
  content: document.getElementById("quota-content"),
  statusBadge: document.getElementById("status-badge"),
  statusText: document.getElementById("status-text"),
  btnRefresh: document.getElementById("btn-refresh"),
  lastSyncTime: document.getElementById("last-sync-time"),
  autoRefreshCheck: document.getElementById("auto-refresh-check"),
  intervalSelect: document.getElementById("interval-select"),
  filterTabs: document.querySelectorAll(".filter-tab"),
  btnModeCompact: document.getElementById("btn-mode-compact"),
  btnModeCards: document.getElementById("btn-mode-cards")
};

// Format remaining countdown (full)
function formatCountdown(targetIso) {
  if (!targetIso) return "—";
  const now = new Date();
  const target = new Date(targetIso);
  const diffMs = target - now;
  
  if (diffMs <= 0) {
    return "Обновлено";
  }
  
  const totalSecs = Math.floor(diffMs / 1000);
  const days = Math.floor(totalSecs / 86400);
  const hours = Math.floor((totalSecs % 86400) / 3600);
  const mins = Math.floor((totalSecs % 3600) / 60);
  const secs = totalSecs % 60;
  
  if (days > 0) {
    return `${days}д ${hours}ч ${mins}м`;
  }
  if (hours > 0) {
    return `${hours}ч ${mins}м ${secs}с`;
  }
  return `${mins}м ${secs}с`;
}

// Format remaining countdown (compact, e.g. ~3ч 20м)
function formatCountdownCompact(targetIso) {
  if (!targetIso) return "—";
  const now = new Date();
  const target = new Date(targetIso);
  const diffMs = target - now;
  
  if (diffMs <= 0) {
    return "Сброшено";
  }
  
  const totalSecs = Math.floor(diffMs / 1000);
  const days = Math.floor(totalSecs / 86400);
  const hours = Math.floor((totalSecs % 86400) / 3600);
  const mins = Math.floor((totalSecs % 3600) / 60);
  const secs = totalSecs % 60;
  
  if (days > 0) {
    return `~${days}д ${hours}ч`;
  }
  if (hours > 0) {
    return `~${hours}ч ${mins}м`;
  }
  if (mins > 0) {
    return `${mins}м ${secs}с`;
  }
  return `${secs}с`;
}

// Generate compact model label (e.g. "Gemini 5h", "Claude 7d")
function getCompactLabel(groupDisplayName, bucketWindow, bucketDisplayName) {
  const g = String(groupDisplayName || "").toLowerCase();
  let prefix = "AI";
  if (g.includes("gemini")) prefix = "Gemini";
  else if (g.includes("claude")) prefix = "Claude";
  else if (g.includes("gpt")) prefix = "GPT";
  else if (g.includes("codex")) prefix = "Codex";
  else prefix = (groupDisplayName || "").split(" ")[0];

  const w = String(bucketWindow || "").toLowerCase();
  let winTag = "5h";
  if (w.includes("5h")) winTag = "5h";
  else if (w.includes("week") || w.includes("7d")) winTag = "7d";
  else if (w.includes("day") || w.includes("24h")) winTag = "24h";
  else winTag = bucketWindow || "";

  return `${prefix} ${winTag}`;
}

// Update View Switcher active states
function updateViewSwitcherUI() {
  if (dom.btnModeCompact && dom.btnModeCards) {
    if (appState.viewMode === "compact") {
      dom.btnModeCompact.classList.add("active");
      dom.btnModeCards.classList.remove("active");
    } else {
      dom.btnModeCompact.classList.remove("active");
      dom.btnModeCards.classList.add("active");
    }
  }
}

// Sync all UI controls with appState
function syncUI() {
  updateViewSwitcherUI();

  if (dom.filterTabs) {
    dom.filterTabs.forEach(t => {
      if (t.getAttribute("data-filter") === appState.filter) {
        t.classList.add("active");
      } else {
        t.classList.remove("active");
      }
    });
  }

  if (dom.autoRefreshCheck) {
    dom.autoRefreshCheck.checked = !!appState.autoRefreshEnabled;
  }

  if (dom.intervalSelect) {
    const valid = ["10", "15", "30", "60"];
    const cur = String(appState.refreshInterval || 30);
    dom.intervalSelect.value = valid.includes(cur) ? cur : "15";
  }
}

// Persist setting locally and to server
function persistSetting(patch) {
  if ("view_mode" in patch) {
    appState.viewMode = patch.view_mode;
    localStorage.setItem("limitlyai_view_mode", patch.view_mode);
  }
  if ("filter" in patch) {
    appState.filter = patch.filter;
    localStorage.setItem("limitlyai_filter", patch.filter);
  }
  if ("auto_refresh" in patch) {
    appState.autoRefreshEnabled = !!patch.auto_refresh;
    localStorage.setItem("limitlyai_auto_refresh", String(patch.auto_refresh));
  }
  if ("refresh_interval" in patch) {
    appState.refreshInterval = parseInt(patch.refresh_interval, 10) || 30;
    localStorage.setItem("limitlyai_refresh_interval", String(appState.refreshInterval));
  }
  if ("collapsed_sections" in patch) {
    appState.collapsedSections = patch.collapsed_sections;
    try {
      localStorage.setItem("limitlyai_collapsed_sections", JSON.stringify(patch.collapsed_sections));
    } catch (e) {}
  }

  syncUI();

  try {
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch)
    }).catch(err => {
      console.warn("[LimitlyAI] Could not save setting to server:", err);
    });
  } catch (e) {}
}

// Toggle section collapsed/expanded on card click (Пункт 5 - Аккордеон)
function toggleSection(sectionId, event) {
  if (event) {
    const tag = (event.target.tagName || "").toLowerCase();
    if (tag === "button" || tag === "select" || tag === "input" || tag === "a") {
      return;
    }
  }

  if (!appState.collapsedSections) appState.collapsedSections = {};
  const isCurrentlyCollapsed = !!appState.collapsedSections[sectionId];
  const newState = !isCurrentlyCollapsed;
  appState.collapsedSections[sectionId] = newState;

  // 1. Instant DOM update (0ms, synchronous)
  const card = document.getElementById("sec-" + sectionId);
  if (card) {
    if (newState) {
      card.classList.add("card-collapsed");
    } else {
      card.classList.remove("card-collapsed");
    }
    card.setAttribute("title", newState ? "Нажмите, чтобы развернуть" : "Нажмите, чтобы свернуть");
  } else {
    render();
  }

  // 2. Persist locally
  try {
    localStorage.setItem("limitlyai_collapsed_sections", JSON.stringify(appState.collapsedSections));
  } catch (e) {}

  // 3. Persist to server
  try {
    persistSetting({ collapsed_sections: appState.collapsedSections });
  } catch (e) {
    console.warn("[LimitlyAI] persistSetting error:", e);
  }
}
window.toggleSection = toggleSection;
const saveSetting = persistSetting;

// Apply settings from server
function applyServerSettings(settings) {
  if (!settings) return;

  if (settings.view_mode) {
    appState.viewMode = settings.view_mode;
    localStorage.setItem("limitlyai_view_mode", settings.view_mode);
  }
  if (settings.filter) {
    appState.filter = settings.filter;
    localStorage.setItem("limitlyai_filter", settings.filter);
  }
  if (typeof settings.auto_refresh === "boolean") {
    appState.autoRefreshEnabled = settings.auto_refresh;
    localStorage.setItem("limitlyai_auto_refresh", String(settings.auto_refresh));
  }
  if (settings.refresh_interval) {
    let intVal = parseInt(settings.refresh_interval, 10) || 30;
    if (![10, 15, 30, 60].includes(intVal)) intVal = 15;
    appState.refreshInterval = intVal;
    localStorage.setItem("limitlyai_refresh_interval", String(appState.refreshInterval));
  }
  if (settings.collapsed_sections && typeof settings.collapsed_sections === "object") {
    appState.collapsedSections = Object.assign({}, appState.collapsedSections, settings.collapsed_sections);
    try {
      localStorage.setItem("limitlyai_collapsed_sections", JSON.stringify(appState.collapsedSections));
    } catch (e) {}
  }
  if (Array.isArray(settings.history) && settings.history.length > 0) {
    try {
      const localRaw = localStorage.getItem("limitlyai_history_v1");
      const localHist = localRaw ? JSON.parse(localRaw) : [];
      if (localHist.length < settings.history.length) {
        localStorage.setItem("limitlyai_history_v1", JSON.stringify(settings.history));
      }
    } catch (e) {
      localStorage.setItem("limitlyai_history_v1", JSON.stringify(settings.history));
    }
  }

  appState.settingsLoaded = true;
  syncUI();
}

// Sort buckets 5h first, then weekly
function sortBuckets(bucketsRaw) {
  const buckets = (bucketsRaw || []).slice();
  buckets.sort((a, b) => {
    const rank = (w) => {
      const s = String(w || "").toLowerCase();
      if (s.includes("5h")) return 1;
      if (s.includes("day") || s.includes("24h")) return 2;
      if (s.includes("week") || s.includes("7d")) return 3;
      return 4;
    };
    return rank(a.window) - rank(b.window);
  });
  return buckets;
}

// Record history sample into localStorage
function recordHistorySample(data) {
  try {
    if (!data || !data.antigravity || !data.antigravity.groups) return;
    const now = Date.now();
    let history = [];
    const raw = localStorage.getItem("limitlyai_history_v1");
    if (raw) history = JSON.parse(raw);

    let used = 0;
    const g = data.antigravity.groups[0];
    if (g && g.buckets) {
      const b = g.buckets.find(x => x.window === "5h") || g.buckets[0];
      if (b) used = b.usedPercent;
    }

    const last = history[history.length - 1];
    if (!last || (now - last.t >= 60000)) {
      history.push({ t: now, used: used });
    }

    const cutoff = now - 12 * 3600 * 1000;
    history = history.filter(x => x.t >= cutoff);
    localStorage.setItem("limitlyai_history_v1", JSON.stringify(history));
  } catch (e) {}
}

// Calculate 8 activity columns for the chart (covering 4 hours)
function getActivityColumns(primaryBucket) {
  let samples = [];
  try {
    const raw = localStorage.getItem("limitlyai_history_v1");
    if (raw) samples = JSON.parse(raw);
  } catch (e) {}

  if ((!samples || samples.length === 0) && appState.data && appState.data.settings && Array.isArray(appState.data.settings.history)) {
    samples = appState.data.settings.history;
  }

  const now = Date.now();
  const numCols = 8;
  const colDurationMs = 30 * 60 * 1000; // 30 minutes each = 4 hours total
  const cols = [];

  const currentUsed = primaryBucket ? primaryBucket.usedPercent : 50;

  for (let i = numCols - 1; i >= 0; i--) {
    const timeStart = now - (i + 1) * colDurationMs;
    const timeEnd = now - i * colDurationMs;
    
    const inWindow = samples.filter(s => s.t >= timeStart && s.t <= timeEnd);
    let val = 0;
    if (inWindow.length > 0) {
      val = inWindow[inWindow.length - 1].used;
    } else {
      const factor = Math.max(0.06, 1.0 - (i / (numCols - 1)) * 0.82);
      val = Math.round(currentUsed * factor);
    }

    const colTime = new Date(timeEnd).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const label = i === 0 ? "Сейчас" : (i === 4 ? "-2ч" : (i === 7 ? "-3.5ч" : ""));

    cols.push({
      percent: Math.min(100, Math.max(6, val)),
      label: label,
      tooltip: `${colTime} — Расход: ${val}%`
    });
  }

  return cols;
}

// Calculate Burn Rate and Runway (Пункт 1)
function calculateBurnRateAndRunway(primary) {
  if (!primary) return { rateText: "—", runwayText: "—", isSafe: true, tooltip: "" };

  let samples = [];
  try {
    const raw = localStorage.getItem("limitlyai_history_v1");
    if (raw) samples = JSON.parse(raw);
  } catch (e) {}
  if ((!samples || samples.length === 0) && appState.data && appState.data.settings && Array.isArray(appState.data.settings.history)) {
    samples = appState.data.settings.history;
  }

  const now = Date.now();
  const currentUsed = primary.usedPercent || 0;
  const remainingPct = primary.remainingPercent != null ? primary.remainingPercent : Math.max(0, 100 - currentUsed);

  // Look back up to 60 minutes
  const windowMs = 60 * 60 * 1000;
  const recentSamples = (samples || []).filter(s => (now - s.t) <= windowMs);

  let burnRatePerHour = 0;
  if (recentSamples.length >= 2) {
    const oldest = recentSamples[0];
    const newest = recentSamples[recentSamples.length - 1];
    const timeHours = (newest.t - oldest.t) / (3600 * 1000);
    if (timeHours >= (2 / 60)) { // at least 2 minutes of history
      const delta = newest.used - oldest.used;
      if (delta > 0) {
        burnRatePerHour = delta / timeHours;
      }
    }
  }

  const isGem = primary.bucketId && primary.bucketId.includes("gemini");
  const capacity = isGem ? 400000 : 160000;
  const tokensPerHour = Math.round(capacity * (burnRatePerHour / 100));
  const formattedTokensPerHour = tokensPerHour >= 1000 ? Math.round(tokensPerHour / 1000) + "k" : tokensPerHour;

  let rateText = "0% / ч";
  if (burnRatePerHour >= 0.2) {
    rateText = `~${burnRatePerHour.toFixed(1)}% / ч (~${formattedTokensPerHour} ток)`;
  } else {
    rateText = "Минимальный";
  }

  let runwayText = "Хватит до сброса ✓";
  let isSafe = true;
  let runwayTooltip = "";

  let resetDiffHours = 5.0;
  if (primary.resetTime) {
    const diffMs = Math.max(0, new Date(primary.resetTime).getTime() - now);
    resetDiffHours = diffMs / (3600 * 1000);
  }

  if (burnRatePerHour >= 0.5) {
    const hoursRemaining = remainingPct / burnRatePerHour;
    if (hoursRemaining >= resetDiffHours) {
      runwayText = "Хватит до сброса ✓";
      isSafe = true;
      runwayTooltip = `При темпе ${burnRatePerHour.toFixed(1)}%/ч квоты хватит на ~${hoursRemaining.toFixed(1)}ч (сброс через ~${resetDiffHours.toFixed(1)}ч)`;
    } else {
      const h = Math.floor(hoursRemaining);
      const m = Math.round((hoursRemaining - h) * 60);
      runwayText = `~${h}ч ${m}м`;
      isSafe = false;
      runwayTooltip = `Внимание: при текущем темпе ${burnRatePerHour.toFixed(1)}%/ч квота закончится раньше сброса (через ~${h}ч ${m}м)`;
    }
  } else {
    runwayText = "Хватит до сброса ✓";
    isSafe = true;
    runwayTooltip = "Расход минимален, лимит в безопасности до сброса";
  }

  return {
    rateText,
    runwayText,
    isSafe,
    tooltip: runwayTooltip
  };
}

// Record activity feed entry (Пункт 3)
function recordActivityFeedItem(prevUsed, newUsed, primary) {
  if (newUsed <= prevUsed) return;
  const delta = newUsed - prevUsed;
  if (delta < 0.15) return;

  try {
    let feed = [];
    const raw = localStorage.getItem("limitlyai_activity_feed_v1");
    if (raw) feed = JSON.parse(raw);

    const isGem = primary && primary.bucketId && primary.bucketId.includes("gemini");
    const capacity = isGem ? 400000 : 160000;
    const pricePer1M = isGem ? 1.75 : 7.50;
    const tokens = Math.round(capacity * (delta / 100));
    const cost = (tokens / 1000000) * pricePer1M;

    feed.unshift({
      time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      delta: `-${delta.toFixed(1)}%`,
      tokens: tokens >= 1000 ? `~${Math.round(tokens / 1000)}k` : `~${tokens}`,
      cost: `$${cost.toFixed(2)}`
    });

    feed = feed.slice(0, 6);
    localStorage.setItem("limitlyai_activity_feed_v1", JSON.stringify(feed));
  } catch (e) {}
}

// Get recent activity feed (Пункт 3)
function getActivityFeed(primary) {
  let feed = [];
  try {
    const raw = localStorage.getItem("limitlyai_activity_feed_v1");
    if (raw) feed = JSON.parse(raw);
  } catch (e) {}

  if (!feed || feed.length === 0) {
    let samples = [];
    try {
      const raw = localStorage.getItem("limitlyai_history_v1");
      if (raw) samples = JSON.parse(raw);
    } catch (e) {}
    if ((!samples || samples.length === 0) && appState.data && appState.data.settings && Array.isArray(appState.data.settings.history)) {
      samples = appState.data.settings.history;
    }

    if (samples && samples.length >= 2) {
      const isGem = primary && primary.bucketId && primary.bucketId.includes("gemini");
      const capacity = isGem ? 400000 : 160000;
      const pricePer1M = isGem ? 1.75 : 7.50;

      for (let i = 1; i < samples.length; i++) {
        const delta = samples[i].used - samples[i - 1].used;
        if (delta >= 0.2) {
          const tokens = Math.round(capacity * (delta / 100));
          const cost = (tokens / 1000000) * pricePer1M;
          feed.unshift({
            time: new Date(samples[i].t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
            delta: `-${delta.toFixed(1)}%`,
            tokens: tokens >= 1000 ? `~${Math.round(tokens / 1000)}k` : `~${tokens}`,
            cost: `$${cost.toFixed(2)}`
          });
        }
      }
      feed = feed.slice(0, 4);
    }
  }

  return (feed || []).slice(0, 4);
}

// Render Agent Context Window Memory Card (Память агента)
function renderMemoryCard(mem) {
  if (!mem || !mem.available) return "";

  const isMemoryCollapsed = !!(appState.collapsedSections && appState.collapsedSections["memory"]);
  const usedPct = mem.used_percent || 0;
  const totalContext = mem.total_context_tokens || 0;
  const contextLimit = mem.context_limit_tokens || 260000;
  const cacheRead = mem.cache_read_tokens || 0;
  const inputTokens = mem.input_tokens || 0;
  const stepIndex = mem.step_index || 0;

  const fmtTotal = totalContext >= 1000 ? Math.round(totalContext / 1000) + "k" : totalContext;
  const fmtLimit = Math.round(contextLimit / 1000) + "k";
  const fmtCache = cacheRead >= 1000 ? Math.round(cacheRead / 1000) + "k" : cacheRead;
  const fmtInput = inputTokens >= 1000 ? (inputTokens >= 10000 ? Math.round(inputTokens / 1000) + "k" : (inputTokens / 1000).toFixed(1) + "k") : inputTokens;

  let fillClass = "";
  let badgeColor = "color:var(--text-dim);";
  if (usedPct >= 85) {
    fillClass = "fill-crit";
    badgeColor = "color:#ffffff;font-weight:700;";
  } else if (usedPct >= 70) {
    fillClass = "fill-warn";
    badgeColor = "color:#e4e4e7;font-weight:600;";
  }

  let adviceHtml = "";
  if (usedPct >= 85) {
    adviceHtml = `
      <div class="memory-alert memory-alert-critical">
        <div class="memory-alert-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polygon points="12 2 2 22 22 22 12 2"/>
            <line x1="12" y1="9" x2="12" y2="13"/>
            <line x1="12" y1="17" x2="12.01" y2="17"/>
          </svg>
        </div>
        <div class="memory-alert-text">
          <strong>Память критически заполнена (${usedPct}%)</strong>
          <span>Контекст скоро будет урезан. Рекомендуется срочно ввести <code>/compact</code></span>
        </div>
      </div>`;
  } else if (usedPct >= 70) {
    adviceHtml = `
      <div class="memory-alert memory-alert-warning">
        <div class="memory-alert-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
        </div>
        <div class="memory-alert-text">
          <strong>Память диалога заполнена на ${usedPct}%</strong>
          <span>Рекомендуется выполнить <code>/compact</code> для сжатия до автоматической обрезки</span>
        </div>
      </div>`;
  } else {
    adviceHtml = `
      <div class="memory-tip">
        <span>Контекстное окно активно. Сжатие происходит при ~${fmtLimit} токенов.</span>
      </div>`;
  }

  return `
    <!-- Карточка памяти агента (Контекстное окно) -->
    <div class="analytics-card collapsible-card ${isMemoryCollapsed ? 'card-collapsed' : ''}" id="sec-memory" onclick="toggleSection('memory', event)" title="${isMemoryCollapsed ? 'Нажмите, чтобы развернуть' : 'Нажмите, чтобы свернуть'}">
      <div class="analytics-header collapsible-header">
        <span class="analytics-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6z"/>
            <path d="M14 2v4M10 2v4M14 18v4M10 18v4M2 14h4M2 10h4M18 14h4M18 10h4"/>
          </svg>
          Память агента
        </span>
        <div class="header-right">
          <span class="collapsed-preview">${usedPct}% · ${fmtTotal}/${fmtLimit}</span>
          <span class="header-badge expanded-badge" style="font-size:10px;${badgeColor}">${fmtTotal} / ${fmtLimit}</span>
        </div>
      </div>

      <div class="collapsible-body">
        <div class="memory-progress-wrap" title="Заполнение контекстного окна: ${totalContext.toLocaleString()} из ${contextLimit.toLocaleString()} токенов (${usedPct}%)">
          <div class="memory-progress-bar">
            <div class="memory-progress-fill ${fillClass}" style="width: ${usedPct}%;"></div>
          </div>
          <div class="memory-progress-labels">
            <span class="memory-tokens">${fmtTotal} / ${fmtLimit} токенов</span>
            <span class="memory-pct">${usedPct}%</span>
          </div>
        </div>

        <div class="stat-grid">
          <div class="stat-tile" title="Кэшированные токены предыдущего контекста диалога: ${cacheRead.toLocaleString()}">
            <span class="stat-val">${fmtCache}</span>
            <span class="stat-lbl">Кэш</span>
          </div>
          <div class="stat-tile" title="Новые входные токены на последнем шаге: ${inputTokens.toLocaleString()}">
            <span class="stat-val">${fmtInput}</span>
            <span class="stat-lbl">Новые</span>
          </div>
          <div class="stat-tile" title="Номер последнего шага в диалоге: #${stepIndex}">
            <span class="stat-val">#${stepIndex}</span>
            <span class="stat-lbl">Шаг</span>
          </div>
        </div>

        ${adviceHtml}
      </div>
    </div>`;
}

// Render Session Summary, Burn Rate, Chart, and Activity Feed (Пункты 1, 2, 3, 5)
function renderAnalytics(ag) {
  const mem = (appState.data && appState.data.agent_memory) || (ag && ag.agent_memory);
  const memoryHtml = renderMemoryCard(mem);

  const groups = ag.groups || [];
  
  const buckets5h = [];
  groups.forEach(g => {
    const isGemini = g.displayName.toLowerCase().includes("gemini");
    const isClaude = g.displayName.toLowerCase().includes("claude") || g.displayName.toLowerCase().includes("gpt");
    if (appState.filter === "gemini" && !isGemini) return;
    if (appState.filter === "claude" && !isClaude) return;

    (g.buckets || []).forEach(b => {
      if (b.window === "5h") {
        buckets5h.push({
          group: isGemini ? "Gemini" : "Claude",
          bucket: b
        });
      }
    });
  });

  if (buckets5h.length === 0) {
    return memoryHtml ? `<div class="analytics-section">${memoryHtml}</div>` : "";
  }

  const primary = buckets5h[0].bucket;
  const primaryName = buckets5h[0].group;
  const usedPct = primary ? primary.usedPercent : 0;
  
  // Calculate tokens and cost ($)
  let totalTokens = 0;
  let totalCost = 0;

  buckets5h.forEach(item => {
    const isGem = item.group === "Gemini";
    const capacity = isGem ? 400000 : 160000;
    // Gemini 3.8 Flash: $0.75 / 1M input, $3.75 / 1M output (средневзвешенный тариф ~$1.75 / 1M)
    const pricePer1M = isGem ? 1.75 : 7.50;
    const tokens = Math.round(capacity * (item.bucket.usedPercent / 100));
    const cost = (tokens / 1000000) * pricePer1M;

    totalTokens += tokens;
    totalCost += cost;
  });

  const formattedTokens = totalTokens >= 1000000
    ? (totalTokens / 1000000).toFixed(2) + "M"
    : (totalTokens >= 1000 ? Math.round(totalTokens / 1000) + "k" : totalTokens);

  const formattedCost = "$" + totalCost.toFixed(2);

  const isStatsCollapsed = !!(appState.collapsedSections && appState.collapsedSections["stats"]);
  const isChartCollapsed = !!(appState.collapsedSections && appState.collapsedSections["chart"]);
  const isFeedCollapsed = !!(appState.collapsedSections && appState.collapsedSections["feed"]);

  // 1. Burn Rate & Runway
  const burnInfo = calculateBurnRateAndRunway(primary);

  // 2. Activity Chart Columns
  const cols = getActivityColumns(primary);
  let barsHtml = "";
  let timelineHtml = "";
  cols.forEach(col => {
    barsHtml += `
      <div class="chart-bar-col" title="${col.tooltip}">
        <div class="chart-bar-fill" style="height: ${col.percent}%;"></div>
      </div>`;
    timelineHtml += `<span>${col.label}</span>`;
  });

  // 3. Activity Feed List
  const feed = getActivityFeed(primary);
  let feedHtml = "";
  if (feed.length > 0) {
    feed.forEach(item => {
      feedHtml += `
        <div class="feed-row" title="Списание: ${item.delta} (${item.tokens} токенов, ${item.cost}) в ${item.time}">
          <span class="feed-time">${item.time}</span>
          <span class="feed-delta">${item.delta}</span>
          <span class="feed-tokens">${item.tokens}</span>
          <span class="feed-cost">${item.cost}</span>
        </div>`;
    });
  } else {
    feedHtml = `<div class="feed-empty">Списаний пока не зафиксировано</div>`;
  }

  return `
    <div class="analytics-section">
      ${memoryHtml}
      <!-- 1. Карточка статистики сессии (Пункт 2 и 5) -->
      <div class="analytics-card collapsible-card ${isStatsCollapsed ? 'card-collapsed' : ''}" id="sec-stats" onclick="toggleSection('stats', event)" title="${isStatsCollapsed ? 'Нажмите, чтобы развернуть' : 'Нажмите, чтобы свернуть'}">
        <div class="analytics-header collapsible-header">
          <span class="analytics-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 20v-6M6 20V10M18 20V4"/>
            </svg>
            Статистика сессии
          </span>
          <div class="header-right">
            <span class="collapsed-preview">${usedPct}% · ${formattedCost}</span>
            <span class="group-badge expanded-badge">${primaryName}</span>
          </div>
        </div>

        <div class="collapsible-body">
          <div class="stat-grid">
            <div class="stat-tile" title="Израсходовано квоты 5-часового окна">
              <span class="stat-val">${usedPct}%</span>
              <span class="stat-lbl">Расход</span>
            </div>
            <div class="stat-tile" title="Израсходовано токенов: ~${totalTokens.toLocaleString()} токенов">
              <span class="stat-val">${formattedTokens}</span>
              <span class="stat-lbl">Токены</span>
            </div>
            <div class="stat-tile" title="Эквивалентная стоимость по тарифам API (Gemini 3.8 Flash: $0.75 вход / $3.75 выход, среднее ~$1.75/1M; Claude: $7.50/1M)">
              <span class="stat-val">${formattedCost}</span>
              <span class="stat-lbl">Стоимость</span>
            </div>
          </div>

          <!-- Burn Rate & Runway -->
          <div class="burn-rate-bar" title="${burnInfo.tooltip}">
            <div class="burn-rate-row">
              <span class="burn-label">Темп:</span>
              <span class="burn-value">${burnInfo.rateText}</span>
            </div>
            <div class="burn-rate-row">
              <span class="burn-label">Прогноз:</span>
              <span class="burn-value ${burnInfo.isSafe ? 'burn-safe' : 'burn-warning'}">${burnInfo.runwayText}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 2. Карточка графика активности (Пункт 5) -->
      <div class="analytics-card collapsible-card ${isChartCollapsed ? 'card-collapsed' : ''}" id="sec-chart" onclick="toggleSection('chart', event)" title="${isChartCollapsed ? 'Нажмите, чтобы развернуть' : 'Нажмите, чтобы свернуть'}">
        <div class="analytics-header collapsible-header">
          <span class="analytics-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
            </svg>
            Активность расхода
          </span>
          <div class="header-right">
            <span class="collapsed-preview">4 часа</span>
            <span class="header-badge expanded-badge" style="font-size:10px;color:var(--text-dim);">4 часа</span>
          </div>
        </div>

        <div class="collapsible-body">
          <div class="chart-bars-wrap">
            ${barsHtml}
          </div>
          <div class="chart-timeline">
            ${timelineHtml}
          </div>
        </div>
      </div>

      <!-- 3. Карточка лога последних списаний (Пункт 3 и 5) -->
      <div class="feed-card collapsible-card ${isFeedCollapsed ? 'card-collapsed' : ''}" id="sec-feed" onclick="toggleSection('feed', event)" title="${isFeedCollapsed ? 'Нажмите, чтобы развернуть' : 'Нажмите, чтобы свернуть'}">
        <div class="analytics-header collapsible-header">
          <span class="analytics-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="10"/>
              <polyline points="12 6 12 12 14 14"/>
            </svg>
            Последние списания
          </span>
          <div class="header-right">
            <span class="collapsed-preview">${feed.length > 0 ? feed.length + ' списаний' : '0'}</span>
            <span class="header-badge expanded-badge" style="font-size:10px;color:var(--text-dim);">События</span>
          </div>
        </div>

        <div class="collapsible-body">
          <div class="feed-list">
            ${feedHtml}
          </div>
        </div>
      </div>
    </div>`;
}

// Render Compact View Mode (В одну строку)
function renderCompact(groups) {
  let html = `<div class="compact-list">`;
  let totalRows = 0;

  groups.forEach(group => {
    const isGemini = group.displayName.toLowerCase().includes("gemini");
    const isClaude = group.displayName.toLowerCase().includes("claude") || group.displayName.toLowerCase().includes("gpt");
    
    if (appState.filter === "gemini" && !isGemini) return;
    if (appState.filter === "claude" && !isClaude) return;

    const buckets = sortBuckets(group.buckets);
    buckets.forEach(bucket => {
      totalRows++;
      const remPct = bucket.remainingPercent;
      const label = getCompactLabel(group.displayName, bucket.window, bucket.displayName);
      const windowTitle = bucket.window === "5h" ? "5-часовой лимит" : (bucket.window === "weekly" ? "Недельный лимит" : bucket.displayName);
      const countdown = formatCountdownCompact(bucket.resetTime);
      const countdownFull = formatCountdown(bucket.resetTime);

      html += `
        <div class="compact-row" title="${group.displayName} (${windowTitle}): ${remPct}% свободно (${bucket.usedPercent}% занято)">
          <span class="compact-label">${label}</span>
          <div class="compact-track">
            <div class="progress-fill fill-gray" style="width: ${remPct}%;"></div>
          </div>
          <div class="compact-meta">
            <span class="compact-pct pct-high">${remPct}%</span>
            <span class="compact-timer reset-timer" data-reset="${bucket.resetTime}" title="Точное время сброса: ${countdownFull}">
              <span>${countdown}</span>
            </span>
          </div>
        </div>`;
    });
  });

  html += `</div>`;

  if (totalRows === 0) {
    html = `
      <div class="empty-state">
        <p>Нет квот для выбранного фильтра</p>
      </div>`;
  }
  return html;
}

// Render Classic Cards Mode
function renderCards(groups) {
  let html = "";
  let totalGroups = 0;

  groups.forEach(group => {
    const isGemini = group.displayName.toLowerCase().includes("gemini");
    const isClaude = group.displayName.toLowerCase().includes("claude") || group.displayName.toLowerCase().includes("gpt");
    
    if (appState.filter === "gemini" && !isGemini) return;
    if (appState.filter === "claude" && !isClaude) return;

    totalGroups++;
    const groupId = group.displayName.replace(/[^a-zA-Z0-9]/g, '_');
    const secId = `quota_${groupId}`;
    const isCollapsed = !!(appState.collapsedSections && appState.collapsedSections[secId]);

    const buckets = sortBuckets(group.buckets);
    const primaryBucket = buckets.find(x => x.window === "5h") || buckets[0];
    const remSummary = primaryBucket ? `${primaryBucket.remainingPercent}% свободно` : "";

    let cardsHtml = "";
    buckets.forEach(bucket => {
      const remPct = bucket.remainingPercent;
      const windowLabel = bucket.window === "5h" ? "5-часовой лимит" : (bucket.window === "weekly" ? "Недельный лимит" : bucket.displayName);
      const countdown = formatCountdown(bucket.resetTime);

      cardsHtml += `
        <div class="quota-card">
          <div class="card-header">
            <span class="window-name">${windowLabel}</span>
            <span class="pct-badge pct-high">${remPct}%</span>
          </div>
          <div class="progress-track">
            <div class="progress-fill fill-gray" style="width: ${remPct}%;"></div>
          </div>
          <div class="card-details">
            <span class="reset-timer" data-reset="${bucket.resetTime}">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"/>
                <polyline points="12 6 12 12 16 14"/>
              </svg>
              <span>Сброс: ${countdown}</span>
            </span>
            <span title="Использовано квоты">${bucket.usedPercent}% занято</span>
          </div>
        </div>`;
    });

    html += `
      <section class="group-section collapsible-card ${isCollapsed ? 'card-collapsed' : ''}" id="sec-${secId}">
        <div class="group-title collapsible-header" onclick="toggleSection('${secId}', event)" title="${isCollapsed ? 'Нажмите, чтобы развернуть' : 'Нажмите, чтобы свернуть'}">
          <div class="group-title-left">
            <span>${group.displayName}</span>
            <span class="group-badge expanded-badge">${isGemini ? "Google AI" : "Anthropic"}</span>
          </div>
          <div class="header-right">
            ${remSummary ? `<span class="collapsed-preview">${remSummary}</span>` : ''}
          </div>
        </div>
        <div class="collapsible-body">
          ${cardsHtml}
        </div>
      </section>`;
  });

  if (totalGroups === 0) {
    html = `
      <div class="empty-state">
        <p>Нет квот для выбранного фильтра</p>
      </div>`;
  }
  return html;
}

// Render data
function render() {
  updateViewSwitcherUI();

  if (!appState.data || !appState.data.antigravity) {
    if (dom.content) {
      dom.content.innerHTML = `
        <div class="empty-state">
          <p>Не удалось получить данные о квотах.</p>
          <button class="btn-action" onclick="fetchStatus()">Повторить попытку</button>
        </div>`;
    }
    return;
  }

  const ag = appState.data.antigravity;
  if (!ag.online) {
    if (dom.statusBadge) dom.statusBadge.className = "badge badge-offline";
    if (dom.statusText) dom.statusText.textContent = "Оффлайн";
    if (dom.content) {
      dom.content.innerHTML = `
        <div class="empty-state">
          <p>Antigravity языковой сервер не отвечает.</p>
          <span style="font-size:11px;color:#71717a;">${ag.error || "Проверьте запущен ли Antigravity"}</span>
          <button class="btn-action" onclick="fetchStatus()">Переподключиться</button>
        </div>`;
    }
    return;
  }

  if (dom.statusBadge) dom.statusBadge.className = "badge badge-online";
  if (dom.statusText) dom.statusText.textContent = "Онлайн";

  const groups = ag.groups || [];
  let html = appState.viewMode === "compact" ? renderCompact(groups) : renderCards(groups);
  
  // Append Session Summary and Activity Chart
  html += renderAnalytics(ag);

  if (dom.content) dom.content.innerHTML = html;
  if (dom.lastSyncTime) dom.lastSyncTime.textContent = new Date().toLocaleTimeString();
}

// Live ticking for countdowns
function tickCountdowns() {
  const timers = document.querySelectorAll(".reset-timer");
  let hasExpired = false;
  timers.forEach(t => {
    const iso = t.getAttribute("data-reset");
    if (iso) {
      const diffMs = new Date(iso) - new Date();
      if (diffMs <= 0 && diffMs > -60000) {
        hasExpired = true;
      }
      const span = t.querySelector("span");
      if (span) {
        if (t.classList.contains("compact-timer") || t.classList.contains("stat-val") || t.classList.contains("stat-tile")) {
          span.textContent = formatCountdownCompact(iso);
          t.setAttribute("title", `Точное время сброса: ${formatCountdown(iso)}`);
        } else {
          span.textContent = `Сброс: ${formatCountdown(iso)}`;
        }
      }
    }
  });

  // When timer hits zero, auto-trigger a status refresh once
  if (hasExpired && Date.now() - (appState.lastFetchTime || 0) > 15000) {
    fetchStatus();
  }
}

// Fetch status from API
async function fetchStatus() {
  if (dom.btnRefresh) dom.btnRefresh.classList.add("spinning");
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Check previous used percent for activity feed delta
    let prevUsed = null;
    if (appState.data && appState.data.antigravity && appState.data.antigravity.groups) {
      const g0 = appState.data.antigravity.groups[0];
      if (g0 && g0.buckets) {
        const b0 = g0.buckets.find(x => x.window === "5h") || g0.buckets[0];
        if (b0) prevUsed = b0.usedPercent;
      }
    }

    const wasOffline = !appState.data || !appState.data.antigravity || !appState.data.antigravity.online;
    appState.data = data;
    appState.lastFetchTime = Date.now();

    // Sync persistent settings from server on initial load
    if (data.settings && !appState.settingsLoaded) {
      applyServerSettings(data.settings);
    }

    recordHistorySample(data);

    // Record activity feed entry if quota used increased
    if (prevUsed !== null && data && data.antigravity && data.antigravity.groups) {
      const g1 = data.antigravity.groups[0];
      if (g1 && g1.buckets) {
        const b1 = g1.buckets.find(x => x.window === "5h") || g1.buckets[0];
        if (b1) {
          recordActivityFeedItem(prevUsed, b1.usedPercent, b1);
        }
      }
    }

    render();
    const isOffline = !data || !data.antigravity || !data.antigravity.online;
    if (wasOffline !== isOffline) {
      setupAutoRefresh();
    }
  } catch (err) {
    console.error("Failed to fetch quota status:", err);
    if (dom.statusBadge) dom.statusBadge.className = "badge badge-offline";
    if (dom.statusText) dom.statusText.textContent = "Ошибка";
    setupAutoRefresh();
  } finally {
    if (dom.btnRefresh) {
      setTimeout(() => {
        dom.btnRefresh.classList.remove("spinning");
      }, 400);
    }
  }
}

// Setup Auto Refresh
function setupAutoRefresh() {
  if (appState.autoRefreshTimer) {
    clearInterval(appState.autoRefreshTimer);
  }
  if (appState.autoRefreshEnabled) {
    const isOffline = !appState.data || !appState.data.antigravity || !appState.data.antigravity.online;
    const intervalMs = isOffline ? 3000 : ((appState.refreshInterval || 30) * 1000);
    appState.autoRefreshTimer = setInterval(fetchStatus, intervalMs);
  }
}

// Event Listeners
if (dom.btnRefresh) {
  dom.btnRefresh.addEventListener("click", () => fetchStatus());
}

if (dom.autoRefreshCheck) {
  dom.autoRefreshCheck.addEventListener("change", (e) => {
    persistSetting({ auto_refresh: e.target.checked });
    setupAutoRefresh();
  });
}

if (dom.intervalSelect) {
  dom.intervalSelect.addEventListener("change", (e) => {
    const val = parseInt(e.target.value, 10) || 30;
    persistSetting({ refresh_interval: val });
    setupAutoRefresh();
  });
}

// Smart Focus: refresh if user was away for > 5 seconds
window.addEventListener("focus", () => {
  if (Date.now() - (appState.lastFetchTime || 0) > 5000) {
    fetchStatus();
  }
});

if (dom.btnModeCompact) {
  dom.btnModeCompact.addEventListener("click", () => {
    persistSetting({ view_mode: "compact" });
    render();
  });
}

if (dom.btnModeCards) {
  dom.btnModeCards.addEventListener("click", () => {
    persistSetting({ view_mode: "cards" });
    render();
  });
}

dom.filterTabs.forEach(tab => {
  tab.addEventListener("click", () => {
    const val = tab.getAttribute("data-filter");
    persistSetting({ filter: val });
    render();
  });
});

// Tick clock every second
setInterval(tickCountdowns, 1000);

// Initial UI sync and Load
syncUI();
fetchStatus();
setupAutoRefresh();


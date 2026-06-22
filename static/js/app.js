/* ============================================================
   Miraje — mini  ·  client application
   Vanilla JS, no build step, no external requests, no telemetry.
   ============================================================ */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    sessions: [],
    currentSessionId: null,
    currentMode: "chat",          // "chat" | "agent"
    models: [],
    defaultModel: "",
    tools: [],
    enabledTools: {},             // toolName -> bool
    personas: [],
    activePersona: "default",     // persona id
    tags: [],                     // [{tag, count}]
    activeTagFilters: [],         // array of tag strings (multi-select)
    recent: [],                   // recently-viewed sessions
    streaming: false,
    abortCtrl: null,
    uploadedFiles: [],            // [{name, path, size}] pending files attached to next send
  };

  // Deterministic tag color from a palette (amber-leaning, no blue/indigo).
  var TAG_PALETTE = ["#d4a574", "#e2b886", "#c08b54", "#a8744a", "#dbb87a", "#b8946a", "#9c7050", "#d8a064"];
  function tagColor(tag) {
    var h = 0;
    for (var i = 0; i < tag.length; i++) { h = (h * 31 + tag.charCodeAt(i)) >>> 0; }
    return TAG_PALETTE[h % TAG_PALETTE.length];
  }

  var ARCH_SVG =
    '<svg class="welcome-arch" viewBox="0 0 120 130" fill="none">' +
    '<path d="M12,120 L12,42 Q12,18 36,18 L84,18 Q108,18 108,42 L108,120" stroke="currentColor" stroke-width="1.4" opacity="0.22"/>' +
    '<path d="M24,120 L24,50 Q24,30 44,30 L76,30 Q96,30 96,50 L96,120" stroke="currentColor" stroke-width="1.4" opacity="0.38"/>' +
    '<path d="M36,120 L36,58 Q36,42 52,42 L68,42 Q84,42 84,58 L84,120" stroke="currentColor" stroke-width="1.4" opacity="0.55"/>' +
    '<path d="M48,120 L48,66 Q48,54 60,54 Q72,54 72,66 L72,120" stroke="currentColor" stroke-width="1.4" opacity="0.75"/>' +
    '<circle cx="60" cy="72" r="3.5" fill="#d4a574"/>' +
    "</svg>";

  // ---------- API helpers ----------
  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    return fetch(path, opts).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || r.statusText); });
      return r;
    });
  }
  function apiJson(path, opts) {
    return api(path, opts).then(function (r) { return r.json(); });
  }

  // ---------- SSE stream over fetch ----------
  function streamSse(url, body, onEvent, signal, method) {
    return fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: signal,
    }).then(function (resp) {
      if (!resp.ok) return resp.text().then(function (t) { throw new Error(t || resp.statusText); });
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) return;
          buffer += decoder.decode(res.value, { stream: true });
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (chunk) {
            chunk.split("\n").forEach(function (line) {
              if (line.indexOf("data:") === 0) {
                var raw = line.slice(5).trim();
                if (!raw) return;
                try { onEvent(JSON.parse(raw)); } catch (e) { /* ignore */ }
              }
            });
          });
          return pump();
        });
      }
      return pump();
    });
  }

  // ---------- Toast ----------
  var toastTimer = null;
  function toast(msg, type) {
    var el = $("toast");
    el.textContent = msg;
    el.className = "toast" + (type ? " " + type : "") + " show";
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () { el.hidden = true; }, 200);
    }, 2800);
  }

  // ---------- Sessions ----------
  function loadSessions() {
    return apiJson("/api/sessions").then(function (data) {
      state.sessions = data.sessions || [];
      renderSessions();
    });
  }

  function loadTags() {
    return apiJson("/api/tags").then(function (data) {
      state.tags = data.tags || [];
      renderTagFilter();
    });
  }

  function renderTagFilter() {
    var box = $("tagFilter");
    if (!state.tags.length) { box.innerHTML = ""; box.style.display = "none"; return; }
    box.style.display = "flex";
    var html = state.tags.map(function (t) {
      var active = state.activeTagFilters.indexOf(t.tag) !== -1 ? " active" : "";
      return '<span class="tag-chip' + active + '" data-tag="' + window.MirajeMarkdown.escape(t.tag) + '"></span>';
    }).join("");
    if (state.activeTagFilters.length) {
      html += '<span class="tag-chip tag-clear" data-tag="">clear (' + state.activeTagFilters.length + ')</span>';
    }
    box.innerHTML = html;
    box.querySelectorAll(".tag-chip").forEach(function (chip) {
      var tag = chip.getAttribute("data-tag");
      var t = state.tags.find(function (x) { return x.tag === tag; });
      if (t) chip.textContent = t.tag + " (" + t.count + ")";
      chip.addEventListener("click", function () {
        if (!tag) {
          // Clear all
          state.activeTagFilters = [];
        } else {
          var idx = state.activeTagFilters.indexOf(tag);
          if (idx === -1) state.activeTagFilters.push(tag);
          else state.activeTagFilters.splice(idx, 1);
        }
        renderTagFilter();
        renderSessions();
      });
    });
  }

  function loadRecent() {
    return apiJson("/api/sessions/recent").then(function (data) {
      state.recent = data.sessions || [];
      renderRecent();
    }).catch(function () { state.recent = []; renderRecent(); });
  }

  function renderRecent() {
    var section = $("recentSection");
    var list = $("recentList");
    if (!state.recent.length) {
      section.style.display = "none";
      list.innerHTML = "";
      return;
    }
    section.style.display = "block";
    // Exclude the currently-open session from the recent list (it's already visible).
    var items = state.recent.filter(function (s) { return s.id !== state.currentSessionId; }).slice(0, 5);
    if (!items.length) { section.style.display = "none"; return; }
    list.innerHTML = items.map(function (s) {
      var icon = s.mode === "agent"
        ? '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 1 4 4v1a3 3 0 0 1 3 3v3a8 8 0 0 1-16 0v-3a3 3 0 0 1 3-3V6a4 4 0 0 1 4-4z"/></svg>'
        : '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
      return '<div class="recent-item" data-id="' + s.id + '"><span class="ri-icon">' + icon + '</span><span class="ri-title"></span></div>';
    }).join("");
    list.querySelectorAll(".recent-item").forEach(function (el, i) {
      el.querySelector(".ri-title").textContent = items[i].title;
      el.addEventListener("click", function () { selectSession(items[i].id); });
    });
  }

  function renderSessions() {
    var list = $("sessionsList");
    var visible = state.sessions;
    if (state.activeTagFilters.length) {
      visible = visible.filter(function (s) {
        var tags = (s.tags || "").split(",").map(function (t) { return t.trim(); });
        // AND: session must have ALL selected tags.
        return state.activeTagFilters.every(function (ft) { return tags.indexOf(ft) !== -1; });
      });
    }
    if (!visible.length) {
      list.innerHTML = '<div class="sessions-empty">' + (state.activeTagFilters.length ? 'No sessions matching all selected tags.' : "No conversations yet.") + "</div>";
      return;
    }
    list.innerHTML = visible.map(function (s) {
      var icon = s.mode === "agent"
        ? '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 1 4 4v1a3 3 0 0 1 3 3v3a8 8 0 0 1-16 0v-3a3 3 0 0 1 3-3V6a4 4 0 0 1 4-4z"/></svg>'
        : '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
      var active = s.id === state.currentSessionId ? " active" : "";
      var pinned = s.pinned ? " pinned" : "";
      var pinIcon = s.pinned
        ? '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5M9 10.76V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v4.76a2 2 0 0 0 .59 1.41l2.41 2.41a1 1 0 0 1-1 1.7H6.59a1 1 0 0 1-.71-1.7l2.41-2.41A2 2 0 0 0 9 10.76z"/></svg>'
        : '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5M9 10.76V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v4.76a2 2 0 0 0 .59 1.41l2.41 2.41a1 1 0 0 1-1 1.7H6.59a1 1 0 0 1-.71-1.7l2.41-2.41A2 2 0 0 0 9 10.76z"/></svg>';
      // Tag color dots
      var tagDots = "";
      if (s.tags) {
        var tags = s.tags.split(",").map(function (t) { return t.trim(); }).filter(Boolean).slice(0, 3);
        tagDots = '<span class="si-tags">' + tags.map(function (t) {
          return '<span class="si-tag-dot" style="background:' + tagColor(t) + '" title="' + window.MirajeMarkdown.escape(t) + '"></span>';
        }).join("") + "</span>";
      }
      return '<div class="session-item' + active + pinned + '" data-id="' + s.id + '">' +
        '<span class="si-icon">' + icon + "</span>" +
        '<span class="si-title"></span>' +
        tagDots +
        '<button class="si-pin' + (s.pinned ? "" : " unpinned") + '" data-pin="' + s.id + '" aria-label="Pin" title="' + (s.pinned ? "Unpin" : "Pin") + '">' + pinIcon + "</button>" +
        '<button class="si-more" data-more="' + s.id + '" aria-label="More actions" title="More actions">' +
        '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="5" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="12" cy="19" r="1.4"/></svg>' +
        "</button>" +
        '<button class="si-del" data-del="' + s.id + '" aria-label="Delete" title="Delete">' +
        '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>' +
        "</button></div>";
    }).join("");
    // Fill titles via textContent (safe).
    var items = list.querySelectorAll(".session-item");
    items.forEach(function (el, idx) {
      el.querySelector(".si-title").textContent = state.sessions[idx].title;
    });
  }

  // ---------- Session context menu ----------
  var menuEl = null;
  function closeMenu() {
    if (menuEl) { menuEl.remove(); menuEl = null; }
    document.removeEventListener("click", closeMenu);
  }

  function openMenu(sessionId, anchorEl) {
    closeMenu();
    menuEl = document.createElement("div");
    menuEl.className = "ctx-menu";
    menuEl.innerHTML =
      '<button class="ctx-item" data-act="rename">' +
      '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>' +
      'Rename</button>' +
      '<button class="ctx-item" data-act="tags">' +
      '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><circle cx="7" cy="7" r="1.5" fill="currentColor"/></svg>' +
      'Edit tags</button>' +
      '<button class="ctx-item" data-act="duplicate">' +
      '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>' +
      'Duplicate</button>' +
      '<div class="ctx-sep"></div>' +
      '<button class="ctx-item" data-act="export-md">' +
      '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/></svg>' +
      'Export Markdown</button>' +
      '<button class="ctx-item" data-act="export-json">' +
      '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M10 13a2 2 0 0 0-2 2c0 1 1 1.5 2 2s2 1 2 2-1 2-2 2M14 13a2 2 0 0 0-2 2c0 1 1 1.5 2 2s2 1 2 2-1 2-2 2"/></svg>' +
      'Export JSON</button>' +
      '<div class="ctx-sep"></div>' +
      '<button class="ctx-item ctx-danger" data-act="delete">' +
      '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>' +
      'Delete</button>';
    document.body.appendChild(menuEl);
    // Position near the anchor.
    var rect = anchorEl.getBoundingClientRect();
    var menuRect = menuEl.getBoundingClientRect();
    var left = Math.min(rect.right - menuRect.width, window.innerWidth - menuRect.width - 8);
    var top = rect.bottom + 4;
    if (top + menuRect.height > window.innerHeight - 8) top = rect.top - menuRect.height - 4;
    menuEl.style.left = Math.max(8, left) + "px";
    menuEl.style.top = Math.max(8, top) + "px";

    menuEl.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      e.stopPropagation();
      var act = btn.getAttribute("data-act");
      closeMenu();
      if (act === "rename") startRenameSession(sessionId);
      else if (act === "tags") startEditTags(sessionId);
      else if (act === "duplicate") duplicateSessionUI(sessionId);
      else if (act === "export-md") exportSession(sessionId, "markdown");
      else if (act === "export-json") exportSession(sessionId, "json");
      else if (act === "delete") deleteSession(sessionId);
    });
    // Defer the document-close handler so the opening click doesn't immediately close it.
    setTimeout(function () { document.addEventListener("click", closeMenu); }, 0);
  }

  function exportSession(id, fmt) {
    // Direct browser download via a temporary anchor.
    var url = "/api/sessions/" + id + "/export/" + fmt;
    var a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("Exported as " + fmt.toUpperCase(), "success");
  }

  function startRenameSession(sessionId) {
    var s = state.sessions.find(function (x) { return x.id === sessionId; });
    if (!s) return;
    var item = document.querySelector('.session-item[data-id="' + sessionId + '"]');
    if (!item) return;
    var titleEl = item.querySelector(".si-title");
    var original = s.title;
    // Replace the title span with an inline input.
    var input = document.createElement("input");
    input.type = "text";
    input.className = "si-rename-input";
    input.value = original;
    input.style.width = "100%";
    titleEl.replaceWith(input);
    input.focus();
    input.select();
    var done = function (commit) {
      var newTitle = input.value.trim();
      var replacement = document.createElement("span");
      replacement.className = "si-title";
      replacement.textContent = commit && newTitle ? newTitle : original;
      input.replaceWith(replacement);
      if (commit && newTitle && newTitle !== original) {
        api("/api/sessions/" + sessionId + "?title=" + encodeURIComponent(newTitle), { method: "PATCH" }).then(function () {
          s.title = newTitle;
          if (state.currentSessionId === sessionId) $("sessionTitle").textContent = newTitle;
          loadSessions();
          toast("Renamed", "success");
        }).catch(function (e) { toast(e.message, "error"); });
      }
    };
    input.addEventListener("blur", function () { done(true); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      if (e.key === "Escape") { e.preventDefault(); done(false); }
    });
  }

  function startEditTags(sessionId) {
    var s = state.sessions.find(function (x) { return x.id === sessionId; });
    if (!s) return;
    var item = document.querySelector('.session-item[data-id="' + sessionId + '"]');
    if (!item) return;
    var titleEl = item.querySelector(".si-title");
    var original = (s.tags || "");
    var input = document.createElement("input");
    input.type = "text";
    input.className = "si-rename-input";
    input.value = original;
    input.placeholder = "work, research…";
    input.style.width = "100%";
    titleEl.replaceWith(input);
    input.focus();
    input.select();
    var done = function (commit) {
      var newTags = input.value.trim();
      var replacement = document.createElement("span");
      replacement.className = "si-title";
      replacement.textContent = s.title;
      input.replaceWith(replacement);
      if (commit && newTags !== original) {
        api("/api/sessions/" + sessionId + "/tags?tags=" + encodeURIComponent(newTags), { method: "PUT" }).then(function () {
          s.tags = newTags;
          loadSessions();
          loadTags();
          toast("Tags updated", "success");
        }).catch(function (e) { toast(e.message, "error"); });
      }
    };
    input.addEventListener("blur", function () { done(true); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      if (e.key === "Escape") { e.preventDefault(); done(false); }
    });
  }

  function duplicateSessionUI(sessionId) {
    api("/api/sessions/" + sessionId + "/duplicate", { method: "POST" }).then(function (r) { return r.json(); }).then(function (data) {
      toast("Session duplicated", "success");
      loadSessions().then(function () { selectSession(data.session.id); });
    }).catch(function (e) { toast(e.message, "error"); });
  }

  function selectSession(id) {
    state.currentSessionId = id;
    var s = state.sessions.find(function (x) { return x.id === id; });
    state.currentMode = s ? s.mode : "chat";
    // Restore the session's persisted persona (if any).
    if (s && s.persona) state.activePersona = s.persona;
    else state.activePersona = "default";
    renderSessions();
    renderComposerTools();
    loadMessages(id);
    closeMobileSidebar();
    // Mark as viewed (best-effort, for the recently-viewed list), then refresh recent.
    api("/api/sessions/" + id + "/viewed", { method: "POST" }).then(function () { loadRecent(); }).catch(function () {});
    renderRecent();
  }

  function newSession(mode) {
    state.currentMode = mode;
    state.currentSessionId = null;
    renderSessions();
    renderWelcome();
    renderComposerTools();
    updateContextCounter();
    $("sessionTitle").textContent = mode === "agent" ? "New agent task" : "New chat";
    $("modeBadge").textContent = mode;
    $("composerInput").focus();
    closeMobileSidebar();
  }

  function deleteSession(id) {
    if (!confirm("Delete this conversation? This cannot be undone.")) return;
    api("/api/sessions/" + id, { method: "DELETE" }).then(function () {
      if (state.currentSessionId === id) {
        state.currentSessionId = null;
        renderWelcome();
      }
      loadSessions();
      toast("Conversation deleted", "success");
    }).catch(function (e) { toast(e.message, "error"); });
  }

  function loadMessages(id) {
    return apiJson("/api/sessions/" + id).then(function (data) {
      var s = data.session;
      $("sessionTitle").textContent = s.title;
      $("modeBadge").textContent = s.mode;
      var box = $("messages");
      box.innerHTML = '<div class="messages-inner" id="messagesInner"></div>';
      var inner = $("messagesInner");
      (data.messages || []).forEach(function (m) { inner.appendChild(renderMessage(m)); });
      scrollBottom();
      updateContextCounter();
    }).catch(function (e) { toast(e.message, "error"); });
  }

  // ---------- Message rendering ----------
  function renderMessage(m) {
    var row = document.createElement("div");
    row.className = "msg " + m.role;
    if (m.role === "assistant" && m.meta && m.meta.agent) row.classList.add("agent-msg");
    row.setAttribute("data-mid", m.id);

    var avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    if (m.role === "user") avatar.textContent = "You";
    else if (m.role === "assistant") avatar.innerHTML = '<img src="/static/logo.svg" alt=""/>';
    else avatar.textContent = "T";

    var body = document.createElement("div");
    body.className = "msg-body";

    var head = document.createElement("div");
    head.className = "msg-head";

    var role = document.createElement("div");
    role.className = "msg-role";
    role.textContent = m.role === "user" ? "You" : (m.role === "assistant" ? "Miraje" : "Tool");

    // Hover action bar.
    var actions = document.createElement("div");
    actions.className = "msg-actions";
    actions.appendChild(msgAction("Copy", '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>', function () {
      navigator.clipboard.writeText(m.content || "").then(function () { toast("Copied to clipboard", "success"); });
    }));
    if (m.role === "user") {
      actions.appendChild(msgAction("Edit & resend", '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>', function () {
        startInlineEdit(m, row, content);
      }));
    }
    if (m.role === "assistant") {
      actions.appendChild(msgAction("Regenerate", '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>', function () {
        regenerateMessage(m.id);
      }));
    }
    actions.appendChild(msgAction(m.starred ? "Unstar" : "Star",
      m.starred
        ? '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>'
        : '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
      function () { toggleStar(m, row, actions); },
      false, m.starred ? "msg-action-starred" : ""
    ));
    actions.appendChild(msgAction("Delete", '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>', function () {
      deleteMessageUI(m.id, row);
    }, true));

    head.appendChild(role);
    head.appendChild(actions);

    var content = document.createElement("div");
    content.className = "msg-content";
    if (m.role === "assistant") content.innerHTML = window.MirajeMarkdown.render(m.content || "");
    else content.textContent = m.content;

    body.appendChild(head);
    body.appendChild(content);
    row.appendChild(avatar);
    row.appendChild(body);
    return row;
  }

  // ---------- Inline edit (user messages) ----------
  function startInlineEdit(m, rowEl, contentEl) {
    if (state.streaming) { toast("Stop the current generation first", "error"); return; }
    var original = m.content || "";
    contentEl.innerHTML = "";
    var ta = document.createElement("textarea");
    ta.className = "inline-edit-input";
    ta.value = original;
    ta.rows = 1;
    var btnRow = document.createElement("div");
    btnRow.className = "inline-edit-actions";
    var saveBtn = document.createElement("button");
    saveBtn.className = "btn btn-primary btn-sm";
    saveBtn.textContent = "Save & resend";
    var cancelBtn = document.createElement("button");
    cancelBtn.className = "btn btn-ghost btn-sm";
    cancelBtn.textContent = "Cancel";
    btnRow.appendChild(saveBtn);
    btnRow.appendChild(cancelBtn);
    contentEl.appendChild(ta);
    contentEl.appendChild(btnRow);
    // auto-resize
    var resize = function () { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 240) + "px"; };
    ta.addEventListener("input", resize);
    setTimeout(function () { ta.focus(); resize(); }, 10);
    cancelBtn.addEventListener("click", function () {
      contentEl.textContent = original;
    });
    saveBtn.addEventListener("click", function () {
      var newText = ta.value.trim();
      if (!newText) { toast("Content cannot be empty", "error"); return; }
      if (newText === original) { contentEl.textContent = original; return; }
      runEditAndResend(m.id, newText, rowEl);
    });
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveBtn.click(); }
      if (e.key === "Escape") { e.preventDefault(); contentEl.textContent = original; }
    });
  }

  function runEditAndResend(messageId, newText, rowEl) {
    var box = $("messages");
    var inner = box.querySelector("#messagesInner");
    if (!inner) return;
    // Remove every message row after the edited one.
    var found = false;
    Array.prototype.forEach.call(inner.querySelectorAll(".msg"), function (el) {
      if (found) el.remove();
      if (el === rowEl) found = true;
    });
    // Update the edited row's content display.
    var contentEl = rowEl.querySelector(".msg-content");
    contentEl.textContent = newText;

    // Append a fresh assistant placeholder.
    var aRow = document.createElement("div");
    aRow.className = "msg assistant";
    aRow.innerHTML =
      '<div class="msg-avatar"><img src="/static/logo.svg" alt=""/></div>' +
      '<div class="msg-body"><div class="msg-head"><div class="msg-role">Miraje</div></div><div class="msg-content"><div class="typing"><span></span><span></span><span></span></div></div></div>';
    inner.appendChild(aRow);
    var aContent = aRow.querySelector(".msg-content");
    scrollBottom();

    state.streaming = true;
    state.abortCtrl = new AbortController();
    updateStopBtn();
    var acc = "";
    var firstToken = true;

    streamSse("/api/sessions/" + state.currentSessionId + "/messages/" + messageId + "/edit", { content: newText }, function (ev) {
      if (ev.type === "reasoning") {
        var rBox = ensureReasoningBox(aRow);
        if (rBox) {
          rBox.querySelector(".reasoning-body").textContent += ev.content;
          scrollBottom();
        }
      } else if (ev.type === "token") {
        if (firstToken) { aContent.innerHTML = ""; aContent.classList.add("stream-cursor"); firstToken = false; }
        acc += ev.content;
        aContent.innerHTML = window.MirajeMarkdown.render(acc);
        scrollBottom();
      } else if (ev.type === "error") {
        aContent.classList.remove("stream-cursor");
        aContent.innerHTML += '<span style="color:var(--danger)">⚠ ' + window.MirajeMarkdown.escape(ev.content) + "</span>";
        toast(ev.content, "error");
      } else if (ev.type === "done") {
        aContent.classList.remove("stream-cursor");
        if (ev.content) { acc = ev.content; aContent.innerHTML = window.MirajeMarkdown.render(acc); }
      }
    }, state.abortCtrl.signal, "PUT").then(function () {
      state.streaming = false;
      state.abortCtrl = null;
      updateSendState();
      updateStopBtn();
      updateContextCounter();
      loadSessions();
    }).catch(function (e) {
      state.streaming = false;
      state.abortCtrl = null;
      updateSendState();
      updateStopBtn();
      if (e.name !== "AbortError") toast(e.message, "error");
    });
  }

  function msgAction(label, iconHtml, onClick, danger, extraClass) {
    var btn = document.createElement("button");
    btn.className = "msg-action" + (danger ? " msg-action-danger" : "") + (extraClass ? " " + extraClass : "");
    btn.setAttribute("aria-label", label);
    btn.title = label;
    btn.innerHTML = iconHtml + '<span class="msg-action-label">' + label + "</span>";
    btn.addEventListener("click", onClick);
    return btn;
  }

  function toggleStar(m, rowEl, actionsEl) {
    var newVal = !m.starred;
    api("/api/sessions/" + state.currentSessionId + "/messages/" + m.id + "/star?starred=" + (newVal ? "true" : "false"), { method: "PUT" }).then(function () {
      m.starred = newVal;
      // Re-render this message's actions.
      if (actionsEl) {
        actionsEl.innerHTML = "";
        actionsEl.appendChild(msgAction("Copy", '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>', function () {
          navigator.clipboard.writeText(m.content || "").then(function () { toast("Copied to clipboard", "success"); });
        }));
        if (m.role === "user") {
          actionsEl.appendChild(msgAction("Edit & resend", '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>', function () { startInlineEdit(m, rowEl, rowEl.querySelector(".msg-content")); }));
        }
        if (m.role === "assistant") {
          actionsEl.appendChild(msgAction("Regenerate", '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>', function () { regenerateMessage(m.id); }));
        }
        actionsEl.appendChild(msgAction(m.starred ? "Unstar" : "Star",
          m.starred
            ? '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>'
            : '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
          function () { toggleStar(m, rowEl, actionsEl); }, false, m.starred ? "msg-action-starred" : ""));
        actionsEl.appendChild(msgAction("Delete", '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>', function () { deleteMessageUI(m.id, rowEl); }, true));
      }
      rowEl.classList.toggle("starred", newVal);
      toast(newVal ? "Starred" : "Unstarred", "success");
    }).catch(function (e) { toast(e.message, "error"); });
  }

  function deleteMessageUI(messageId, rowEl) {
    if (!confirm("Delete this message?")) return;
    api("/api/sessions/" + state.currentSessionId + "/messages/" + messageId, { method: "DELETE" }).then(function () {
      rowEl.remove();
      toast("Message deleted", "success");
    }).catch(function (e) { toast(e.message, "error"); });
  }

  function regenerateMessage(messageId) {
    if (state.streaming) { toast("Already generating — stop first", "error"); return; }
    if (!state.currentSessionId) return;
    // Stream a regeneration; the server drops the trailing assistant reply + re-runs.
    runRegenerate();
  }

  function runRegenerate() {
    var box = $("messages");
    var inner = box.querySelector("#messagesInner");
    if (!inner) return;
    // Remove the last assistant row from the DOM (server already deleted it).
    var lastAssistant = null;
    Array.prototype.forEach.call(inner.querySelectorAll(".msg.assistant"), function (el) {
      lastAssistant = el;
    });
    if (lastAssistant) lastAssistant.remove();

    // Append a fresh assistant placeholder.
    var aRow = document.createElement("div");
    aRow.className = "msg assistant";
    aRow.innerHTML =
      '<div class="msg-avatar"><img src="/static/logo.svg" alt=""/></div>' +
      '<div class="msg-body"><div class="msg-head"><div class="msg-role">Miraje</div></div><div class="msg-content"><div class="typing"><span></span><span></span><span></span></div></div></div>';
    inner.appendChild(aRow);
    var contentEl = aRow.querySelector(".msg-content");
    scrollBottom();

    state.streaming = true;
    state.abortCtrl = new AbortController();
    updateSendState();
    updateStopBtn();
    var acc = "";
    var firstToken = true;

    streamSse("/api/sessions/" + state.currentSessionId + "/regenerate", { }, function (ev) {
      if (ev.type === "reasoning") {
        var rBox = ensureReasoningBox(aRow);
        if (rBox) {
          rBox.querySelector(".reasoning-body").textContent += ev.content;
          scrollBottom();
        }
      } else if (ev.type === "token") {
        if (firstToken) { contentEl.innerHTML = ""; contentEl.classList.add("stream-cursor"); firstToken = false; }
        acc += ev.content;
        contentEl.innerHTML = window.MirajeMarkdown.render(acc);
        scrollBottom();
      } else if (ev.type === "error") {
        contentEl.classList.remove("stream-cursor");
        contentEl.innerHTML += '<span style="color:var(--danger)">⚠ ' + window.MirajeMarkdown.escape(ev.content) + "</span>";
        toast(ev.content, "error");
      } else if (ev.type === "done") {
        contentEl.classList.remove("stream-cursor");
        if (ev.content) { acc = ev.content; contentEl.innerHTML = window.MirajeMarkdown.render(acc); }
      }
    }, state.abortCtrl.signal).then(function () {
      state.streaming = false;
      state.abortCtrl = null;
      updateSendState();
      updateStopBtn();
      updateContextCounter();
    }).catch(function (e) {
      state.streaming = false;
      state.abortCtrl = null;
      updateSendState();
      updateStopBtn();
      if (e.name !== "AbortError") toast(e.message, "error");
    });
  }

  function renderWelcome() {
    var box = $("messages");
    var mode = state.currentMode;
    var heading = mode === "agent" ? "Give Miraje a task." : "Talk to your models. Privately.";
    var sub = mode === "agent"
      ? "Describe a goal and Miraje will reason step-by-step, using tools only when needed. Watch every thought."
      : "Local-first, no telemetry. Connect any OpenAI-compatible endpoint or Ollama, then start chatting.";
    var suggestions = mode === "agent"
      ? [
          { t: "Research & summarize", s: "Find the latest on a topic and write a brief", q: "Research the current state of small open-source LLMs and summarize the top 3 in a table." },
          { t: "Compute & analyze", s: "Crunch numbers with Python", q: "Compute the first 20 Fibonacci numbers and check which are prime." },
          { t: "Plan a project", s: "Break down a goal into steps", q: "Plan a 7-day itinerary for Rome, balancing sights and food." },
          { t: "Read a page", s: "Fetch & extract a URL", q: "Fetch https://example.com and tell me what it is about in one sentence." }
        ]
      : [
          { t: "Explain a concept", s: "Clear, concise breakdowns", q: "Explain how vector embeddings work, with an analogy." },
          { t: "Draft something", s: "Emails, code, ideas", q: "Draft a friendly out-of-office email for a week off." },
          { t: "Brainstorm", s: "Ideas on tap", q: "Give me 5 product ideas for a privacy-focused tool." },
          { t: "Quick code", s: "Snippets & fixes", q: "Write a Python function to deduplicate a list of dicts by a key." }
        ];
    var compatHint = mode === "agent"
      ? '<div class="compat-hint">' +
        "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M12 9v4M12 17h.01M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'/></svg>" +
        "<span>Agent tool-calling requires an OpenAI-compatible endpoint (OpenAI, OpenRouter, LM Studio, vLLM, Ollama <code>/v1</code>). Native Ollama (<code>/api/chat</code>) answers without tools.</span>" +
        "</div>"
      : "";
    box.innerHTML =
      '<div class="welcome">' + ARCH_SVG +
      "<h2>" + heading + "</h2><p>" + sub + "</p>" +
      '<div class="suggest-grid">' +
      suggestions.map(function (s) {
        return '<div class="suggest" data-q="' + escapeAttr(s.q) + '">' +
          '<div class="s-title"></div><div class="s-sub"></div></div>';
      }).join("") +
      "</div>" +
      compatHint +
      "</div>";
    // Safe text fills.
    box.querySelectorAll(".suggest").forEach(function (el, i) {
      el.querySelector(".s-title").textContent = suggestions[i].t;
      el.querySelector(".s-sub").textContent = suggestions[i].s;
      el.addEventListener("click", function () {
        $("composerInput").value = suggestions[i].q;
        autoResize();
        updateSendState();
        $("composerInput").focus();
      });
    });
    updateContextCounter();
  }

  function escapeAttr(s) {
    return s.replace(/"/g, "&quot;");
  }

  // ---------- File uploads (drag-drop into composer) ----------
  function uploadFile(file, sessionId) {
    return fetch("/api/sessions/" + sessionId + "/upload?filename=" + encodeURIComponent(file.name), {
      method: "POST",
      body: file,  // raw file body, not FormData
      headers: { "Content-Type": "application/octet-stream" }
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || r.statusText); });
      return r.json();
    });
  }

  function ensureSessionForUpload() {
    // Returns a promise resolving to a session id, creating one if necessary.
    if (state.currentSessionId) return Promise.resolve(state.currentSessionId);
    return apiJson("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ mode: state.currentMode || "agent", title: "Untitled" })
    }).then(function (data) {
      var sid = data.session && data.session.id;
      if (sid) {
        state.currentSessionId = sid;
        return loadSessions().then(function () { return sid; });
      }
      throw new Error("Could not create session for upload");
    });
  }

  function handleFileUpload(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    ensureSessionForUpload().then(function (sid) {
      return Promise.all(files.map(function (f) {
        return uploadFile(f, sid).then(function (info) {
          state.uploadedFiles.push({
            name: (info && info.filename) || f.name,
            path: (info && info.path) || "",
            size: (info && info.size) || f.size
          });
        }).catch(function (e) {
          toast("Failed to upload " + f.name + ": " + e.message, "error");
        });
      }));
    }).then(function () {
      renderFileAttachments();
      toast(state.uploadedFiles.length + " file(s) attached", "success");
    }).catch(function (e) {
      toast(e.message, "error");
    });
  }

  function renderFileAttachments() {
    var box = $("fileAttachments");
    if (!box) return;
    if (!state.uploadedFiles.length) { box.innerHTML = ""; return; }
    box.innerHTML = state.uploadedFiles.map(function (f, i) {
      return "<span class='file-chip' data-i='" + i + "'>" +
        "<svg width='11' height='11' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z'/><path d='M13 2v7h7'/></svg>" +
        "<span class='fc-name'></span>" +
        "<button type='button' class='fc-remove' data-rm='" + i + "' aria-label='Remove'>" +
        "<svg width='9' height='9' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round'><path d='M18 6L6 18M6 6l12 12'/></svg>" +
        "</button></span>";
    }).join("");
    box.querySelectorAll(".file-chip").forEach(function (chip, i) {
      chip.querySelector(".fc-name").textContent = state.uploadedFiles[i].name;
    });
    box.querySelectorAll(".fc-remove").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var idx = parseInt(btn.getAttribute("data-rm"), 10);
        if (!isNaN(idx)) state.uploadedFiles.splice(idx, 1);
        renderFileAttachments();
      });
    });
  }

  function clearFileAttachments() {
    state.uploadedFiles = [];
    renderFileAttachments();
  }

  // ---------- Asset & download rendering (agent observations) ----------
  // Parse a blob of observation text for http(s) URLs.
  function parseAssetUrls(text) {
    if (!text) return [];
    var re = /https?:\/\/[^\s)'"<>\]]+/g;
    var matches = text.match(re) || [];
    var seen = {};
    var out = [];
    matches.forEach(function (u) {
      u = u.replace(/[.,;]+$/, "");
      if (!seen[u]) { seen[u] = 1; out.push(u); }
    });
    return out;
  }

  function isImageUrl(url) {
    return /\.(png|jpe?g|gif|webp|svg|bmp|avif)(\?|$)/i.test(url);
  }

  function renderAssetCards(container, urls) {
    if (!urls || !urls.length) return;
    var grid = document.createElement("div");
    grid.className = "asset-grid";
    grid.innerHTML = urls.map(function (u) {
      var preview = isImageUrl(u)
        ? "<img src='" + window.MirajeMarkdown.escape(u) + "' alt='' loading='lazy' onerror='this.style.display=\"none\"'/>"
        : "<svg width='28' height='28' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'><path d='M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z'/><path d='M13 2v7h7'/></svg>";
      return "<div class='asset-card'>" +
        "<div class='ac-preview'>" + preview + "</div>" +
        "<div class='ac-body'>" +
        "<div class='ac-url'></div>" +
        "<div class='ac-actions'>" +
        "<a class='ac-download' href='" + window.MirajeMarkdown.escape(u) + "' target='_blank' rel='noopener noreferrer'>" +
        "<svg width='11' height='11' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3'/></svg>" +
        "open</a>" +
        "</div></div></div>";
    }).join("");
    grid.querySelectorAll(".ac-url").forEach(function (el, i) {
      el.textContent = urls[i];
    });
    container.appendChild(grid);
  }

  function renderDownloadedFile(container, filename) {
    if (!filename) return;
    var card = document.createElement("div");
    card.className = "downloaded-file";
    card.innerHTML =
      "<span class='df-icon'>" +
      "<svg width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3'/></svg>" +
      "</span>" +
      "<span class='df-name'></span>" +
      "<a class='df-link' href='/api/workspace/downloads/" + encodeURIComponent(filename) + "' download>" +
      "<svg width='11' height='11' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3'/></svg>" +
      "download</a>";
    card.querySelector(".df-name").textContent = filename;
    container.appendChild(card);
  }

  // Inspect an agent observation string and append rich UI when relevant.
  function renderObservationExtras(container, text) {
    if (!text) return;
    var lower = text.toLowerCase();
    if (lower.indexOf("found") !== -1 && lower.indexOf("downloadable asset") !== -1) {
      var urls = parseAssetUrls(text);
      if (urls.length) renderAssetCards(container, urls);
    }
    // Surface files the agent downloaded in this turn.
    var dlRe = /downloaded[^\n]*?[\s'\"]([\w.\-]+\.\w+)/gi;
    var m;
    var rendered = {};
    while ((m = dlRe.exec(text)) !== null) {
      var fname = m[1];
      if (!rendered[fname]) {
        rendered[fname] = 1;
        renderDownloadedFile(container, fname);
      }
    }
  }

  // ---------- Composer ----------
  function renderComposerTools() {
    var box = $("composerTools");
    if (state.currentMode !== "agent" || !state.tools.length) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = state.tools.map(function (t) {
      var on = state.enabledTools[t.name] !== false;
      return '<span class="tool-chip' + (on ? " active" : "") + '" data-tool="' + t.name + '">' + t.name + "</span>";
    }).join("");
    box.querySelectorAll(".tool-chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        var name = chip.getAttribute("data-tool");
        state.enabledTools[name] = !state.enabledTools[name];
        chip.classList.toggle("active", state.enabledTools[name] !== false);
      });
    });
  }

  function autoResize() {
    var ta = $("composerInput");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }
  function updateSendState() {
    var has = $("composerInput").value.trim().length > 0;
    $("sendBtn").disabled = !has || state.streaming;
  }

  function updateStopBtn() {
    var stopBtn = $("stopBtn");
    if (!stopBtn) return;
    stopBtn.style.display = state.streaming ? "inline-flex" : "none";
  }

  function stopStreaming() {
    if (state.abortCtrl) {
      state.abortCtrl.abort();
      state.abortCtrl = null;
    }
    state.streaming = false;
    updateSendState();
    updateStopBtn();
    toast("Stopped", "success");
  }

  function send() {
    var input = $("composerInput");
    var text = input.value.trim();
    if (!text || state.streaming) return;
    input.value = "";
    autoResize();
    updateSendState();
    if (state.currentMode === "agent") runAgent(text);
    else sendChat(text);
  }

  // ---------- Chat (streaming) ----------
  function sendChat(text) {
    var box = $("messages");
    if (!state.currentSessionId) {
      // First message — render welcome->chat transition.
      box.innerHTML = '<div class="messages-inner" id="messagesInner"></div>';
    }
    var inner = $("messagesInner");

    // Append user message locally.
    inner.appendChild(renderMessage({ role: "user", content: text }));

    // Append assistant placeholder.
    var aRow = document.createElement("div");
    aRow.className = "msg assistant";
    aRow.innerHTML =
      '<div class="msg-avatar"><img src="/static/logo.svg" alt=""/></div>' +
      '<div class="msg-body"><div class="msg-role">Miraje</div><div class="msg-content"><div class="typing"><span></span><span></span><span></span></div></div></div>';
    inner.appendChild(aRow);
    var contentEl = aRow.querySelector(".msg-content");
    scrollBottom();

    state.streaming = true;
    state.abortCtrl = new AbortController();
    updateStopBtn();
    var acc = "";
    var model = $("modelSelect").value || state.defaultModel;
    var firstToken = true;

    streamSse("/api/chat", {
      session_id: state.currentSessionId,
      messages: [{ role: "user", content: text }],
      model: model,
      persona: state.currentMode === "chat" ? state.activePersona : null
    }, function (ev) {
      if (ev.type === "session") {
        state.currentSessionId = ev.session_id;
        $("sessionTitle").textContent = ev.title;
        $("modeBadge").textContent = "chat";
      } else if (ev.type === "reasoning") {
        var rBox = ensureReasoningBox(aRow);
        if (rBox) {
          rBox.querySelector(".reasoning-body").textContent += ev.content;
          scrollBottom();
        }
      } else if (ev.type === "token") {
        if (firstToken) { contentEl.innerHTML = ""; contentEl.classList.add("stream-cursor"); firstToken = false; }
        acc += ev.content;
        contentEl.innerHTML = window.MirajeMarkdown.render(acc);
        scrollBottom();
      } else if (ev.type === "error") {
        contentEl.classList.remove("stream-cursor");
        contentEl.innerHTML = '<span style="color:var(--danger)">⚠ ' + window.MirajeMarkdown.escape(ev.content) + "</span>";
        toast(ev.content, "error");
      } else if (ev.type === "done") {
        contentEl.classList.remove("stream-cursor");
        if (ev.content) { acc = ev.content; contentEl.innerHTML = window.MirajeMarkdown.render(acc); }
      }
    }, state.abortCtrl.signal).then(function () {
      state.streaming = false;
      state.abortCtrl = null;
      updateSendState();
      updateStopBtn();
      updateContextCounter();
      loadSessions();
    }).catch(function (e) {
      state.streaming = false;
      state.abortCtrl = null;
      updateSendState();
      updateStopBtn();
      if (e.name !== "AbortError") {
        contentEl.innerHTML += '<div style="color:var(--danger)">⚠ ' + window.MirajeMarkdown.escape(e.message) + "</div>";
        toast(e.message, "error");
      }
    });
  }

  // ---------- Agent (streaming steps) ----------
  function runAgent(task) {
    var box = $("messages");
    if (!state.currentSessionId) {
      box.innerHTML = '<div class="messages-inner" id="messagesInner"></div>';
    }
    var inner = $("messagesInner");

    // Build the task text sent to the model. If the user uploaded files, prepend
    // their names and server-side paths so the agent knows about them.
    var sendTask = task;
    if (state.uploadedFiles && state.uploadedFiles.length) {
      var list = state.uploadedFiles.map(function (f) {
        return "- " + f.name + " (" + (f.path || "uploads/" + f.name) + ")";
      }).join("\n");
      sendTask = "The user uploaded these files:\n" + list +
        "\n\nYou can read them with read_local_file using their absolute paths.\n\nTask: " + task;
    }

    // User task bubble (shows the original message — not the augmented task).
    inner.appendChild(renderMessage({ role: "user", content: task }));

    // Agent run container.
    var run = document.createElement("div");
    run.className = "msg assistant";
    run.innerHTML =
      '<div class="msg-avatar"><img src="/static/logo.svg" alt=""/></div>' +
      '<div class="msg-body"><div class="msg-role">Miraje · agent</div>' +
      '<div class="msg-content"><div class="agent-run"></div></div></div>';
    inner.appendChild(run);
    var runBox = run.querySelector(".agent-run");
    scrollBottom();

    // Files have been referenced in the task — clear the composer attachments.
    clearFileAttachments();

    state.streaming = true;
    state.abortCtrl = new AbortController();
    updateStopBtn();
    var model = $("modelSelect").value || state.defaultModel;
    var enabled = Object.keys(state.enabledTools).filter(function (k) { return state.enabledTools[k] !== false; });
    if (!enabled.length) enabled = state.tools.map(function (t) { return t.name; });

    var curStep = null;

    streamSse("/api/agent/run", {
      session_id: state.currentSessionId,
      task: sendTask,
      model: model,
      enabled_tools: enabled
    }, function (ev) {
      if (ev.type === "session") {
        state.currentSessionId = ev.session_id;
        $("sessionTitle").textContent = ev.title;
        $("modeBadge").textContent = "agent";
      } else if (ev.type === "step") {
        curStep = document.createElement("div");
        curStep.className = "agent-step";
        curStep.innerHTML =
          '<div class="agent-step-head"><span class="step-num">Step ' + ev.step + "</span><span>reasoning</span></div>" +
          '<div class="agent-step-body"></div>';
        runBox.appendChild(curStep);
        scrollBottom();
      } else if (ev.type === "thought") {
        if (!curStep) return;
        var t = document.createElement("div");
        t.className = "agent-thought";
        t.textContent = ev.content;
        curStep.querySelector(".agent-step-body").appendChild(t);
        scrollBottom();
      } else if (ev.type === "reasoning") {
        // Reasoning tokens (e.g. from <think> blocks) — attach to the current step,
        // or the run container if no step has started yet.
        var rTarget = curStep ? curStep.querySelector(".agent-step-body") : runBox;
        if (rTarget) {
          var rEl = rTarget.querySelector(":scope > .agent-reasoning") || (function () {
            var d = document.createElement("div");
            d.className = "agent-reasoning";
            rTarget.appendChild(d);
            return d;
          })();
          rEl.textContent += ev.content;
          scrollBottom();
        }
      } else if (ev.type === "tool_call") {
        if (!curStep) return;
        var tc = document.createElement("div");
        tc.className = "agent-tool";
        tc.innerHTML =
          '<div class="agent-tool-head">🔧 <span class="tool-name"></span></div>' +
          '<div class="agent-tool-input"></div>';
        tc.querySelector(".tool-name").textContent = ev.tool;
        tc.querySelector(".agent-tool-input").textContent = JSON.stringify(ev.input);
        curStep.querySelector(".agent-step-body").appendChild(tc);
        scrollBottom();
      } else if (ev.type === "observation") {
        if (!curStep) return;
        var ob = document.createElement("div");
        ob.className = "agent-obs" + (ev.ok === false ? " error" : "");
        ob.textContent = ev.content;
        var obsBody = curStep.querySelector(".agent-step-body");
        obsBody.appendChild(ob);
        // Rich UI: render asset cards / download links for relevant observations.
        renderObservationExtras(obsBody, ev.content || "");
        scrollBottom();
      } else if (ev.type === "final") {
        var f = document.createElement("div");
        f.className = "agent-final";
        f.innerHTML = window.MirajeMarkdown.render(ev.content);
        runBox.appendChild(f);
        scrollBottom();
      } else if (ev.type === "error") {
        var er = document.createElement("div");
        er.className = "agent-obs error";
        er.textContent = "⚠ " + ev.content;
        runBox.appendChild(er);
        toast(ev.content, "error");
        scrollBottom();
      }
    }, state.abortCtrl.signal).then(function () {
      state.streaming = false;
      state.abortCtrl = null;
      updateSendState();
      updateStopBtn();
      updateContextCounter();
      loadSessions();
    }).catch(function (e) {
      state.streaming = false;
      state.abortCtrl = null;
      updateSendState();
      updateStopBtn();
      if (e.name !== "AbortError") toast(e.message, "error");
    });
  }

  function scrollBottom() {
    var box = $("messages");
    box.scrollTop = box.scrollHeight;
  }

  // ---------- Models ----------
  function loadModels() {
    return apiJson("/api/models").then(function (data) {
      state.models = data.models || [];
      state.defaultModel = data.default || "";
      var sel = $("modelSelect");
      if (!state.models.length || (state.models.length === 1 && state.models[0].indexOf("unable") === 0)) {
        sel.innerHTML = "<option>" + (state.defaultModel || "configure in settings") + "</option>";
      } else {
        sel.innerHTML = state.models.map(function (m) {
          var selAttr = m === state.defaultModel ? " selected" : "";
          return "<option" + selAttr + ">" + m + "</option>";
        }).join("");
      }
    }).catch(function () {
      $("modelSelect").innerHTML = "<option>(no models)</option>";
    });
  }

  function loadTools() {
    return apiJson("/api/tools").then(function (data) {
      state.tools = data.tools || [];
      state.tools.forEach(function (t) { if (!(t.name in state.enabledTools)) state.enabledTools[t.name] = true; });
      renderComposerTools();
    });
  }

  function loadPersonas() {
    return apiJson("/api/personas").then(function (data) {
      state.personas = data.personas || [];
    });
  }

  // ---------- Context counter ----------
  function updateContextCounter() {
    var el = $("contextCounter");
    if (!el) return;
    var inner = $("messagesInner");
    var count = 0;
    if (inner) {
      inner.querySelectorAll(".msg.user, .msg.assistant").forEach(function (m) {
        // Skip empty streaming placeholders that only contain a typing indicator.
        if (m.querySelector(".typing")) return;
        count++;
      });
    }
    if (count > 0) {
      el.classList.add("has-context");
      el.innerHTML =
        "<svg width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><path d='M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'/></svg>" +
        count + " msgs in context";
    } else {
      el.classList.remove("has-context");
      el.innerHTML = "<svg width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><path d='M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'/></svg> 0 msgs in context";
    }
  }

  // ---------- Reasoning panel helper ----------
  function ensureReasoningBox(aRow) {
    var body = aRow.querySelector(".msg-body");
    if (!body) return null;
    var existing = body.querySelector(".reasoning-box");
    if (existing) return existing;
    var box = document.createElement("div");
    box.className = "reasoning-box";
    box.innerHTML =
      '<button class="reasoning-header" type="button" aria-expanded="true">' +
      "<svg width='11' height='11' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><path d='M9 18l6-6-6-6'/></svg>" +
      '<span>Reasoning</span>' +
      '</button>' +
      '<div class="reasoning-body"></div>';
    var content = body.querySelector(".msg-content");
    body.insertBefore(box, content);
    box.querySelector(".reasoning-header").addEventListener("click", function () {
      var collapsed = box.classList.toggle("collapsed");
      this.setAttribute("aria-expanded", collapsed ? "false" : "true");
    });
    return box;
  }

  // ---------- Persona manager (settings) ----------
  function renderPersonaManager() {
    var dropdown = $("setActivePersona");
    var list = $("personaManagerList");
    var cur = state.activePersona || "default";
    if (dropdown) {
      dropdown.innerHTML = state.personas.map(function (p) {
        var sel = p.id === cur ? " selected" : "";
        return "<option value=\"" + window.MirajeMarkdown.escape(p.id) + "\"" + sel + ">" + window.MirajeMarkdown.escape(p.name) + "</option>";
      }).join("");
      dropdown.value = cur;
    }
    if (list) {
      if (!state.personas.length) {
        list.innerHTML = '<div class="persona-mgr-empty">No personas yet.</div>';
        return;
      }
      list.innerHTML = state.personas.map(function (p) {
        return '<div class="persona-mgr-item" data-pid="' + window.MirajeMarkdown.escape(p.id) + '">' +
          '<span class="pmi-icon">' + (p.icon || "●") + '</span>' +
          '<span class="pmi-name"></span>' +
          (p.builtin ? '<span class="pmi-badge">built-in</span>' : '<span class="pmi-badge custom">custom</span>') +
          '<span class="pmi-actions">' +
          '<button class="btn btn-ghost btn-sm pmi-edit" data-act="edit" title="Edit">Edit</button>' +
          '<button class="btn btn-ghost btn-sm pmi-del" data-act="del" title="Delete">Delete</button>' +
          '</span>' +
          '</div>';
      }).join("");
      list.querySelectorAll(".persona-mgr-item").forEach(function (el, i) {
        el.querySelector(".pmi-name").textContent = state.personas[i].name;
      });
      list.querySelectorAll(".pmi-edit").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var pid = btn.closest(".persona-mgr-item").getAttribute("data-pid");
          openPersonaEditor(pid);
        });
      });
      list.querySelectorAll(".pmi-del").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var pid = btn.closest(".persona-mgr-item").getAttribute("data-pid");
          deletePersona(pid);
        });
      });
    }
  }

  function deletePersona(pid) {
    var p = state.personas.find(function (x) { return x.id === pid; });
    if (!p) return;
    if (!confirm("Delete persona \"" + p.name + "\"? This cannot be undone.")) return;
    api("/api/personas/" + encodeURIComponent(pid), { method: "DELETE" }).then(function () {
      toast("Persona deleted", "success");
      if (state.activePersona === pid) state.activePersona = "default";
      return loadPersonas().then(renderPersonaManager);
    }).catch(function (e) { toast(e.message, "error"); });
  }

  function openPersonaEditor(pid) {
    var modal = $("personaEditorModal");
    $("personaEditId").value = pid || "";
    if (pid) {
      $("personaEditorTitle").textContent = "Edit persona";
      // Load existing persona details (name + system) in parallel with memory.
      var personaP = apiJson("/api/personas/" + encodeURIComponent(pid)).then(function (data) {
        var p = data.persona || {};
        $("personaEditName").value = p.name || "";
        $("personaEditSystem").value = p.system || "";
      });
      var memP = apiJson("/api/personas/" + encodeURIComponent(pid) + "/memory").then(function (data) {
        $("personaEditMemory").value = data.memory || "";
      }).catch(function () { $("personaEditMemory").value = ""; });
      Promise.all([personaP, memP]).catch(function (e) { toast(e.message, "error"); });
    } else {
      $("personaEditorTitle").textContent = "New persona";
      $("personaEditName").value = "";
      $("personaEditSystem").value = "";
      $("personaEditMemory").value = "";
    }
    modal.hidden = false;
    setTimeout(function () { $("personaEditName").focus(); }, 10);
  }

  function closePersonaEditor() {
    $("personaEditorModal").hidden = true;
  }

  function savePersonaEditor() {
    var pid = $("personaEditId").value.trim();
    var name = $("personaEditName").value.trim();
    var system = $("personaEditSystem").value;
    var memory = $("personaEditMemory").value;
    if (!name) { toast("Name is required", "error"); return; }
    var body = JSON.stringify({ name: name, system: system });
    var p;
    if (pid) {
      p = api("/api/personas/" + encodeURIComponent(pid), { method: "PUT", body: body });
    } else {
      p = api("/api/personas", { method: "POST", body: body });
    }
    p.then(function (r) { return r.json(); }).then(function (data) {
      var newId = (data.persona && data.persona.id) || pid;
      // Save memory separately (replace whatever was there).
      return api("/api/personas/" + encodeURIComponent(newId) + "/memory", {
        method: "PUT",
        body: JSON.stringify({ content: memory || "" })
      });
    }).then(function () {
      toast(pid ? "Persona updated" : "Persona created", "success");
      closePersonaEditor();
      return loadPersonas().then(renderPersonaManager);
    }).catch(function (e) { toast(e.message, "error"); });
  }

  // ---------- Settings ----------
  function openSettings() {
    // Load settings + personas in parallel so the persona dropdown + manager
    // are populated by the time the modal is shown.
    return Promise.all([
      apiJson("/api/settings"),
      loadPersonas()
    ]).then(function (results) {
      var s = results[0] || {};
      $("setProvider").value = s.provider || "openai-compatible";
      $("setBaseUrl").value = s.base_url || "";
      $("setApiKey").value = "";
      $("setApiKey").placeholder = s.api_key_set ? s.api_key_masked : "(none set)";
      $("setOllamaUrl").value = s.ollama_url || "";
      $("setModel").value = s.model || "";
      $("setAgentSteps").value = s.agent_max_steps;
      $("setAgentTemp").value = s.agent_temperature;
      $("setCustomSystemPrompt").value = s.custom_system_prompt || "";
      var showReasoning = $("setShowReasoning");
      if (showReasoning) showReasoning.checked = !!s.show_reasoning;
      // active_persona setting — fall back to current state if unset.
      var activePersona = s.active_persona || state.activePersona || "default";
      state.activePersona = activePersona;
      renderPersonaManager();
      toggleProviderFields();
      $("settingsModal").hidden = false;
    }).catch(function (e) { toast(e.message, "error"); });
  }
  function closeSettings() { $("settingsModal").hidden = true; }

  // ---------- Stats ----------
  function openStats() {
    $("statsModal").hidden = false;
    $("statsBody").innerHTML = '<div class="stats-loading">Loading…</div>';
    apiJson("/api/stats").then(renderStats).catch(function (e) {
      $("statsBody").innerHTML = '<div class="stats-loading" style="color:var(--danger)">' + window.MirajeMarkdown.escape(e.message) + "</div>";
    });
  }
  function closeStats() { $("statsModal").hidden = true; }

  // ---------- Pin session ----------
  function togglePin(sessionId) {
    var s = state.sessions.find(function (x) { return x.id === sessionId; });
    if (!s) return;
    var newVal = !s.pinned;
    api("/api/sessions/" + sessionId + "/pin?pinned=" + (newVal ? "true" : "false"), { method: "PUT" }).then(function () {
      s.pinned = newVal;
      renderSessions();
      toast(newVal ? "Pinned" : "Unpinned", "success");
    }).catch(function (e) { toast(e.message, "error"); });
  }

  // ---------- Search ----------
  var searchTimer = null;
  function openSearch() {
    $("searchModal").hidden = false;
    $("searchInput").value = "";
    $("searchResults").innerHTML = '<div class="search-hint">Type to search. Matches message content across every session.</div>';
    setTimeout(function () { $("searchInput").focus(); }, 10);
  }
  function closeSearch() { $("searchModal").hidden = true; }

  // ---------- Starred messages ----------
  function openStarred() {
    $("starredModal").hidden = false;
    $("starredResults").innerHTML = '<div class="search-hint">Loading…</div>';
    apiJson("/api/messages/starred").then(function (data) {
      renderStarred(data.results || []);
    }).catch(function (e) {
      $("starredResults").innerHTML = '<div class="search-empty">Failed to load starred messages.</div>';
    });
  }
  function closeStarred() { $("starredModal").hidden = true; }

  // ---------- Import file ----------
  function handleImportFile(file) {
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function (e) {
      var text = e.target.result;
      var name = file.name.toLowerCase();
      var endpoint, body;
      if (name.endsWith(".json")) {
        try {
          var parsed = JSON.parse(text);
          endpoint = "/api/sessions/import/json";
          body = JSON.stringify(parsed);
        } catch (err) {
          toast("Invalid JSON file: " + err.message, "error");
          return;
        }
      } else {
        endpoint = "/api/sessions/import/text";
        body = JSON.stringify({ markdown: text });
      }
      api(endpoint, { method: "POST", body: body }).then(function (r) { return r.json(); }).then(function (data) {
        if (data.status === "ok") {
          toast("Imported " + data.messages_imported + " message(s)", "success");
          loadSessions().then(function () {
            if (data.session) selectSession(data.session.id);
            loadTags();
          });
        } else {
          toast(data.detail || "Import failed", "error");
        }
      }).catch(function (err) { toast(err.message, "error"); });
    };
    reader.readAsText(file);
  }
  function renderStarred(results) {
    var box = $("starredResults");
    if (!results.length) {
      box.innerHTML = '<div class="search-empty">No starred messages yet. Click the star icon on any message to bookmark it.</div>';
      return;
    }
    box.innerHTML = results.map(function (r) {
      return '<div class="search-result" data-sid="' + r.session_id + '">' +
        '<div class="sr-head"><span class="sr-role"></span><span class="sr-title"></span></div>' +
        '<div class="sr-snippet"></div></div>';
    }).join("");
    box.querySelectorAll(".search-result").forEach(function (el, i) {
      var r = results[i];
      el.querySelector(".sr-role").textContent = r.role;
      el.querySelector(".sr-title").textContent = r.session_title;
      var snippet = (r.content || "").slice(0, 200);
      el.querySelector(".sr-snippet").textContent = snippet;
      el.addEventListener("click", function () {
        closeStarred();
        selectSession(r.session_id);
      });
    });
  }
  function runSearch(q) {
    clearTimeout(searchTimer);
    if (!q.trim()) {
      $("searchResults").innerHTML = '<div class="search-hint">Type to search. Matches message content across every session.</div>';
      return;
    }
    searchTimer = setTimeout(function () {
      apiJson("/api/search?q=" + encodeURIComponent(q)).then(function (data) {
        renderSearchResults(data.results || [], q);
      }).catch(function () {
        $("searchResults").innerHTML = '<div class="search-empty">Search failed.</div>';
      });
    }, 220);
  }
  function renderSearchResults(results, q) {
    var box = $("searchResults");
    if (!results.length) {
      box.innerHTML = '<div class="search-empty">No matches found.</div>';
      return;
    }
    box.innerHTML = results.map(function (r) {
      return '<div class="search-result" data-sid="' + r.session_id + '">' +
        '<div class="sr-head"><span class="sr-role"></span><span class="sr-title"></span></div>' +
        '<div class="sr-snippet"></div></div>';
    }).join("");
    var safeQ = q.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
    var re = new RegExp("(" + safeQ.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
    box.querySelectorAll(".search-result").forEach(function (el, i) {
      var r = results[i];
      el.querySelector(".sr-role").textContent = r.role;
      el.querySelector(".sr-title").textContent = r.session_title;
      var snippet = (r.content || "").slice(0, 160);
      el.querySelector(".sr-snippet").innerHTML = window.MirajeMarkdown.escape(snippet).replace(re, "<mark>$1</mark>");
      el.addEventListener("click", function () {
        closeSearch();
        selectSession(r.session_id);
      });
    });
  }
  function fmtNum(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }
  function renderStats(s) {
    var body = $("statsBody");
    var roleColors = { user: "#d4a574", assistant: "#7bc47f", system: "#9a9aa4", tool: "#e5736b" };
    var roles = Object.keys(s.messages_by_role || {});
    var top = s.top_sessions || [];
    var html =
      '<div class="stats-grid">' +
      '<div class="stats-card"><div class="sc-value">' + fmtNum(s.sessions) + '</div><div class="sc-label">sessions</div></div>' +
      '<div class="stats-card"><div class="sc-value">' + fmtNum(s.messages) + '</div><div class="sc-label">messages</div></div>' +
      '<div class="stats-card"><div class="sc-value">' + fmtNum(s.total_tokens) + '</div><div class="sc-label">est. tokens</div></div>' +
      "</div>";
    if (roles.length) {
      html += '<div class="stats-section-title">By role</div><div class="stats-roles">';
      roles.forEach(function (r) {
        var info = s.messages_by_role[r];
        html += '<div class="stats-role-chip"><span class="src-dot" style="background:' + (roleColors[r] || "#9a9aa4") + '"></span><span class="src-name"></span><span class="src-count">' + info.count + " msgs · " + fmtNum(info.tokens) + " tok</span></div>";
      });
      html += "</div>";
    }
    if (top.length) {
      html += '<div class="stats-section-title">Top sessions</div><div class="stats-top-list">';
      top.forEach(function (t) {
        var icon = t.mode === "agent" ? "🤖" : "💬";
        html += '<div class="stats-top-item"><span class="sti-icon">' + icon + '</span><span class="sti-title"></span><span class="sti-meta">' + t.messages + " msgs · " + fmtNum(t.tokens) + " tok</span></div>";
      });
      html += "</div>";
    }
    if (!s.sessions) {
      html = '<div class="stats-loading">No conversations yet. Start chatting to see your stats here.</div>';
    }
    body.innerHTML = html;
    // Safe text fills.
    body.querySelectorAll(".stats-role-chip").forEach(function (el, i) {
      el.querySelector(".src-name").textContent = roles[i];
    });
    body.querySelectorAll(".stats-top-item").forEach(function (el, i) {
      el.querySelector(".sti-title").textContent = top[i].title;
    });
  }
  function toggleProviderFields() {
    var p = $("setProvider").value;
    var isOllama = p === "ollama";
    $("fieldBaseUrl").style.display = isOllama ? "none" : "";
    $("fieldApiKey").style.display = isOllama ? "none" : "";
    $("fieldOllamaUrl").style.display = isOllama ? "" : "none";
  }
  function saveSettings() {
    var showReasoning = $("setShowReasoning");
    var activePersona = $("setActivePersona");
    var body = {
      provider: $("setProvider").value,
      base_url: $("setBaseUrl").value,
      ollama_url: $("setOllamaUrl").value,
      model: $("setModel").value,
      agent_max_steps: parseInt($("setAgentSteps").value, 10) || 12,
      agent_temperature: parseFloat($("setAgentTemp").value) || 0.2,
      custom_system_prompt: $("setCustomSystemPrompt").value,
      show_reasoning: showReasoning ? !!showReasoning.checked : false,
      active_persona: activePersona ? activePersona.value : "default",
    };
    state.activePersona = body.active_persona;
    var key = $("setApiKey").value;
    if (key) body.api_key = key;
    api("/api/settings", { method: "PUT", body: JSON.stringify(body) }).then(function () {
      closeSettings();
      toast("Settings saved", "success");
      loadModels();
    }).catch(function (e) { toast(e.message, "error"); });
  }

  // ---------- Mobile sidebar ----------
  function openMobileSidebar() { $("sidebar").classList.add("open"); showBackdrop(true); }
  function closeMobileSidebar() { $("sidebar").classList.remove("open"); showBackdrop(false); }
  function showBackdrop(on) {
    var bd = document.querySelector(".backdrop");
    if (!bd) {
      bd = document.createElement("div");
      bd.className = "backdrop";
      bd.addEventListener("click", closeMobileSidebar);
      document.body.appendChild(bd);
    }
    bd.classList.toggle("show", on);
  }

  // ---------- Command palette (Cmd/Ctrl+K) ----------
  var cmdState = { items: [], active: 0, open: false };

  function cmdActions() {
    return [
      { id: "new-chat", label: "New chat", hint: "chat mode", icon: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>', run: function () { newSession("chat"); } },
      { id: "new-agent", label: "New agent task", hint: "agent mode", icon: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 1 4 4v1a3 3 0 0 1 3 3v3a8 8 0 0 1-16 0v-3a3 3 0 0 1 3-3V6a4 4 0 0 1 4-4z"/></svg>', run: function () { newSession("agent"); } },
      { id: "settings", label: "Open settings", hint: "config", icon: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>', run: function () { openSettings(); } },
      { id: "stats", label: "Open stats", hint: "usage", icon: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18M18 17V9M13 17V5M8 17v-3"/></svg>', run: function () { openStats(); } },
      { id: "search", label: "Search conversations", hint: "⌘F", icon: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>', run: function () { openSearch(); } },
      { id: "starred", label: "View starred messages", hint: "bookmarks", icon: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>', run: function () { openStarred(); } },
      { id: "refresh-models", label: "Refresh models", hint: "reload list", icon: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>', run: function () { loadModels().then(function () { toast("Models refreshed", "success"); }); } },
    ].concat(state.sessions.slice(0, 8).map(function (s) {
      return {
        id: "open-" + s.id,
        label: s.title,
        hint: s.mode === "agent" ? "agent · open" : "chat · open",
        icon: s.mode === "agent"
          ? '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 1 4 4v1a3 3 0 0 1 3 3v3a8 8 0 0 1-16 0v-3a3 3 0 0 1 3-3V6a4 4 0 0 1 4-4z"/></svg>'
          : '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
        run: function () { selectSession(s.id); },
      };
    }));
  }

  function openCmdPalette() {
    cmdState.open = true;
    cmdState.active = 0;
    $("cmdPalette").hidden = false;
    $("cmdInput").value = "";
    renderCmdList("");
    setTimeout(function () { $("cmdInput").focus(); }, 10);
  }

  function closeCmdPalette() {
    cmdState.open = false;
    $("cmdPalette").hidden = true;
  }

  function renderCmdList(query) {
    var all = cmdActions();
    var q = query.trim().toLowerCase();
    cmdState.items = q
      ? all.filter(function (a) { return a.label.toLowerCase().indexOf(q) !== -1 || a.hint.toLowerCase().indexOf(q) !== -1; })
      : all;
    if (cmdState.active >= cmdState.items.length) cmdState.active = 0;
    var list = $("cmdList");
    if (!cmdState.items.length) {
      list.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-dim);font-size:13px;">No matching commands</div>';
      return;
    }
    list.innerHTML = cmdState.items.map(function (a, i) {
      var active = i === cmdState.active ? " active" : "";
      return '<div class="cmd-item' + active + '" data-ci="' + i + '"><span class="ci-icon">' + a.icon + "</span><span class=\"ci-label\"></span><span class=\"ci-hint\"></span></div>";
    }).join("");
    list.querySelectorAll(".cmd-item").forEach(function (el, i) {
      el.querySelector(".ci-label").textContent = cmdState.items[i].label;
      el.querySelector(".ci-hint").textContent = cmdState.items[i].hint;
      el.addEventListener("mouseenter", function () { cmdState.active = i; renderCmdList(q); });
      el.addEventListener("click", function () { runCmd(i); });
    });
  }

  function runCmd(i) {
    var a = cmdState.items[i];
    if (!a) return;
    closeCmdPalette();
    a.run();
  }

  // ---------- Wire up ----------
  function init() {
    $("newChatBtn").addEventListener("click", function () { newSession("chat"); });
    $("newAgentBtn").addEventListener("click", function () { newSession("agent"); });
    $("settingsBtn").addEventListener("click", openSettings);
    $("cmdPaletteBtn").addEventListener("click", openCmdPalette);
    $("statsBtn").addEventListener("click", openStats);
    $("closeStats").addEventListener("click", closeStats);
    $("searchBtn").addEventListener("click", openSearch);
    $("closeSearch").addEventListener("click", closeSearch);
    $("starredBtn").addEventListener("click", openStarred);
    $("closeStarred").addEventListener("click", closeStarred);
    $("importBtn").addEventListener("click", function () { $("importFileInput").click(); });
    $("importFileInput").addEventListener("change", function (e) {
      if (e.target.files && e.target.files[0]) handleImportFile(e.target.files[0]);
      e.target.value = "";
    });
    $("searchInput").addEventListener("input", function () { runSearch($("searchInput").value); });
    $("statsClose").addEventListener("click", closeStats);
    $("backupBtn").addEventListener("click", function () {
      var a = document.createElement("a");
      a.href = "/api/export/all.zip";
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast("Backing up all conversations…", "success");
    });
    $("closeSettings").addEventListener("click", closeSettings);
    $("settingsCancel").addEventListener("click", closeSettings);
    $("settingsSave").addEventListener("click", saveSettings);
    $("setProvider").addEventListener("change", toggleProviderFields);
    // Persona editor wiring.
    $("addPersonaBtn").addEventListener("click", function () { openPersonaEditor(""); });
    $("closePersonaEditor").addEventListener("click", closePersonaEditor);
    $("personaEditorCancel").addEventListener("click", closePersonaEditor);
    $("personaEditorSave").addEventListener("click", savePersonaEditor);
    $("personaEditorModal").addEventListener("click", function (e) {
      if (e.target === $("personaEditorModal")) closePersonaEditor();
    });
    $("refreshModelsBtn").addEventListener("click", function () { loadModels().then(function () { toast("Models refreshed", "success"); }); });
    $("menuBtn").addEventListener("click", openMobileSidebar);
    $("recentToggle").addEventListener("click", function () {
      $("recentSection").classList.toggle("collapsed");
    });

    $("sessionsList").addEventListener("click", function (e) {
      var del = e.target.closest("[data-del]");
      if (del) { e.stopPropagation(); deleteSession(del.getAttribute("data-del")); return; }
      var pin = e.target.closest("[data-pin]");
      if (pin) { e.stopPropagation(); togglePin(pin.getAttribute("data-pin")); return; }
      var more = e.target.closest("[data-more]");
      if (more) { e.stopPropagation(); openMenu(more.getAttribute("data-more"), more); return; }
      var item = e.target.closest(".session-item");
      if (item) selectSession(item.getAttribute("data-id"));
    });

    var input = $("composerInput");
    input.addEventListener("input", function () { autoResize(); updateSendState(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });
    $("sendBtn").addEventListener("click", send);
    $("stopBtn").addEventListener("click", stopStreaming);

    // Drag-and-drop file uploads onto the composer.
    var composer = document.querySelector(".composer-inner");
    if (composer) {
      composer.addEventListener("dragover", function (e) {
        e.preventDefault();
        var dz = $("dropZone");
        if (dz) { dz.style.display = "flex"; dz.classList.add("dragover"); }
      });
      composer.addEventListener("dragleave", function (e) {
        if (e.target === composer) {
          var dz = $("dropZone");
          if (dz) { dz.style.display = "none"; dz.classList.remove("dragover"); }
        }
      });
      composer.addEventListener("drop", function (e) {
        e.preventDefault();
        var dz = $("dropZone");
        if (dz) { dz.style.display = "none"; dz.classList.remove("dragover"); }
        if (e.dataTransfer.files && e.dataTransfer.files.length) {
          handleFileUpload(e.dataTransfer.files);
        }
      });
    }

    // Close modal on overlay click.
    $("settingsModal").addEventListener("click", function (e) {
      if (e.target === $("settingsModal")) closeSettings();
    });
    $("statsModal").addEventListener("click", function (e) {
      if (e.target === $("statsModal")) closeStats();
    });
    $("searchModal").addEventListener("click", function (e) {
      if (e.target === $("searchModal")) closeSearch();
    });
    $("starredModal").addEventListener("click", function (e) {
      if (e.target === $("starredModal")) closeStarred();
    });

    // Command palette: Cmd/Ctrl+K to open, keyboard nav inside. Cmd/Ctrl+F = search.
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (cmdState.open) closeCmdPalette();
        else openCmdPalette();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        openSearch();
        return;
      }
      if (e.key === "Escape" && cmdState.open) {
        closeCmdPalette();
        return;
      }
      if (!cmdState.open) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        cmdState.active = Math.min(cmdState.items.length - 1, cmdState.active + 1);
        renderCmdList($("cmdInput").value);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        cmdState.active = Math.max(0, cmdState.active - 1);
        renderCmdList($("cmdInput").value);
      } else if (e.key === "Enter") {
        e.preventDefault();
        runCmd(cmdState.active);
      }
    });
    $("cmdInput").addEventListener("input", function () {
      cmdState.active = 0;
      renderCmdList($("cmdInput").value);
    });
    $("cmdPalette").addEventListener("click", function (e) {
      if (e.target === $("cmdPalette")) closeCmdPalette();
    });

    renderWelcome();
    loadSessions();
    loadModels();
    loadTools();
    loadPersonas();
    loadTags();
    loadRecent();
    // Load active persona from settings so the chat composer uses the user's
    // global default until they pick one for a specific session.
    apiJson("/api/settings").then(function (s) {
      if (s.active_persona) state.activePersona = s.active_persona;
    }).catch(function () {});
    updateContextCounter();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();

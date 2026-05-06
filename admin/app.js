(function () {
  const panelConfig = window.ADMIN_PANEL_CONFIG || {};
  const state = {
    dashboard: null,
    posts: [],
    comments: [],
    reports: [],
    admins: [],
    channelRequests: [],
    channels: [],
    selectedChannelId: "",
    activeTab: "dashboard",
    initData: "",
    insideMax: false,
    apiBase: panelConfig.apiBase || "/api/admin",
    mode: panelConfig.mode || "channel",
  };

  const $ = (selector) => document.querySelector(selector);
  const webApp = window.WebApp || null;

  const views = {
    login: $("#login-view"),
    denied: $("#denied-view"),
    admin: $("#admin-view"),
  };

  const reasonLabels = {
    insult: "Оскорбление",
    profanity: "Ненормативная лексика",
    threat: "Угроза",
    spam: "Спам",
    hate: "Дискриминация или ненависть",
    other: "Другое",
  };

  const statusLabels = {
    active: "Активный",
    deleted: "Удалён",
    hidden: "Скрыт",
    new: "Новая",
    in_review: "В работе",
    accepted: "Принята",
    rejected: "Отклонена",
    pending: "Ожидает",
    approved: "Одобрена",
    cancelled: "Отменена",
    duplicate: "Дубль",
    published: "Опубликован",
  };

  function hideAllViews() {
    views.login.classList.add("hidden");
    views.denied.classList.add("hidden");
    views.admin.classList.add("hidden");
  }

  function showLogin(message = "") {
    hideAllViews();
    views.login.classList.remove("hidden");
    $("#login-error").textContent = message;
  }

  function showDenied() {
    hideAllViews();
    views.denied.classList.remove("hidden");
  }

  function showAdmin() {
    hideAllViews();
    views.admin.classList.remove("hidden");
  }

  function setNotice(message, type = "success") {
    const box = $("#notice");
    if (!message) {
      box.classList.add("hidden");
      box.classList.remove("error");
      box.textContent = "";
      return;
    }
    box.textContent = message;
    box.classList.toggle("error", type === "error");
    box.classList.remove("hidden");
  }

  function buildHeaders(extraHeaders = {}) {
    const headers = { ...extraHeaders };
    if (state.initData) {
      headers["X-Max-Init-Data"] = state.initData;
    }
    return headers;
  }

  async function requestJson(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      credentials: "same-origin",
      headers: buildHeaders({
        "Content-Type": "application/json",
        ...(options.headers || {}),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.message || payload.error || "Ошибка запроса");
      error.status = response.status;
      error.code = payload.error || "";
      throw error;
    }
    return payload;
  }

  function formatDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function channelQuery() {
    return state.selectedChannelId
      ? `?channel_id=${encodeURIComponent(state.selectedChannelId)}`
      : "";
  }

  function isSuperAdmin() {
    return state.mode === "super" && state.dashboard?.admin?.role === "super_admin";
  }

  function isSuperPanel() {
    return state.mode === "super";
  }

  function statusPill(status) {
    const tone = status === "active" || status === "published" || status === "approved"
      ? "success"
      : status === "new" || status === "pending"
        ? "danger"
        : "";
    return `<span class="pill ${tone}">${escapeHtml(statusLabels[status] || status || "-")}</span>`;
  }

  function renderChannelSelect() {
    const select = $("#channel-select");
    select.innerHTML = "";
    if (isSuperPanel()) {
      const allOption = document.createElement("option");
      allOption.value = "";
      allOption.textContent = "Все каналы";
      select.append(allOption);
    }
    for (const channel of state.channels) {
      const option = document.createElement("option");
      option.value = channel.channel_chat_id;
      option.textContent = channel.channel_label || String(channel.channel_chat_id);
      select.append(option);
    }
    if (state.selectedChannelId) {
      select.value = state.selectedChannelId;
    }
    select.disabled = !isSuperPanel() && state.channels.length <= 1;
  }

  function renderRoleVisibility() {
    const superAdmin = isSuperAdmin();
    document.querySelectorAll(".super-only").forEach((node) => {
      node.hidden = !superAdmin;
    });
    const channelTab = document.querySelector('[data-tab="channels"]');
    if (channelTab) {
      channelTab.textContent = superAdmin ? "Все каналы" : "Мои каналы";
    }
    if (!superAdmin && ["admins", "settings", "requests"].includes(state.activeTab)) {
      setActiveTab("dashboard");
    }
  }

  function renderPasswordWarning(admin) {
    const warning = $("#password-warning");
    if (!warning) return;
    warning.classList.toggle("hidden", !admin?.must_change_password);
  }

  function renderChannelsList() {
    const list = $("#channels-list");
    list.innerHTML = "";
    if (!state.channels.length) {
      list.innerHTML = '<div class="empty">Каналы не найдены.</div>';
      return;
    }
    for (const channel of state.channels) {
      const item = document.createElement("article");
      item.className = "item";
      item.innerHTML = `
        <div class="item-row">
          <div>
            <div class="item-title">${escapeHtml(channel.channel_label || channel.channel_chat_id)}</div>
            <div class="item-meta">Канал: ${escapeHtml(channel.channel_chat_id)}</div>
            <div class="item-meta">Чат комментариев: ${escapeHtml(channel.comments_chat_label || channel.comments_chat_id)}</div>
          </div>
          <span class="pill">${channel.same_chat ? "один чат" : "отдельный чат"}</span>
        </div>
        <div class="item-actions">
          ${channel.channel_url ? `<a class="action-link" href="${escapeHtml(channel.channel_url)}" target="_blank" rel="noreferrer">Открыть канал</a>` : ""}
          ${channel.comments_chat_url ? `<a class="action-link" href="${escapeHtml(channel.comments_chat_url)}" target="_blank" rel="noreferrer">Открыть чат</a>` : ""}
          ${isSuperAdmin() ? `<button class="danger-button" type="button" data-remove-channel="${channel.channel_chat_id}">Удалить привязку</button>` : ""}
        </div>
      `;
      list.append(item);
    }
  }

  function renderSettings() {
    const list = $("#settings-list");
    if (!list) return;
    const app = state.dashboard?.app || {};
    const rows = [
      ["Версия", app.version || "-"],
      ["Режим доставки", app.delivery_mode || "-"],
      ["Webhook URL", app.webhook_public_url || "-"],
      ["Webhook path", app.webhook_path || "-"],
      ["WebApp URL", app.web_app_public_url || "-"],
      ["Бот", app.bot_username ? `@${app.bot_username}` : "-"],
      ["Администраторов", app.admin_count || 0],
    ];
    list.innerHTML = rows.map(([title, value]) => `
      <article class="item">
        <div class="item-row">
          <div class="item-title">${escapeHtml(title)}</div>
          <div class="item-meta">${escapeHtml(value)}</div>
        </div>
      </article>
    `).join("");
  }

  function renderDashboard(payload) {
    state.dashboard = payload;
    state.channels = Array.isArray(payload.channels) ? payload.channels : [];
    const stats = payload.stats || {};
    $("#admin-role").textContent = payload.admin?.role === "super_admin"
      ? "Панель супер-администратора"
      : "Роль: администратор канала";
    $("#stat-users").textContent = String(stats.usersCount || 0);
    $("#stat-posts").textContent = String(stats.postsCount || 0);
    $("#stat-comments").textContent = String(stats.commentsCount || 0);
    $("#stat-reports").textContent = String(stats.reportsCount || 0);
    $("#stat-new-reports").textContent = String(stats.newReportsCount || 0);
    $("#stat-deleted-comments").textContent = String(stats.deletedCommentsCount || 0);
    const newRequests = $("#stat-new-requests");
    if (newRequests) newRequests.textContent = String(stats.newRequestsCount || 0);
    renderChannelSelect();
    renderRoleVisibility();
    renderPasswordWarning(payload.admin);
    renderChannelsList();
    renderSettings();
    renderMiniList("#latest-posts", payload.latestPosts || [], renderPostItem);
    renderMiniList("#latest-comments", payload.latestComments || [], renderCommentItem);
    renderMiniList("#latest-reports", payload.latestReports || [], renderReportItem);
    renderMiniList("#latest-requests", payload.latestRequests || [], renderChannelRequestItem);
  }

  function renderMiniList(selector, items, renderer) {
    const list = $(selector);
    if (!list) return;
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = '<div class="empty">Пока нет данных.</div>';
      return;
    }
    items.forEach((item) => list.append(renderer(item, true)));
  }

  function renderPostItem(post) {
    const item = document.createElement("article");
    item.className = "item";
    item.innerHTML = `
      <div class="item-row">
        <div>
          <div class="item-title">${escapeHtml(post.title || "Пост без заголовка")}</div>
          <div class="item-meta">${formatDate(post.created_at)} · ${post.comment_count || 0} комментариев</div>
        </div>
        ${statusPill(post.status)}
      </div>
      <div class="item-text">${escapeHtml(post.content || "").slice(0, 280)}</div>
      <div class="item-actions">
        ${post.webapp_url ? `<a class="action-link" href="${escapeHtml(post.webapp_url)}" target="_blank" rel="noreferrer">Открыть</a>` : ""}
        <button class="danger-button" type="button" data-delete-post="${escapeHtml(post.post_message_id)}">Удалить</button>
      </div>
    `;
    return item;
  }

  function renderCommentItem(comment) {
    const item = document.createElement("article");
    item.className = "item";
    item.innerHTML = `
      <div class="item-row">
        <div>
          <div class="item-title">${escapeHtml(comment.author || "Пользователь")}</div>
          <div class="item-meta">${formatDate(comment.created_at)} · жалоб: ${comment.reports_count || 0}</div>
          <div class="item-meta">Пост: ${escapeHtml(comment.post_title || comment.post_preview || "-")}</div>
        </div>
        ${statusPill(comment.status)}
      </div>
      <div class="item-text">${escapeHtml(comment.text || "Фото/медиа")}</div>
      <div class="item-actions">
        ${
          comment.status === "active"
            ? `<button class="danger-button" type="button" data-delete-comment="${comment.id}">Удалить</button>`
            : `<button class="ghost-button" type="button" data-restore-comment="${comment.id}">Восстановить</button>`
        }
      </div>
    `;
    return item;
  }

  function renderReportItem(report) {
    const item = document.createElement("article");
    item.className = "item";
    item.innerHTML = `
      <div class="item-row">
        <div>
          <div class="item-title">${escapeHtml(reasonLabels[report.reason] || report.reason)}</div>
          <div class="item-meta">${formatDate(report.created_at)} · жалоб на комментарий: ${report.reports_count || 0}</div>
          <div class="item-meta">Автор комментария: ${escapeHtml(report.comment_author || "-")}</div>
        </div>
        ${statusPill(report.status)}
      </div>
      <div class="item-text">${escapeHtml(report.comment_text || "")}</div>
      ${report.details ? `<div class="item-meta">Описание: ${escapeHtml(report.details)}</div>` : ""}
      ${report.admin_comment ? `<div class="item-meta">Комментарий администратора: ${escapeHtml(report.admin_comment)}</div>` : ""}
      <div class="item-actions">
        <button class="ghost-button" type="button" data-review-report="${report.id}">В работу</button>
        <button class="danger-button" type="button" data-accept-report="${report.id}">Принять и удалить</button>
        <button class="ghost-button" type="button" data-reject-report="${report.id}">Отклонить</button>
      </div>
    `;
    return item;
  }

  function requestCommentValue(requestId) {
    const input = Array.from(document.querySelectorAll("[data-request-comment]"))
      .find((node) => String(node.dataset.requestComment) === String(requestId));
    return input ? input.value.trim() : "";
  }

  function renderChannelRequestItem(request) {
    const item = document.createElement("article");
    item.className = "item";
    const channelTitle = request.channelTitle || request.channel_title || request.channelId || request.channel_id || "Канал";
    const channelId = request.channelId || request.channel_id || "-";
    const requester = request.requesterMaxUserId || request.requester_max_user_id || "-";
    const forwardedPost = request.forwardedPostId || request.forwarded_post_id || "";
    const isPending = request.status === "pending";
    item.innerHTML = `
      <div class="item-row">
        <div>
          <div class="item-title">${escapeHtml(channelTitle)}</div>
          <div class="item-meta">ID канала: ${escapeHtml(channelId)} · заявитель: ${escapeHtml(requester)}</div>
          <div class="item-meta">Дата: ${formatDate(request.createdAt || request.created_at)}</div>
          ${forwardedPost ? `<div class="item-meta">Пост: ${escapeHtml(forwardedPost)}</div>` : ""}
        </div>
        ${statusPill(request.status)}
      </div>
      ${request.adminComment || request.admin_comment ? `<div class="item-meta">Комментарий: ${escapeHtml(request.adminComment || request.admin_comment)}</div>` : ""}
      ${
        isPending
          ? `
            <label class="field">
              <span>Комментарий супер-админа</span>
              <textarea rows="2" data-request-comment="${escapeHtml(request.id)}" placeholder="Необязательно"></textarea>
            </label>
            <div class="item-actions">
              <button class="primary-button" type="button" data-approve-channel-request="${escapeHtml(request.id)}">Одобрить</button>
              <button class="danger-button" type="button" data-reject-channel-request="${escapeHtml(request.id)}">Отклонить</button>
            </div>
          `
          : ""
      }
    `;
    return item;
  }

  function renderPosts() {
    const list = $("#posts-list");
    list.innerHTML = "";
    if (!state.posts.length) {
      list.innerHTML = '<div class="empty">Постов пока нет.</div>';
      return;
    }
    state.posts.forEach((post) => list.append(renderPostItem(post)));
  }

  function renderComments() {
    const list = $("#comments-list");
    list.innerHTML = "";
    if (!state.comments.length) {
      list.innerHTML = '<div class="empty">Комментарии не найдены.</div>';
      return;
    }
    state.comments.forEach((comment) => list.append(renderCommentItem(comment)));
  }

  function renderReports() {
    const list = $("#reports-list");
    list.innerHTML = "";
    if (!state.reports.length) {
      list.innerHTML = '<div class="empty">Жалоб по выбранному фильтру нет.</div>';
      return;
    }
    state.reports.forEach((report) => list.append(renderReportItem(report)));
  }

  function renderChannelRequests() {
    const list = $("#channel-requests-list");
    if (!list) return;
    list.innerHTML = "";
    if (!state.channelRequests.length) {
      list.innerHTML = '<div class="empty">Заявок по выбранному фильтру нет.</div>';
      return;
    }
    state.channelRequests.forEach((request) => list.append(renderChannelRequestItem(request)));
  }

  function renderAdminUserChannelOptions() {
    const select = $("#admin-user-channels");
    if (!select) return;
    select.innerHTML = "";
    for (const channel of state.channels) {
      const option = document.createElement("option");
      option.value = channel.channel_chat_id;
      option.textContent = channel.channel_label || String(channel.channel_chat_id);
      select.append(option);
    }
  }

  function renderAdmins() {
    const list = $("#admins-list");
    if (!list) return;
    renderAdminUserChannelOptions();
    list.innerHTML = "";
    if (!state.admins.length) {
      list.innerHTML = '<div class="empty">Администраторы ещё не добавлены.</div>';
      return;
    }
    for (const admin of state.admins) {
      const channelText = admin.role === "super_admin"
        ? "Все каналы"
        : (admin.channel_ids || []).join(", ") || "Каналы не назначены";
      const item = document.createElement("article");
      item.className = "item";
      item.innerHTML = `
        <div class="item-row">
          <div>
            <div class="item-title">${escapeHtml(admin.max_user_id)}</div>
            <div class="item-meta">Роль: ${escapeHtml(admin.role)} · ${admin.is_active ? "активен" : "отключён"}</div>
            <div class="item-meta">Каналы: ${escapeHtml(channelText)}</div>
            ${admin.must_change_password ? '<div class="item-meta">Пароль по умолчанию, нужна смена</div>' : ""}
          </div>
          ${statusPill(admin.is_active ? "active" : "deleted")}
        </div>
        <div class="item-actions">
          <button class="ghost-button" type="button" data-edit-admin="${admin.id}">Редактировать</button>
          <button class="ghost-button" type="button" data-reset-admin-password="${admin.id}">Сбросить пароль</button>
          <button class="danger-button" type="button" data-toggle-admin="${admin.id}" data-next-active="${admin.is_active ? "0" : "1"}">
            ${admin.is_active ? "Отключить" : "Включить"}
          </button>
        </div>
      `;
      list.append(item);
    }
  }

  function renderPostFilter() {
    const select = $("#comments-post");
    const currentValue = select.value;
    select.innerHTML = '<option value="">Все посты</option>';
    state.posts.forEach((post) => {
      const option = document.createElement("option");
      option.value = post.post_message_id;
      option.textContent = post.title || post.post_message_id;
      select.append(option);
    });
    select.value = Array.from(select.options).some((option) => option.value === currentValue) ? currentValue : "";
  }

  async function loadDashboard() {
    const payload = await requestJson(`${state.apiBase}/dashboard${channelQuery()}`);
    const channels = Array.isArray(payload.channels) ? payload.channels : [];
    if (!isSuperPanel()) {
      const nextSelected = channels.some((channel) => String(channel.channel_chat_id) === String(state.selectedChannelId))
        ? state.selectedChannelId
        : String(channels[0]?.channel_chat_id || "");
      if (nextSelected && nextSelected !== state.selectedChannelId) {
        state.selectedChannelId = nextSelected;
        return loadDashboard();
      }
    }
    renderDashboard(payload);
    showAdmin();
  }

  async function loadPosts() {
    const payload = await requestJson(`${state.apiBase}/posts${channelQuery()}`);
    state.posts = Array.isArray(payload.posts) ? payload.posts : [];
    renderPosts();
    renderPostFilter();
  }

  async function loadComments() {
    const params = new URLSearchParams();
    if (state.selectedChannelId) params.set("channel_id", state.selectedChannelId);
    if ($("#comments-status").value) params.set("status", $("#comments-status").value);
    if ($("#comments-post").value) params.set("post_id", $("#comments-post").value);
    const payload = await requestJson(`${state.apiBase}/comments?${params.toString()}`);
    state.comments = Array.isArray(payload.comments) ? payload.comments : [];
    renderComments();
  }

  async function loadReports() {
    const params = new URLSearchParams();
    if (state.selectedChannelId) params.set("channel_id", state.selectedChannelId);
    if ($("#reports-status").value) params.set("status", $("#reports-status").value);
    const payload = await requestJson(`${state.apiBase}/reports?${params.toString()}`);
    state.reports = Array.isArray(payload.reports) ? payload.reports : [];
    renderReports();
  }

  async function loadAdmins() {
    if (!isSuperAdmin()) {
      state.admins = [];
      renderAdmins();
      return;
    }
    const payload = await requestJson(`${state.apiBase}/users`);
    state.admins = Array.isArray(payload.users) ? payload.users : [];
    renderAdmins();
  }

  async function loadChannelRequests() {
    if (!isSuperAdmin()) {
      state.channelRequests = [];
      renderChannelRequests();
      return;
    }
    const params = new URLSearchParams();
    const statusSelect = $("#channel-requests-status");
    if (statusSelect?.value) params.set("status", statusSelect.value);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    const payload = await requestJson(`${state.apiBase}/channel-requests${suffix}`);
    state.channelRequests = Array.isArray(payload.requests) ? payload.requests : [];
    renderChannelRequests();
  }

  async function loadAll() {
    try {
      await loadDashboard();
      await loadPosts();
      await loadComments();
      await loadReports();
      await loadAdmins();
      await loadChannelRequests();
      setNotice("");
    } catch (error) {
      if (error.status === 401) {
        showLogin("");
        return;
      }
      if (error.status === 403) {
        showDenied();
        return;
      }
      setNotice(error.message, "error");
      if (!state.dashboard) {
        showLogin(error.message);
      }
    }
  }

  function setActiveTab(tabName) {
    state.activeTab = tabName;
    document.querySelectorAll(".tab").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.tab === tabName);
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("hidden", panel.id !== `${tabName}-tab`);
    });
  }

  async function handleLogin(event) {
    event.preventDefault();
    $("#login-error").textContent = "";
    try {
      await requestJson(`${state.apiBase}/auth/login`, {
        method: "POST",
        body: JSON.stringify({
          login: $("#login-user-id").value.trim(),
          password: $("#login-password").value,
        }),
      });
      $("#login-user-id").value = "";
      $("#login-password").value = "";
      await loadAll();
    } catch (error) {
      showLogin(error.message);
    }
  }

  async function handleLogout() {
    await requestJson(`${state.apiBase}/auth/logout`, { method: "POST", body: "{}" }).catch(() => null);
    state.dashboard = null;
    if (state.insideMax) {
      await loadAll();
    } else {
      showLogin("");
    }
  }

  async function handleCreatePost(event) {
    event.preventDefault();
    setNotice("");
    try {
      const payload = {
        channel_id: state.selectedChannelId,
        title: $("#post-title").value.trim(),
        content: $("#post-content").value.trim(),
      };
      const result = await requestJson(`${state.apiBase}/posts`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      $("#post-title").value = "";
      $("#post-content").value = "";
      await loadAll();
      setNotice(`Пост опубликован: ${result.post.post_message_id}`);
      setActiveTab("posts");
    } catch (error) {
      setNotice(error.message, "error");
    }
  }

  async function handleChangePassword(event) {
    event.preventDefault();
    setNotice("");
    try {
      await requestJson(`${state.apiBase}/auth/change-password`, {
        method: "POST",
        body: JSON.stringify({
          oldPassword: $("#old-password").value,
          newPassword: $("#new-password").value,
        }),
      });
      $("#old-password").value = "";
      $("#new-password").value = "";
      await loadAll();
      setNotice("Пароль изменён.");
    } catch (error) {
      setNotice(error.message, "error");
    }
  }

  async function handleChannelSubmit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    setNotice("");
    try {
      const result = await requestJson(`${state.apiBase}/channels`, {
        method: "POST",
        body: JSON.stringify({
          channel_chat_id: $("#channel-id").value.trim(),
          comments_chat_id: $("#comments-chat-id").value.trim(),
          comments_chat_url: $("#comments-chat-url").value.trim(),
          sync_now: true,
        }),
      });
      if (form instanceof HTMLFormElement) {
        form.reset();
      }
      await loadAll();
      setNotice(`Канал подключён. Подцеплено постов: ${result.attached_count}`);
    } catch (error) {
      setNotice(error.message, "error");
    }
  }

  async function handleSync() {
    setNotice("");
    try {
      const result = await requestJson(`${state.apiBase}/sync`, { method: "POST", body: "{}" });
      await loadAll();
      setNotice(`Синхронизация завершена. Подцеплено постов: ${result.attached_count}`);
    } catch (error) {
      setNotice(error.message, "error");
    }
  }

  function selectedAdminChannelIds() {
    return Array.from($("#admin-user-channels").selectedOptions).map((option) => option.value);
  }

  async function handleAdminUserSubmit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    setNotice("");
    try {
      await requestJson(`${state.apiBase}/users`, {
        method: "POST",
        body: JSON.stringify({
          max_user_id: $("#admin-user-id").value.trim(),
          role: $("#admin-user-role").value,
          channel_ids: selectedAdminChannelIds(),
          is_active: true,
        }),
      });
      if (form instanceof HTMLFormElement) {
        form.reset();
      }
      await loadAll();
      setNotice("Администратор сохранён, выбранные каналы добавлены. Пароль по умолчанию равен MAX user id.");
    } catch (error) {
      setNotice(error.message, "error");
    }
  }

  async function setCommentStatus(commentId, status, reason) {
    await requestJson(`${state.apiBase}/comments/${encodeURIComponent(commentId)}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status, reason }),
    });
    await loadAll();
  }

  async function updateReport(reportId, payload) {
    await requestJson(`${state.apiBase}/reports/${encodeURIComponent(reportId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    await loadAll();
  }

  async function handleAdminClick(event) {
    const deletePost = event.target.closest("[data-delete-post]");
    const deleteComment = event.target.closest("[data-delete-comment]");
    const restoreComment = event.target.closest("[data-restore-comment]");
    const reviewReport = event.target.closest("[data-review-report]");
    const acceptReport = event.target.closest("[data-accept-report]");
    const rejectReport = event.target.closest("[data-reject-report]");
    const removeChannel = event.target.closest("[data-remove-channel]");
    const editAdmin = event.target.closest("[data-edit-admin]");
    const resetAdminPassword = event.target.closest("[data-reset-admin-password]");
    const toggleAdmin = event.target.closest("[data-toggle-admin]");
    const approveChannelRequest = event.target.closest("[data-approve-channel-request]");
    const rejectChannelRequest = event.target.closest("[data-reject-channel-request]");
    try {
      if (approveChannelRequest) {
        const requestId = approveChannelRequest.dataset.approveChannelRequest;
        await requestJson(`${state.apiBase}/channel-requests/${encodeURIComponent(requestId)}/approve`, {
          method: "POST",
          body: JSON.stringify({ adminComment: requestCommentValue(requestId) }),
        });
        await loadAll();
        setNotice("Заявка одобрена, канал подключён.");
        setActiveTab("requests");
        return;
      }
      if (rejectChannelRequest) {
        const requestId = rejectChannelRequest.dataset.rejectChannelRequest;
        await requestJson(`${state.apiBase}/channel-requests/${encodeURIComponent(requestId)}/reject`, {
          method: "POST",
          body: JSON.stringify({ adminComment: requestCommentValue(requestId) }),
        });
        await loadAll();
        setNotice("Заявка отклонена.");
        setActiveTab("requests");
        return;
      }
      if (removeChannel) {
        if (!window.confirm(`Удалить привязку канала ${removeChannel.dataset.removeChannel}?`)) return;
        await requestJson(`${state.apiBase}/channels/${encodeURIComponent(removeChannel.dataset.removeChannel)}`, {
          method: "DELETE",
        });
        await loadAll();
        setNotice("Привязка канала удалена.");
        return;
      }
      if (editAdmin) {
        const admin = state.admins.find((item) => String(item.id) === String(editAdmin.dataset.editAdmin));
        if (!admin) return;
        $("#admin-user-id").value = admin.max_user_id;
        $("#admin-user-role").value = admin.role;
        renderAdminUserChannelOptions();
        Array.from($("#admin-user-channels").options).forEach((option) => {
          option.selected = (admin.channel_ids || []).map(String).includes(String(option.value));
        });
        setActiveTab("admins");
        return;
      }
      if (resetAdminPassword) {
        if (!window.confirm("Сбросить пароль администратора к MAX user id?")) return;
        await requestJson(`${state.apiBase}/users/${encodeURIComponent(resetAdminPassword.dataset.resetAdminPassword)}/reset-password`, {
          method: "POST",
          body: "{}",
        });
        await loadAll();
        setNotice("Пароль сброшен к MAX user id.");
        return;
      }
      if (toggleAdmin) {
        await requestJson(`${state.apiBase}/users/${encodeURIComponent(toggleAdmin.dataset.toggleAdmin)}`, {
          method: "PATCH",
          body: JSON.stringify({
            is_active: toggleAdmin.dataset.nextActive === "1",
          }),
        });
        await loadAll();
        setNotice("Статус администратора обновлён.");
        return;
      }
      if (deletePost) {
        if (!window.confirm("Удалить пост из админки? Комментарии к нему перестанут открываться.")) return;
        await requestJson(`${state.apiBase}/posts/${encodeURIComponent(deletePost.dataset.deletePost)}`, { method: "DELETE" });
        await loadAll();
        setNotice("Пост удалён.");
        return;
      }
      if (deleteComment) {
        const reason = window.prompt("Причина удаления", "Нарушение правил общения") || "Нарушение правил общения";
        await setCommentStatus(deleteComment.dataset.deleteComment, "deleted", reason);
        setNotice("Комментарий удалён.");
        return;
      }
      if (restoreComment) {
        await setCommentStatus(restoreComment.dataset.restoreComment, "active", "Восстановлено администратором");
        setNotice("Комментарий восстановлен.");
        return;
      }
      if (reviewReport) {
        await updateReport(reviewReport.dataset.reviewReport, { status: "in_review" });
        setNotice("Жалоба взята в работу.");
        return;
      }
      if (acceptReport) {
        const adminComment = window.prompt("Комментарий администратора", "Комментарий содержит нарушение правил общения") || "";
        await updateReport(acceptReport.dataset.acceptReport, {
          status: "accepted",
          action: "delete_comment",
          adminComment,
        });
        setNotice("Жалоба принята, комментарий удалён.");
        return;
      }
      if (rejectReport) {
        const adminComment = window.prompt("Комментарий администратора", "Нарушение не подтверждено") || "";
        await updateReport(rejectReport.dataset.rejectReport, {
          status: "rejected",
          adminComment,
        });
        setNotice("Жалоба отклонена.");
      }
    } catch (error) {
      setNotice(error.message, "error");
    }
  }

  function bindEvents() {
    $("#login-form").addEventListener("submit", handleLogin);
    $("#logout-button").addEventListener("click", handleLogout);
    $("#refresh-button").addEventListener("click", loadAll);
    $("#post-form").addEventListener("submit", handleCreatePost);
    const changePasswordForm = $("#change-password-form");
    if (changePasswordForm) changePasswordForm.addEventListener("submit", handleChangePassword);
    const channelForm = $("#channel-form");
    if (channelForm) channelForm.addEventListener("submit", handleChannelSubmit);
    const syncButton = $("#sync-button");
    if (syncButton) syncButton.addEventListener("click", handleSync);
    const adminUserForm = $("#admin-user-form");
    if (adminUserForm) adminUserForm.addEventListener("submit", handleAdminUserSubmit);
    $("#channel-select").addEventListener("change", async (event) => {
      state.selectedChannelId = event.target.value;
      await loadAll();
    });
    $("#comments-status").addEventListener("change", loadComments);
    $("#comments-post").addEventListener("change", loadComments);
    $("#reports-status").addEventListener("change", loadReports);
    const channelRequestsStatus = $("#channel-requests-status");
    if (channelRequestsStatus) channelRequestsStatus.addEventListener("change", loadChannelRequests);
    document.querySelector(".tabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-tab]");
      if (button) {
        setActiveTab(button.dataset.tab);
      }
    });
    views.admin.addEventListener("click", handleAdminClick);
  }

  async function bootstrap() {
    if (webApp) {
      state.insideMax = true;
      state.initData = webApp.initData || "";
      if (webApp.ready) webApp.ready();
      if (webApp.expand) webApp.expand();
    }
    bindEvents();
    setActiveTab("dashboard");
    await loadAll();
  }

  bootstrap();
})();

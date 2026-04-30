(function () {
  const state = {
    dashboard: null,
    posts: [],
    comments: [],
    reports: [],
    channels: [],
    selectedChannelId: "",
    activeTab: "dashboard",
    initData: "",
    insideMax: false,
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

  function statusPill(status) {
    const tone = status === "active" || status === "published" ? "success" : status === "new" ? "danger" : "";
    return `<span class="pill ${tone}">${escapeHtml(statusLabels[status] || status || "-")}</span>`;
  }

  function renderChannelSelect() {
    const select = $("#channel-select");
    select.innerHTML = "";
    for (const channel of state.channels) {
      const option = document.createElement("option");
      option.value = channel.channel_chat_id;
      option.textContent = channel.channel_label || String(channel.channel_chat_id);
      select.append(option);
    }
    if (state.selectedChannelId) {
      select.value = state.selectedChannelId;
    }
    select.disabled = state.channels.length <= 1;
  }

  function renderDashboard(payload) {
    state.dashboard = payload;
    state.channels = Array.isArray(payload.channels) ? payload.channels : [];
    const stats = payload.stats || {};
    $("#admin-role").textContent = payload.admin?.role === "super_admin"
      ? "Роль: super-admin"
      : "Роль: администратор канала";
    $("#stat-users").textContent = String(stats.usersCount || 0);
    $("#stat-posts").textContent = String(stats.postsCount || 0);
    $("#stat-comments").textContent = String(stats.commentsCount || 0);
    $("#stat-reports").textContent = String(stats.reportsCount || 0);
    $("#stat-new-reports").textContent = String(stats.newReportsCount || 0);
    $("#stat-deleted-comments").textContent = String(stats.deletedCommentsCount || 0);
    renderChannelSelect();
    renderMiniList("#latest-posts", payload.latestPosts || [], renderPostItem);
    renderMiniList("#latest-comments", payload.latestComments || [], renderCommentItem);
    renderMiniList("#latest-reports", payload.latestReports || [], renderReportItem);
  }

  function renderMiniList(selector, items, renderer) {
    const list = $(selector);
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
    const payload = await requestJson(`/api/admin/dashboard${channelQuery()}`);
    const channels = Array.isArray(payload.channels) ? payload.channels : [];
    const nextSelected = channels.some((channel) => String(channel.channel_chat_id) === String(state.selectedChannelId))
      ? state.selectedChannelId
      : String(channels[0]?.channel_chat_id || "");
    if (nextSelected && nextSelected !== state.selectedChannelId) {
      state.selectedChannelId = nextSelected;
      return loadDashboard();
    }
    renderDashboard(payload);
    showAdmin();
  }

  async function loadPosts() {
    const payload = await requestJson(`/api/admin/posts${channelQuery()}`);
    state.posts = Array.isArray(payload.posts) ? payload.posts : [];
    renderPosts();
    renderPostFilter();
  }

  async function loadComments() {
    const params = new URLSearchParams();
    if (state.selectedChannelId) params.set("channel_id", state.selectedChannelId);
    if ($("#comments-status").value) params.set("status", $("#comments-status").value);
    if ($("#comments-post").value) params.set("post_id", $("#comments-post").value);
    const payload = await requestJson(`/api/admin/comments?${params.toString()}`);
    state.comments = Array.isArray(payload.comments) ? payload.comments : [];
    renderComments();
  }

  async function loadReports() {
    const params = new URLSearchParams();
    if (state.selectedChannelId) params.set("channel_id", state.selectedChannelId);
    if ($("#reports-status").value) params.set("status", $("#reports-status").value);
    const payload = await requestJson(`/api/admin/reports?${params.toString()}`);
    state.reports = Array.isArray(payload.reports) ? payload.reports : [];
    renderReports();
  }

  async function loadAll() {
    try {
      await loadDashboard();
      await loadPosts();
      await loadComments();
      await loadReports();
      setNotice("");
    } catch (error) {
      if (error.status === 401) {
        if (state.insideMax) {
          showDenied();
        } else {
          showLogin("");
        }
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
      await requestJson("/admin/login", {
        method: "POST",
        body: JSON.stringify({ token: $("#login-token").value }),
      });
      $("#login-token").value = "";
      await loadAll();
    } catch (error) {
      showLogin(error.message);
    }
  }

  async function handleLogout() {
    await requestJson("/admin/logout", { method: "POST", body: "{}" }).catch(() => null);
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
      const result = await requestJson("/api/admin/posts", {
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

  async function setCommentStatus(commentId, status, reason) {
    await requestJson(`/api/admin/comments/${encodeURIComponent(commentId)}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status, reason }),
    });
    await loadAll();
  }

  async function updateReport(reportId, payload) {
    await requestJson(`/api/admin/reports/${encodeURIComponent(reportId)}`, {
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
    try {
      if (deletePost) {
        if (!window.confirm("Удалить пост из админки? Комментарии к нему перестанут открываться.")) return;
        await requestJson(`/api/admin/posts/${encodeURIComponent(deletePost.dataset.deletePost)}`, { method: "DELETE" });
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
    $("#channel-select").addEventListener("change", async (event) => {
      state.selectedChannelId = event.target.value;
      await loadAll();
    });
    $("#comments-status").addEventListener("change", loadComments);
    $("#comments-post").addEventListener("change", loadComments);
    $("#reports-status").addEventListener("change", loadReports);
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

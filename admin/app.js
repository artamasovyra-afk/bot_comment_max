const state = {
  data: null,
  filters: {
    postsChannelId: "",
    postsSearch: "",
  },
};

const $ = (selector) => document.querySelector(selector);

const views = {
  login: $("#login-view"),
  admin: $("#admin-view"),
};

function showLogin(message = "") {
  views.login.classList.remove("hidden");
  views.admin.classList.add("hidden");
  $("#login-error").textContent = message;
}

function showAdmin() {
  views.login.classList.add("hidden");
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

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || "Ошибка запроса");
    error.status = response.status;
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

function safeHttpUrl(value) {
  const url = String(value || "").trim();
  if (!/^https?:\/\//i.test(url)) return "";
  return url;
}

function optionLabel(channel) {
  const channelName = channel.channel_label || String(channel.channel_chat_id);
  const commentsName = channel.comments_chat_label || String(channel.comments_chat_id);
  return `${channelName} → ${commentsName}`;
}

function channelLabelById(channelId) {
  const channels = state.data?.channels || [];
  const channel = channels.find((item) => String(item.channel_chat_id) === String(channelId));
  return channel ? channel.channel_label || String(channel.channel_chat_id) : String(channelId);
}

function normalizeSearch(value) {
  return String(value || "").trim().toLocaleLowerCase("ru-RU");
}

function fillStatus(data) {
  $("#status-version").textContent = data.app.version || "-";
  $("#status-mode").textContent = data.app.delivery_mode || "-";
  $("#status-bot").textContent = data.app.bot_username ? `@${data.app.bot_username}` : "-";
  $("#status-channels").textContent = String(data.channels.length);
}

function renderPublishChannels(channels) {
  const select = $("#publish-channel");
  select.innerHTML = "";
  if (!channels.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Нет подключённых каналов";
    select.append(option);
    return;
  }
  for (const channel of channels) {
    const option = document.createElement("option");
    option.value = channel.channel_chat_id;
    option.textContent = optionLabel(channel);
    select.append(option);
  }
}

function renderPostFilters(channels) {
  const select = $("#posts-channel-filter");
  const search = $("#posts-search");
  const selectedChannelId = state.filters.postsChannelId;
  select.innerHTML = '<option value="">Все каналы</option>';

  for (const channel of channels) {
    const option = document.createElement("option");
    option.value = channel.channel_chat_id;
    option.textContent = channel.channel_label || String(channel.channel_chat_id);
    select.append(option);
  }

  if (selectedChannelId && Array.from(select.options).some((option) => option.value === selectedChannelId)) {
    select.value = selectedChannelId;
  } else {
    select.value = "";
    state.filters.postsChannelId = "";
  }
  search.value = state.filters.postsSearch;
}

function filterPosts(posts) {
  const channelId = state.filters.postsChannelId;
  const query = normalizeSearch(state.filters.postsSearch);
  return posts.filter((post) => {
    if (channelId && String(post.channel_chat_id) !== channelId) {
      return false;
    }
    if (!query) {
      return true;
    }
    const searchableText = normalizeSearch(`${post.post_text || ""} ${post.comment_search_text || ""}`);
    return searchableText.includes(query);
  });
}

function renderChannels(channels) {
  const list = $("#channels-list");
  list.innerHTML = "";
  if (!channels.length) {
    list.innerHTML = '<div class="empty">Каналы ещё не подключены.</div>';
    return;
  }
  for (const channel of channels) {
    const item = document.createElement("article");
    item.className = "item";
    const channelUrl = safeHttpUrl(channel.channel_url);
    const commentsChatUrl = safeHttpUrl(channel.comments_chat_url);
    item.innerHTML = `
      <div class="item-row">
        <div>
          <div class="item-title">${escapeHtml(channel.channel_label || channel.channel_chat_id)}</div>
          <div class="item-meta">Канал: ${escapeHtml(channel.channel_label || channel.channel_chat_id)}</div>
          <div class="item-meta">Чат комментариев: ${escapeHtml(channel.comments_chat_label || channel.comments_chat_id)}</div>
          <div class="item-meta">ID: ${channel.channel_chat_id} → ${channel.comments_chat_id}</div>
        </div>
        <span class="pill">${channel.same_chat ? "один чат" : "отдельный чат"}</span>
      </div>
      <div class="item-row item-footer">
        <span class="item-meta">Обновлено: ${formatDate(channel.updated_at)}</span>
        <div class="item-actions">
          ${
            channelUrl
              ? `<a class="action-link" href="${escapeHtml(channelUrl)}" target="_blank" rel="noreferrer">Открыть канал</a>`
              : '<span class="action-link disabled">Канал недоступен</span>'
          }
          ${
            commentsChatUrl
              ? `<a class="action-link" href="${escapeHtml(commentsChatUrl)}" target="_blank" rel="noreferrer">Открыть чат</a>`
              : '<span class="action-link disabled">Чат недоступен</span>'
          }
          <button class="danger-button" type="button" data-remove-channel="${channel.channel_chat_id}">Удалить</button>
        </div>
      </div>
    `;
    list.append(item);
  }
}

function renderPosts(posts) {
  const list = $("#posts-list");
  list.innerHTML = "";
  if (!posts.length) {
    list.innerHTML = '<div class="empty">Пока нет зарегистрированных постов.</div>';
    return;
  }
  const filteredPosts = filterPosts(posts);
  if (!filteredPosts.length) {
    list.innerHTML = '<div class="empty">По выбранному фильтру посты не найдены.</div>';
    return;
  }
  for (const post of filteredPosts) {
    const item = document.createElement("article");
    item.className = "item";
    const postUrl = safeHttpUrl(post.post_url);
    item.innerHTML = `
      <div class="item-row">
        <div>
          <div class="item-title">${escapeHtml(post.post_text || "Пост без текста")}</div>
          <div class="item-meta">Канал: ${escapeHtml(channelLabelById(post.channel_chat_id))}</div>
          <div class="item-meta">Message ID: ${escapeHtml(post.post_message_id)}</div>
        </div>
        <span class="pill">${post.comment_count} комм.</span>
      </div>
      <div class="item-row">
        <span class="item-meta">${formatDate(post.created_at)}</span>
        ${postUrl ? `<a class="item-meta" href="${escapeHtml(postUrl)}" target="_blank" rel="noreferrer">Открыть</a>` : ""}
      </div>
    `;
    list.append(item);
  }
}

function renderPending(bindings) {
  const list = $("#pending-list");
  list.innerHTML = "";
  if (!bindings.length) {
    list.innerHTML = '<div class="empty">Каналов в процессе настройки нет.</div>';
    return;
  }
  for (const binding of bindings) {
    const item = document.createElement("article");
    item.className = "item";
    item.innerHTML = `
      <div class="item-row">
        <div>
          <div class="item-title">${escapeHtml(binding.channel_label || "Канал")}</div>
          <div class="item-meta">ID канала: ${binding.channel_chat_id}</div>
          <div class="item-meta">Команда: ${escapeHtml(binding.setup_command || `/bind_comments ${binding.bind_code}`)}</div>
        </div>
        <span class="pill">ожидает чат</span>
      </div>
      <div class="item-row">
        <span class="item-meta">Админ: ${binding.requested_by_user_id}</span>
        <span class="item-meta">Осталось: ${escapeHtml(binding.remaining_display)}</span>
      </div>
    `;
    list.append(item);
  }
}

function render(data) {
  state.data = data;
  fillStatus(data);
  renderPublishChannels(data.channels);
  renderChannels(data.channels);
  renderPostFilters(data.channels);
  renderPosts(data.posts);
  renderPending(data.pending_bindings);
}

async function loadState() {
  try {
    const data = await requestJson("/api/admin/state");
    render(data);
    showAdmin();
    setNotice("");
  } catch (error) {
    if (error.status === 401 || error.status === 503) {
      showLogin(error.status === 503 ? "Админка пока не настроена." : "");
      return;
    }
    showLogin(error.message);
  }
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
    await loadState();
  } catch (error) {
    showLogin(error.message);
  }
}

async function handleLogout() {
  await requestJson("/admin/logout", { method: "POST", body: "{}" }).catch(() => null);
  showLogin("");
}

async function handleChannelSubmit(event) {
  event.preventDefault();
  setNotice("");
  try {
    const result = await requestJson("/api/admin/channels", {
      method: "POST",
      body: JSON.stringify({
        channel_chat_id: $("#channel-id").value.trim(),
        comments_chat_id: $("#comments-chat-id").value.trim(),
        comments_chat_url: $("#comments-chat-url").value.trim(),
        sync_now: true,
      }),
    });
    event.currentTarget.reset();
    await loadState();
    setNotice(`Канал подключён. Подцеплено постов: ${result.attached_count}`);
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function handlePublish(event) {
  event.preventDefault();
  setNotice("");
  try {
    const result = await requestJson("/api/admin/publish", {
      method: "POST",
      body: JSON.stringify({
        channel_chat_id: $("#publish-channel").value,
        text: $("#publish-text").value.trim(),
      }),
    });
    $("#publish-text").value = "";
    await loadState();
    setNotice(`Пост опубликован: ${result.post_message_id}`);
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function handleSync() {
  setNotice("");
  try {
    const result = await requestJson("/api/admin/sync", { method: "POST", body: "{}" });
    await loadState();
    setNotice(`Синхронизация завершена. Подцеплено постов: ${result.attached_count}`);
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function handleListClick(event) {
  const button = event.target.closest("[data-remove-channel]");
  if (!button) return;
  const channelId = button.dataset.removeChannel;
  if (!window.confirm(`Отключить канал ${channelId}?`)) return;
  setNotice("");
  try {
    await requestJson(`/api/admin/channels/${encodeURIComponent(channelId)}`, {
      method: "DELETE",
    });
    await loadState();
    setNotice("Канал отключён.");
  } catch (error) {
    setNotice(error.message, "error");
  }
}

function handlePostsFilterChange() {
  state.filters.postsChannelId = $("#posts-channel-filter").value;
  state.filters.postsSearch = $("#posts-search").value;
  if (state.data) {
    renderPosts(state.data.posts);
  }
}

$("#login-form").addEventListener("submit", handleLogin);
$("#logout-button").addEventListener("click", handleLogout);
$("#refresh-button").addEventListener("click", loadState);
$("#sync-button").addEventListener("click", handleSync);
$("#channel-form").addEventListener("submit", handleChannelSubmit);
$("#publish-form").addEventListener("submit", handlePublish);
$("#channels-list").addEventListener("click", handleListClick);
$("#posts-channel-filter").addEventListener("change", handlePostsFilterChange);
$("#posts-search").addEventListener("input", handlePostsFilterChange);

loadState();

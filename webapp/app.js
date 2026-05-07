(function () {
  const MAX_COMMENT_LENGTH = 4000;
  const COMMENTS_PAGE_LIMIT = 40;
  const IMAGE_PREVIEW_MAX_DIMENSION = 1600;
  const IMAGE_PREVIEW_TARGET_BYTES = 2 * 1024 * 1024;
  const AUTO_REFRESH_INTERVAL_MS = 4000;
  const AUTO_REFRESH_NEAR_BOTTOM_PX = 88;
  const BOTTOM_SCROLL_RETRY_DELAYS_MS = [72, 180, 360];
  const LONG_PRESS_DELAY_MS = 500;
  const LONG_PRESS_MOVE_THRESHOLD_PX = 10;
  const TOUCH_CLICK_SUPPRESS_MS = 750;
  const LINKS_BLOCKED_MESSAGE = "Ссылки запрещены правилами сервиса.";
  const COMMENT_BLOCKED_CODE = "COMMENT_BLOCKED";
  const COMMENT_BLOCKED_TITLE = "Комментарий не опубликован";
  const COMMENT_BLOCKED_TEXT =
    "В комментарии есть запрещённые выражения. Пожалуйста, измените формулировку и отправьте комментарий снова.";
  const COMMENT_LINK_RE =
    /(^|[^@\w])((?:https?:\/\/|ftp:\/\/|www\.)\S+|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?::\d{2,5})?(?:\/[^\s]*)?)/i;

  const state = {
    postRef: null,
    post: null,
    comments: [],
    initData: "",
    insideMax: false,
    submitting: false,
    currentUserId: null,
    isAdmin: false,
    hasMore: false,
    oldestCommentId: null,
    loadingOlder: false,
    replyToCommentId: null,
    deletingCommentId: null,
    pendingPhoto: null,
    editingCommentId: null,
    loadedOlderHistory: false,
    autoRefreshTimer: null,
    autoRefreshInFlight: false,
    stickToBottom: true,
    bottomScrollFrameId: 0,
    bottomScrollTimeoutIds: [],
    initialTargetScrollDone: false,
    targetHighlightTimeoutId: 0,
    readState: null,
    targetCommentId: null,
    hasUnread: false,
    markReadInFlight: false,
    contextMenuCommentId: null,
    menuOpenedAt: 0,
    reportingCommentId: null,
    submittingReport: false,
    imageViewerItems: [],
    imageViewerIndex: 0,
    imagePress: null,
    commentPress: null,
    lastImageTouchOpenAt: 0,
    lastImageMenuOpenAt: 0,
  };

  const elements = {
    root: document.getElementById("app-root"),
    form: document.getElementById("comment-form"),
    input: document.getElementById("comment-input"),
    photoButton: document.getElementById("photo-button"),
    photoInput: document.getElementById("photo-input"),
    replyState: document.getElementById("reply-state"),
    replyCaption: document.getElementById("reply-state-caption"),
    replyCancel: document.getElementById("reply-cancel"),
    editorState: document.getElementById("editor-state"),
    editorCaption: document.getElementById("editor-state-caption"),
    editorCancel: document.getElementById("editor-cancel"),
    preview: document.getElementById("composer-preview"),
    previewImage: document.getElementById("composer-preview-image"),
    previewCaption: document.getElementById("composer-preview-caption"),
    previewRemove: document.getElementById("composer-preview-remove"),
    submit: document.getElementById("submit-button"),
    loadOlder: document.getElementById("load-older-button"),
    banner: document.getElementById("state-banner"),
    list: document.getElementById("comments-list"),
    postTemplate: document.getElementById("post-template"),
    template: document.getElementById("comment-template"),
    commentMenu: document.getElementById("comment-menu"),
    commentMenuBackdrop: document.getElementById("comment-menu-backdrop"),
    commentMenuReply: document.getElementById("comment-menu-reply"),
    commentMenuEdit: document.getElementById("comment-menu-edit"),
    commentMenuDelete: document.getElementById("comment-menu-delete"),
    commentMenuReport: document.getElementById("comment-menu-report"),
    blockedModal: document.getElementById("comment-blocked-modal"),
    blockedTitle: document.getElementById("comment-blocked-title"),
    blockedText: document.getElementById("comment-blocked-text"),
    blockedFix: document.getElementById("comment-blocked-fix"),
    reportModal: document.getElementById("report-modal"),
    reportForm: document.getElementById("report-form"),
    reportReason: document.getElementById("report-reason"),
    reportDetails: document.getElementById("report-details"),
    reportCancel: document.getElementById("report-cancel"),
    reportSubmit: document.getElementById("report-submit"),
    imageViewer: document.getElementById("image-viewer"),
    imageViewerImage: document.getElementById("image-viewer-image"),
    imageViewerClose: document.getElementById("image-viewer-close"),
    imageViewerPrev: document.getElementById("image-viewer-prev"),
    imageViewerNext: document.getElementById("image-viewer-next"),
    imageViewerCounter: document.getElementById("image-viewer-counter"),
  };

  const webApp = window.WebApp || null;

  function setBanner(text, tone) {
    if (!text) {
      elements.banner.hidden = true;
      elements.banner.textContent = "";
      delete elements.banner.dataset.tone;
      return;
    }

    elements.banner.hidden = false;
    elements.banner.textContent = text;
    if (tone) {
      elements.banner.dataset.tone = tone;
    } else {
      delete elements.banner.dataset.tone;
    }
  }

  function clearBanner() {
    setBanner("", "");
  }

  function clearComposerError() {
    elements.input.classList.remove("composer__input--error");
    elements.input.removeAttribute("aria-invalid");
  }

  function markComposerError() {
    elements.input.classList.add("composer__input--error");
    elements.input.setAttribute("aria-invalid", "true");
  }

  function focusComposerInput() {
    elements.input.focus();
    try {
      const end = elements.input.value.length;
      elements.input.setSelectionRange(end, end);
    } catch (_err) {
      // Some embedded WebView builds do not support selection APIs on textarea.
    }
  }

  function closeCommentBlockedDialog() {
    elements.blockedModal.hidden = true;
    focusComposerInput();
  }

  function showCommentBlockedDialog() {
    clearBanner();
    markComposerError();
    elements.blockedTitle.textContent = COMMENT_BLOCKED_TITLE;
    elements.blockedText.textContent = COMMENT_BLOCKED_TEXT;
    elements.blockedModal.hidden = false;
    elements.blockedFix.focus();
  }

  function haptic(kind) {
    if (!webApp || !webApp.HapticFeedback) {
      return;
    }
    try {
      if (kind === "success" || kind === "error" || kind === "warning") {
        webApp.HapticFeedback.notificationOccurred(kind);
        return;
      }
      webApp.HapticFeedback.impactOccurred(kind || "light");
    } catch (_err) {
      // Ignore unsupported bridge methods.
    }
  }

  function buildApiHeaders(extraHeaders) {
    const headers = Object.assign({}, extraHeaders || {});
    if (state.initData) {
      headers["X-Max-Init-Data"] = state.initData;
    }
    return headers;
  }

  function getPostRef() {
    if (webApp && webApp.initDataUnsafe && webApp.initDataUnsafe.start_param) {
      return String(webApp.initDataUnsafe.start_param);
    }
    return new URLSearchParams(window.location.search).get("post");
  }

  function formatCounter(count) {
    const value = Number(count) || 0;
    const mod10 = value % 10;
    const mod100 = value % 100;
    if (mod10 === 1 && mod100 !== 11) {
      return `${value} комментарий`;
    }
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
      return `${value} комментария`;
    }
    return `${value} комментариев`;
  }

  function formatCommentTime(value) {
    try {
      return new Date(value).toLocaleTimeString("ru-RU", {
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (_err) {
      return value;
    }
  }

  function formatDayDivider(value) {
    try {
      const date = new Date(value);
      return date.toLocaleDateString("ru-RU", {
        day: "numeric",
        month: "long",
      });
    } catch (_err) {
      return "";
    }
  }

  function getDateKey(value) {
    try {
      const date = new Date(value);
      return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
    } catch (_err) {
      return String(value);
    }
  }

  function buildPostHeadline(post) {
    const text = String(post.post_text || post.post_preview || "").trim();
    if (!text) {
      return "Пост канала";
    }
    const firstLine = text.split(/\n+/)[0].trim();
    if (firstLine.length <= 84) {
      return firstLine;
    }
    return `${firstLine.slice(0, 81)}...`;
  }

  function containsForbiddenLink(text) {
    return COMMENT_LINK_RE.test(String(text || ""));
  }

  function updateComposerCount() {
    if (!elements.input) {
      return;
    }
    const rows = Math.max(1, Math.min(4, String(elements.input.value || "").split("\n").length));
    elements.input.rows = rows;
  }

  function bytesFromDataUrl(dataUrl) {
    const markerIndex = String(dataUrl || "").indexOf(",");
    if (markerIndex < 0) {
      return 0;
    }
    const base64 = dataUrl.slice(markerIndex + 1);
    const padding = base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0;
    return Math.max(0, Math.floor((base64.length * 3) / 4) - padding);
  }

  function clampImageDimensions(width, height, maxDimension) {
    if (!width || !height) {
      return { width: 0, height: 0 };
    }
    const longestSide = Math.max(width, height);
    if (longestSide <= maxDimension) {
      return { width, height };
    }
    const scale = maxDimension / longestSide;
    return {
      width: Math.max(1, Math.round(width * scale)),
      height: Math.max(1, Math.round(height * scale)),
    };
  }

  function loadImageFromFile(file) {
    return new Promise(function (resolve, reject) {
      const objectUrl = URL.createObjectURL(file);
      const image = new Image();
      image.onload = function () {
        URL.revokeObjectURL(objectUrl);
        resolve(image);
      };
      image.onerror = function () {
        URL.revokeObjectURL(objectUrl);
        reject(new Error("Не удалось открыть изображение."));
      };
      image.src = objectUrl;
    });
  }

  async function compressImageFile(file) {
    const image = await loadImageFromFile(file);
    const dimensions = clampImageDimensions(
      image.naturalWidth || image.width,
      image.naturalHeight || image.height,
      IMAGE_PREVIEW_MAX_DIMENSION
    );
    const canvas = document.createElement("canvas");
    canvas.width = dimensions.width;
    canvas.height = dimensions.height;
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) {
      throw new Error("Не удалось подготовить изображение.");
    }
    context.drawImage(image, 0, 0, canvas.width, canvas.height);

    const qualities = [0.86, 0.78, 0.7, 0.62, 0.54];
    let dataUrl = canvas.toDataURL("image/jpeg", qualities[0]);
    for (let index = 1; index < qualities.length; index += 1) {
      if (bytesFromDataUrl(dataUrl) <= IMAGE_PREVIEW_TARGET_BYTES) {
        break;
      }
      dataUrl = canvas.toDataURL("image/jpeg", qualities[index]);
    }

    return {
      kind: "image",
      fileName: String(file && file.name ? file.name : "comment-photo.jpg").replace(/\.[^.]+$/, ".jpg"),
      mimeType: "image/jpeg",
      width: canvas.width,
      height: canvas.height,
      sizeBytes: bytesFromDataUrl(dataUrl),
      dataUrl,
      previewUrl: dataUrl,
    };
  }

  function clearPhotoSelection() {
    state.pendingPhoto = null;
    if (elements.photoInput) {
      elements.photoInput.value = "";
    }
    if (elements.preview) {
      elements.preview.hidden = true;
    }
    if (elements.previewImage) {
      elements.previewImage.removeAttribute("src");
    }
    if (elements.previewCaption) {
      elements.previewCaption.textContent = "";
    }
  }

  function renderPhotoSelection() {
    const photo = state.pendingPhoto;
    if (!elements.preview) {
      return;
    }
    if (!photo) {
      clearPhotoSelection();
      return;
    }
    elements.preview.hidden = false;
    elements.previewImage.src = photo.previewUrl;
    elements.previewCaption.textContent = `${photo.width}×${photo.height} · ${Math.max(
      1,
      Math.round(photo.sizeBytes / 1024)
    )} КБ`;
  }

  function renderPost(post) {
    state.post = post;
    document.title = `${buildPostHeadline(post)} | Комментарии`;
  }

  function renderPostMedia(container, mediaItems) {
    const items = Array.isArray(mediaItems) ? mediaItems : [];
    container.innerHTML = "";
    if (!items.length) {
      container.hidden = true;
      return;
    }

    items.forEach(function (mediaItem) {
      if (!mediaItem || mediaItem.kind !== "image" || !mediaItem.url) {
        return;
      }
      const image = document.createElement("img");
      image.className = "post-card__image";
      image.alt = "Изображение поста";
      image.loading = "lazy";
      if (mediaItem.width) {
        image.width = Number(mediaItem.width);
      }
      if (mediaItem.height) {
        image.height = Number(mediaItem.height);
      }
      image.addEventListener("load", function () {
        if (state.stickToBottom) {
          scrollCommentsToBottom();
        }
      });
      image.src = mediaItem.preview_url || mediaItem.url;
      container.appendChild(image);
    });

    container.hidden = container.childElementCount === 0;
  }

  function renderPostCard(post) {
    if (!post || !elements.postTemplate) {
      return;
    }
    const node = elements.postTemplate.content.firstElementChild.cloneNode(true);
    const textNode = node.querySelector(".post-card__text");
    const mediaNode = node.querySelector(".post-card__media");
    const postText = String(post.post_text || "").trim();

    textNode.textContent = postText;
    textNode.hidden = !postText;
    renderPostMedia(mediaNode, post.media);
    elements.list.appendChild(node);
  }

  function avatarInitials(displayName) {
    const parts = String(displayName || "")
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    if (!parts.length) {
      return "?";
    }
    return (parts[0][0] || "?").toUpperCase();
  }

  function avatarHue(displayName) {
    const value = String(displayName || "");
    let hash = 0;
    for (let index = 0; index < value.length; index += 1) {
      hash = (hash * 31 + value.charCodeAt(index)) % 360;
    }
    return 180 + (hash % 120);
  }

  function commentAuthorData(comment) {
    const author = comment && typeof comment.author === "object" ? comment.author : null;
    const userId = Number(
      (author && (author.id || author.userId || author.user_id || author.maxUserId || author.max_user_id)) ||
      (comment && comment.user_id)
    ) || null;
    if (!userId) {
      return null;
    }
    const displayName = String(
      (author && (author.displayName || author.display_name)) ||
      (comment && comment.display_name) ||
      "Пользователь"
    ).trim() || "Пользователь";
    const username = String(
      (author && (author.username || author.user_name)) ||
      (comment && comment.username) ||
      ""
    ).trim();
    const avatarUrl = String(
      (author && (author.avatarUrl || author.avatar_url)) || ""
    ).trim();
    const profileUrl = String(
      (author && (author.profileUrl || author.profile_url)) || ""
    ).trim();
    return {
      userId,
      displayName,
      username: username || null,
      avatarUrl: avatarUrl || null,
      profileUrl: profileUrl || null,
    };
  }

  function openExternalUrl(url) {
    const targetUrl = String(url || "").trim();
    if (!targetUrl) {
      return;
    }
    if (webApp && typeof webApp.openLink === "function") {
      webApp.openLink(targetUrl);
      return;
    }
    window.open(targetUrl, "_blank", "noopener,noreferrer");
  }

  function renderAvatarBadge(element, user) {
    if (!element) {
      return;
    }
    const displayName = user && user.displayName ? user.displayName : "Пользователь";
    const avatarUrl = String((user && user.avatarUrl) || "").trim();
    element.style.setProperty("--avatar-hue", String(avatarHue(displayName)));
    if (avatarUrl) {
      element.textContent = "";
      element.classList.add("message__avatar--image");
      element.style.backgroundImage = `url("${avatarUrl.replace(/"/g, "%22")}")`;
      return;
    }
    element.textContent = avatarInitials(displayName);
    element.classList.remove("message__avatar--image");
    element.style.removeProperty("background-image");
  }

  function resolveMaxProfileUrl(user) {
    // MAX Bridge документирует только openMaxLink("https://max.ru/<some-url>").
    // Пока MAX не опишет поддержанный профильный URL для мини-приложений,
    // не строим ссылки на профиль из user_id или username.
    const profileUrl = String((user && user.profileUrl) || "").trim();
    if (!profileUrl) {
      return null;
    }
    if (!/^https:\/\/max\.ru\/.+/i.test(profileUrl)) {
      return null;
    }
    return profileUrl;
  }

  function openMaxProfileUrl(profileUrl) {
    const targetUrl = String(profileUrl || "").trim();
    if (!targetUrl) {
      return;
    }
    if (webApp && typeof webApp.openMaxLink === "function" && /^https:\/\/max\.ru\//i.test(targetUrl)) {
      webApp.openMaxLink(targetUrl);
      return;
    }
    openExternalUrl(targetUrl);
  }

  function mediaImageUrl(mediaItem) {
    if (!mediaItem || mediaItem.kind !== "image") {
      return "";
    }
    return String(
      mediaItem.url ||
      mediaItem.fileUrl ||
      mediaItem.file_url ||
      mediaItem.originalUrl ||
      mediaItem.original_url ||
      ""
    );
  }

  function mediaThumbUrl(mediaItem) {
    if (!mediaItem || mediaItem.kind !== "image") {
      return "";
    }
    return String(
      mediaItem.thumbnailUrl ||
      mediaItem.thumbnail_url ||
      mediaItem.thumbUrl ||
      mediaItem.thumb_url ||
      mediaImageUrl(mediaItem)
    );
  }

  function commentImageItems(mediaItems) {
    return (Array.isArray(mediaItems) ? mediaItems : [])
      .filter(function (mediaItem) {
        return mediaItem && mediaItem.kind === "image" && mediaImageUrl(mediaItem);
      })
      .map(function (mediaItem) {
        return {
          url: mediaImageUrl(mediaItem),
          thumbnailUrl: mediaThumbUrl(mediaItem),
        };
      });
  }

  function bindAuthorProfileHandlers(node, comment) {
    const user = commentAuthorData(comment);
    const profileUrl = resolveMaxProfileUrl(user);
    if (!user || !profileUrl) {
      return;
    }
    const clickableNodes = [
      node.querySelector(".message__avatar"),
      node.querySelector(".message__author"),
    ];
    clickableNodes.forEach(function (target) {
      if (!target) {
        return;
      }
      target.classList.add("message__author-target");
      target.tabIndex = 0;
      target.setAttribute("role", "link");
      target.setAttribute("aria-label", "Открыть профиль пользователя MAX");
      target.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        openMaxProfileUrl(profileUrl);
      });
      target.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        openMaxProfileUrl(profileUrl);
      });
    });
  }

  function isLongPressMovementExceeded(pressState, clientX, clientY) {
    if (!pressState) {
      return false;
    }
    return (
      Math.abs(Number(clientX) - Number(pressState.startX)) > LONG_PRESS_MOVE_THRESHOLD_PX ||
      Math.abs(Number(clientY) - Number(pressState.startY)) > LONG_PRESS_MOVE_THRESHOLD_PX
    );
  }

  function releaseImagePressState() {
    const pressState = state.imagePress;
    if (!pressState) {
      return;
    }
    if (pressState.timerId) {
      window.clearTimeout(pressState.timerId);
    }
    if (pressState.button) {
      pressState.button.classList.remove("message__image-button--pressing");
    }
    state.imagePress = null;
  }

  function releaseCommentPressState() {
    const pressState = state.commentPress;
    if (!pressState) {
      return;
    }
    if (pressState.timerId) {
      window.clearTimeout(pressState.timerId);
    }
    if (pressState.bubble) {
      pressState.bubble.classList.remove("message__bubble--pressing");
    }
    state.commentPress = null;
  }

  function openCommentMenuFromAnchor(commentId, clientX, clientY, anchorNode) {
    const rect = anchorNode ? anchorNode.getBoundingClientRect() : null;
    const fallbackX = rect ? Math.min(rect.left + rect.width - 16, window.innerWidth - 28) : window.innerWidth - 28;
    const fallbackY = rect ? Math.min(rect.top + rect.height * 0.5, window.innerHeight - 28) : window.innerHeight - 28;
    openCommentMenu(
      commentId,
      Number.isFinite(clientX) ? clientX : fallbackX,
      Number.isFinite(clientY) ? clientY : fallbackY
    );
    haptic("light");
  }

  function bindImageInteraction(button, commentId, items, index) {
    button.addEventListener("pointerdown", function (event) {
      if (state.submitting || state.deletingCommentId !== null) {
        return;
      }
      if (event.pointerType !== "touch") {
        return;
      }
      releaseImagePressState();
      button.classList.add("message__image-button--pressing");
      state.imagePress = {
        button,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        clientX: event.clientX,
        clientY: event.clientY,
        longPressTriggered: false,
        timerId: window.setTimeout(function () {
          const pressState = state.imagePress;
          if (!pressState || pressState.pointerId !== event.pointerId) {
            return;
          }
          pressState.longPressTriggered = true;
          openCommentMenuFromAnchor(commentId, pressState.clientX, pressState.clientY, button);
          state.lastImageMenuOpenAt = Date.now();
          button.classList.remove("message__image-button--pressing");
        }, LONG_PRESS_DELAY_MS),
      };
    });

    button.addEventListener("pointermove", function (event) {
      const pressState = state.imagePress;
      if (!pressState || pressState.pointerId !== event.pointerId) {
        return;
      }
      pressState.clientX = event.clientX;
      pressState.clientY = event.clientY;
      if (isLongPressMovementExceeded(pressState, event.clientX, event.clientY)) {
        releaseImagePressState();
      }
    });

    button.addEventListener("pointerup", function (event) {
      if (event.pointerType === "mouse") {
        if (event.button === 0) {
          event.stopPropagation();
          openImageViewer(items, index);
        }
        return;
      }
      const pressState = state.imagePress;
      if (!pressState || pressState.pointerId !== event.pointerId) {
        return;
      }
      const longPressTriggered = pressState.longPressTriggered;
      releaseImagePressState();
      event.stopPropagation();
      if (longPressTriggered) {
        event.preventDefault();
        return;
      }
      state.lastImageTouchOpenAt = Date.now();
      openImageViewer(items, index);
    });

    button.addEventListener("pointercancel", function (event) {
      const pressState = state.imagePress;
      if (!pressState || pressState.pointerId !== event.pointerId) {
        return;
      }
      releaseImagePressState();
    });

    button.addEventListener("click", function (event) {
      event.stopPropagation();
      if (window.PointerEvent && event.detail !== 0) {
        if (
          Date.now() - state.lastImageTouchOpenAt < TOUCH_CLICK_SUPPRESS_MS ||
          Date.now() - state.lastImageMenuOpenAt < TOUCH_CLICK_SUPPRESS_MS
        ) {
          event.preventDefault();
        }
        return;
      }
      event.preventDefault();
      openImageViewer(items, index);
    });

    button.addEventListener("contextmenu", function (event) {
      event.preventDefault();
      event.stopPropagation();
      releaseImagePressState();
      if (Date.now() - state.lastImageMenuOpenAt < TOUCH_CLICK_SUPPRESS_MS) {
        return;
      }
      openCommentMenuFromAnchor(commentId, event.clientX, event.clientY, button);
      state.lastImageMenuOpenAt = Date.now();
    });
  }

  function updateImageViewer() {
    const total = state.imageViewerItems.length;
    const item = total ? state.imageViewerItems[state.imageViewerIndex] : null;
    if (!item) {
      closeImageViewer();
      return;
    }
    elements.imageViewerImage.src = item.url;
    elements.imageViewerCounter.textContent = total > 1
      ? `${state.imageViewerIndex + 1} из ${total}`
      : "";
    elements.imageViewerCounter.hidden = total <= 1;
    elements.imageViewerPrev.hidden = total <= 1;
    elements.imageViewerNext.hidden = total <= 1;
  }

  function openImageViewer(items, index) {
    const images = Array.isArray(items) ? items.filter(function (item) {
      return item && item.url;
    }) : [];
    if (!images.length) {
      return;
    }
    state.imageViewerItems = images;
    state.imageViewerIndex = Math.max(0, Math.min(Number(index) || 0, images.length - 1));
    elements.imageViewer.hidden = false;
    document.body.classList.add("image-viewer-open");
    updateImageViewer();
    elements.imageViewerClose.focus();
    haptic("light");
  }

  function closeImageViewer() {
    if (elements.imageViewer.hidden) {
      return;
    }
    elements.imageViewer.hidden = true;
    elements.imageViewerImage.removeAttribute("src");
    state.imageViewerItems = [];
    state.imageViewerIndex = 0;
    document.body.classList.remove("image-viewer-open");
  }

  function showNextImage() {
    const total = state.imageViewerItems.length;
    if (total <= 1) {
      return;
    }
    state.imageViewerIndex = (state.imageViewerIndex + 1) % total;
    updateImageViewer();
  }

  function showPrevImage() {
    const total = state.imageViewerItems.length;
    if (total <= 1) {
      return;
    }
    state.imageViewerIndex = (state.imageViewerIndex - 1 + total) % total;
    updateImageViewer();
  }

  function renderCommentMedia(container, mediaItems, commentId) {
    const items = commentImageItems(mediaItems);
    container.innerHTML = "";
    if (!items.length) {
      container.hidden = true;
      return;
    }
    container.classList.toggle("message__media--single", items.length === 1);
    container.classList.toggle("message__media--grid", items.length > 1);

    items.forEach(function (mediaItem, index) {
      const button = document.createElement("button");
      button.className = "message__image-button";
      button.type = "button";
      button.setAttribute("aria-label", "Открыть изображение комментария");
      const image = document.createElement("img");
      image.className = "message__image";
      image.alt = "Изображение комментария";
      image.loading = "lazy";
      const fallback = document.createElement("span");
      fallback.className = "message__image-fallback";
      fallback.textContent = "Изображение недоступно";
      fallback.hidden = true;
      image.addEventListener("load", function () {
        if (state.stickToBottom) {
          scrollCommentsToBottom();
        }
      });
      image.addEventListener("error", function () {
        image.hidden = true;
        fallback.hidden = false;
      });
      bindImageInteraction(button, commentId, items, index);
      image.src = mediaItem.thumbnailUrl || mediaItem.url;
      button.appendChild(image);
      button.appendChild(fallback);
      container.appendChild(button);
    });

    container.hidden = container.childElementCount === 0;
  }

  function renderReplyReference(container, parentComment) {
    if (!parentComment) {
      container.hidden = true;
      return;
    }
    const authorNode = container.querySelector(".message__reply-author");
    const textNode = container.querySelector(".message__reply-text");
    const author = parentComment.username
      ? `${parentComment.display_name} @${parentComment.username}`
      : parentComment.display_name;
    const preview = parentComment.text || (parentComment.has_media ? "Фото" : "Комментарий");
    authorNode.textContent = author;
    textNode.textContent = preview;
    container.hidden = false;
  }

  function clearScheduledBottomScroll() {
    if (state.bottomScrollFrameId) {
      window.cancelAnimationFrame(state.bottomScrollFrameId);
      state.bottomScrollFrameId = 0;
    }
    if (state.bottomScrollTimeoutIds.length) {
      state.bottomScrollTimeoutIds.forEach(function (timeoutId) {
        window.clearTimeout(timeoutId);
      });
      state.bottomScrollTimeoutIds = [];
    }
  }

  function applyBottomScroll() {
    elements.list.scrollTop = elements.list.scrollHeight;
  }

  function scrollCommentsToBottom() {
    state.stickToBottom = true;
    clearScheduledBottomScroll();
    state.bottomScrollFrameId = requestAnimationFrame(function () {
      applyBottomScroll();
      state.bottomScrollFrameId = 0;
    });
    state.bottomScrollTimeoutIds = BOTTOM_SCROLL_RETRY_DELAYS_MS.map(function (delayMs) {
      return window.setTimeout(function () {
        applyBottomScroll();
      }, delayMs);
    });
  }

  function restoreScrollAfterPrepend(previousHeight, previousTop) {
    requestAnimationFrame(function () {
      const delta = elements.list.scrollHeight - previousHeight;
      elements.list.scrollTop = previousTop + delta;
    });
  }

  function restoreScrollPosition(previousTop) {
    requestAnimationFrame(function () {
      elements.list.scrollTop = previousTop;
    });
  }

  function getLastCommentId(comments) {
    if (!Array.isArray(comments) || !comments.length) {
      return null;
    }
    return Number(comments[comments.length - 1].id) || null;
  }

  function getCommentNode(commentId) {
    const id = Number(commentId);
    if (!id) {
      return null;
    }
    return elements.list.querySelector(`[data-comment-id="${id}"]`);
  }

  function getLastCommentNode() {
    const nodes = elements.list.querySelectorAll("[data-comment-id]");
    return nodes.length ? nodes[nodes.length - 1] : null;
  }

  function highlightCommentNode(node) {
    if (!node) {
      return;
    }
    if (state.targetHighlightTimeoutId) {
      window.clearTimeout(state.targetHighlightTimeoutId);
      state.targetHighlightTimeoutId = 0;
    }
    node.classList.add("message--target-highlight");
    state.targetHighlightTimeoutId = window.setTimeout(function () {
      node.classList.remove("message--target-highlight");
      state.targetHighlightTimeoutId = 0;
    }, 1800);
  }

  function scrollToComment(commentId, options) {
    const highlight = Boolean(options && options.highlight);
    clearScheduledBottomScroll();
    requestAnimationFrame(function () {
      const targetNode = getCommentNode(commentId) || getLastCommentNode();
      if (!targetNode) {
        return;
      }
      targetNode.scrollIntoView({
        block: "center",
        behavior: "auto",
      });
      state.stickToBottom = isListNearBottom();
      if (highlight) {
        highlightCommentNode(targetNode);
      }
    });
  }

  function updateLocalReadState(commentId) {
    const id = Number(commentId);
    if (!id) {
      return;
    }
    const currentReadId = state.readState && state.readState.lastReadCommentId
      ? Number(state.readState.lastReadCommentId)
      : null;
    if (currentReadId !== null && currentReadId >= id) {
      return;
    }
    state.readState = Object.assign({}, state.readState || {}, {
      lastReadCommentId: id,
      last_read_comment_id: id,
    });
    state.hasUnread = false;
  }

  async function markCommentsRead(commentId) {
    const id = Number(commentId);
    if (!id || !state.postRef || !state.initData || state.markReadInFlight) {
      return;
    }
    const currentReadId = state.readState && state.readState.lastReadCommentId
      ? Number(state.readState.lastReadCommentId)
      : null;
    if (currentReadId !== null && currentReadId >= id) {
      return;
    }

    state.markReadInFlight = true;
    try {
      const payload = await fetchJson(`/api/posts/${encodeURIComponent(state.postRef)}/comments/read`, {
        method: "POST",
        headers: buildApiHeaders({
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({
          lastReadCommentId: id,
          initData: state.initData,
        }),
      });
      const nextReadState = payload && payload.readState ? payload.readState : null;
      if (nextReadState && nextReadState.lastReadCommentId) {
        updateLocalReadState(nextReadState.lastReadCommentId);
      } else {
        updateLocalReadState(id);
      }
    } catch (error) {
      console.warn("Failed to mark comments as read:", error);
    } finally {
      state.markReadInFlight = false;
    }
  }

  function scheduleMarkCommentsRead(commentId) {
    const id = Number(commentId);
    if (!id) {
      return;
    }
    window.setTimeout(function () {
      markCommentsRead(id);
    }, 280);
  }

  function isListNearBottom() {
    const remaining = elements.list.scrollHeight - elements.list.scrollTop - elements.list.clientHeight;
    return remaining <= AUTO_REFRESH_NEAR_BOTTOM_PX;
  }

  function mergeLatestComments(currentComments, incomingComments) {
    const merged = Array.isArray(currentComments) ? currentComments.slice() : [];
    const indexById = new Map();

    merged.forEach(function (comment, index) {
      indexById.set(Number(comment.id), index);
    });

    (Array.isArray(incomingComments) ? incomingComments : []).forEach(function (comment) {
      const id = Number(comment.id);
      if (indexById.has(id)) {
        merged[indexById.get(id)] = comment;
        return;
      }
      indexById.set(id, merged.length);
      merged.push(comment);
    });

    merged.sort(function (left, right) {
      return Number(left.id) - Number(right.id);
    });
    return merged;
  }

  function applyThreadPayload(payload, options) {
    const loadOlder = Boolean(options && options.loadOlder);
    const mergeLatest = Boolean(options && options.mergeLatest);
    const incomingComments = Array.isArray(payload.comments) ? payload.comments : [];
    const previousCount = state.post ? Number(state.post.comment_count || state.comments.length) : state.comments.length;

    if (payload.viewer) {
      state.isAdmin = Boolean(payload.viewer.is_admin);
      if (payload.viewer.user_id) {
        state.currentUserId = Number(payload.viewer.user_id);
      }
    }
    if (!loadOlder) {
      state.readState = payload.readState || payload.read_state || state.readState;
      state.targetCommentId = Number(payload.targetCommentId || payload.target_comment_id) || null;
      state.hasUnread = Boolean(payload.hasUnread || payload.has_unread);
    }

    renderPost(payload.post);
    const nextCount = state.post ? Number(state.post.comment_count || incomingComments.length) : incomingComments.length;

    if (loadOlder) {
      state.comments = incomingComments.concat(state.comments);
      if (incomingComments.length) {
        state.loadedOlderHistory = true;
      }
      state.hasMore = Boolean(payload.page && payload.page.has_more);
    } else if (mergeLatest && state.comments.length && state.loadedOlderHistory && nextCount >= previousCount) {
      state.comments = mergeLatestComments(state.comments, incomingComments);
    } else {
      state.comments = incomingComments;
      state.loadedOlderHistory = false;
      state.hasMore = Boolean(payload.page && payload.page.has_more);
    }

    state.oldestCommentId = state.comments.length ? Number(state.comments[0].id) : null;
    if (!loadOlder && (!mergeLatest || !state.loadedOlderHistory)) {
      state.hasMore = Boolean(payload.page && payload.page.has_more);
    }
  }

  function renderEmptyState() {
    const node = elements.template.content.firstElementChild.cloneNode(true);
    node.classList.add("message--empty");
    node.querySelector(".message__avatar").remove();
    const menuButton = node.querySelector(".message__menu-button");
    if (menuButton) {
      menuButton.remove();
    }
    node.querySelector(".message__author").textContent = "Лента комментариев";
    node.querySelector(".message__time").textContent = "Сейчас";
    node.querySelector(".message__text").textContent =
      "Пока здесь тихо. Первое сообщение в этой ветке может быть вашим.";
    elements.list.appendChild(node);
  }

  function renderDayDivider(label) {
    const divider = document.createElement("div");
    divider.className = "day-divider";
    divider.innerHTML = `<span>${label}</span>`;
    elements.list.appendChild(divider);
  }

  function canEditComment(comment) {
    if (!comment || state.currentUserId === null) {
      return false;
    }
    return Number(comment.user_id) === Number(state.currentUserId);
  }

  function canReplyComment(comment) {
    return Boolean(comment) && state.insideMax;
  }

  function canDeleteComment(comment) {
    if (!comment) {
      return false;
    }
    if (state.isAdmin) {
      return true;
    }
    if (state.currentUserId === null) {
      return false;
    }
    return Number(comment.user_id) === Number(state.currentUserId);
  }

  function canReportComment(comment) {
    if (!comment || !state.insideMax || state.currentUserId === null) {
      return false;
    }
    return Number(comment.user_id) !== Number(state.currentUserId);
  }

  function hasCommentActions(comment) {
    return (
      canReplyComment(comment) ||
      canEditComment(comment) ||
      canDeleteComment(comment) ||
      canReportComment(comment)
    );
  }

  function getCommentById(commentId) {
    return state.comments.find(function (comment) {
      return Number(comment.id) === Number(commentId);
    }) || null;
  }

  function useTapCommentActions() {
    const platform = String((webApp && webApp.platform) || "").toLowerCase();
    if (platform === "ios" || platform === "android") {
      return true;
    }
    if (window.matchMedia) {
      return (
        window.matchMedia("(pointer: coarse)").matches ||
        window.matchMedia("(hover: none)").matches
      );
    }
    return false;
  }

  function closeCommentMenu() {
    state.contextMenuCommentId = null;
    state.menuOpenedAt = 0;
    elements.commentMenu.hidden = true;
    elements.commentMenuBackdrop.hidden = true;
  }

  function isCommentLongPressIgnoredTarget(target) {
    return Boolean(
      target &&
      target.closest(".message__image-button, .message__menu-button, .message__author-target, button, a, input, textarea, select")
    );
  }

  function positionCommentMenu(clientX, clientY) {
    const menu = elements.commentMenu;
    menu.style.left = "0px";
    menu.style.top = "0px";
    requestAnimationFrame(function () {
      const menuRect = menu.getBoundingClientRect();
      const maxLeft = Math.max(12, window.innerWidth - menuRect.width - 12);
      const maxTop = Math.max(12, window.innerHeight - menuRect.height - 12);
      const left = Math.min(Math.max(12, clientX), maxLeft);
      const top = Math.min(Math.max(12, clientY), maxTop);
      menu.style.left = `${left}px`;
      menu.style.top = `${top}px`;
    });
  }

  function openCommentMenu(commentId, clientX, clientY) {
    const comment = getCommentById(commentId);
    if (!hasCommentActions(comment)) {
      return;
    }

    state.contextMenuCommentId = Number(commentId);
    state.menuOpenedAt = Date.now();
    elements.commentMenuReply.hidden = !canReplyComment(comment);
    elements.commentMenuEdit.hidden = !canEditComment(comment);
    elements.commentMenuDelete.hidden = !canDeleteComment(comment);
    elements.commentMenuReport.hidden = !canReportComment(comment);
    elements.commentMenu.hidden = false;
    elements.commentMenuBackdrop.hidden = false;
    positionCommentMenu(clientX, clientY);
  }

  function renderReplyState() {
    const comment = getCommentById(state.replyToCommentId);
    if (!comment) {
      state.replyToCommentId = null;
      elements.replyState.hidden = true;
      elements.replyCaption.textContent = "";
      return;
    }
    const author = comment.username
      ? `${comment.display_name} @${comment.username}`
      : comment.display_name;
    const text = comment.text || (comment.media && comment.media.length ? "Фото" : "Комментарий");
    elements.replyState.hidden = false;
    elements.replyCaption.textContent = `${author}: ${text}`.slice(0, 180);
  }

  function renderEditorState() {
    const comment = getCommentById(state.editingCommentId);
    if (!comment) {
      state.editingCommentId = null;
      elements.editorState.hidden = true;
      elements.editorCaption.textContent = "";
      return;
    }
    elements.editorState.hidden = false;
    elements.editorCaption.textContent = comment.media && comment.media.length
      ? "Фото останется на месте, можно изменить текст комментария"
      : "Измените текст и отправьте комментарий заново";
  }

  function cancelEditing(options) {
    state.editingCommentId = null;
    renderEditorState();
    if (options && options.clearInput) {
      elements.input.value = "";
      updateComposerCount();
    }
    syncComposerState();
  }

  function cancelReplying() {
    state.replyToCommentId = null;
    renderReplyState();
    syncComposerState();
  }

  function beginEditingComment(commentId) {
    const comment = getCommentById(commentId);
    if (!canEditComment(comment)) {
      return;
    }
    cancelReplying();
    clearPhotoSelection();
    closeCommentMenu();
    state.editingCommentId = Number(commentId);
    elements.input.value = comment.text || "";
    updateComposerCount();
    renderEditorState();
    syncComposerState();
    elements.input.focus();
    elements.input.setSelectionRange(elements.input.value.length, elements.input.value.length);
  }

  function beginReplyingToComment(commentId) {
    const comment = getCommentById(commentId);
    if (!canReplyComment(comment)) {
      return;
    }
    if (state.editingCommentId !== null) {
      cancelEditing({ clearInput: false });
    }
    closeCommentMenu();
    state.replyToCommentId = Number(commentId);
    renderReplyState();
    syncComposerState();
    elements.input.focus();
  }

  function closeReportModal() {
    state.reportingCommentId = null;
    elements.reportModal.hidden = true;
    elements.reportDetails.value = "";
    elements.reportReason.value = "insult";
    elements.reportSubmit.disabled = false;
  }

  function beginReportingComment(commentId) {
    const comment = getCommentById(commentId);
    if (!canReportComment(comment)) {
      return;
    }
    closeCommentMenu();
    state.reportingCommentId = Number(commentId);
    elements.reportReason.value = "insult";
    elements.reportDetails.value = "";
    elements.reportSubmit.disabled = false;
    elements.reportModal.hidden = false;
    elements.reportReason.focus();
  }

  async function submitReport(event) {
    event.preventDefault();
    if (state.submittingReport || state.reportingCommentId === null) {
      return;
    }
    const comment = getCommentById(state.reportingCommentId);
    if (!canReportComment(comment)) {
      closeReportModal();
      return;
    }
    state.submittingReport = true;
    elements.reportSubmit.disabled = true;
    try {
      const result = await fetchJson(`/api/comments/${encodeURIComponent(state.reportingCommentId)}/report`, {
        method: "POST",
        headers: buildApiHeaders({
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({
          initData: state.initData,
          reason: elements.reportReason.value,
          details: elements.reportDetails.value.trim(),
        }),
      });
      closeReportModal();
      setBanner(result.message || "Жалоба отправлена. Администратор канала проверит комментарий.", "success");
      haptic("success");
    } catch (error) {
      setBanner(error.message || "Не удалось отправить жалобу.", "error");
      haptic("warning");
      if (error.code === "REPORT_ALREADY_EXISTS") {
        closeReportModal();
      }
    } finally {
      state.submittingReport = false;
      elements.reportSubmit.disabled = false;
    }
  }

  function bindCommentActionHandlers(node, comment) {
    if (!hasCommentActions(comment)) {
      return;
    }
    node.classList.add("message--actionable");
    const menuButton = node.querySelector(".message__menu-button");
    const bubble = node.querySelector(".message__bubble");
    if (menuButton) {
      menuButton.hidden = false;
      menuButton.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        if (state.submitting || state.deletingCommentId !== null) {
          return;
        }
        const rect = menuButton.getBoundingClientRect();
        openCommentMenu(comment.id, rect.left, rect.bottom + 4);
        haptic("light");
      });
    }
    if (useTapCommentActions()) {
      node.addEventListener("pointerdown", function (event) {
        if (
          state.submitting ||
          state.deletingCommentId !== null ||
          event.pointerType !== "touch" ||
          isCommentLongPressIgnoredTarget(event.target)
        ) {
          return;
        }
        releaseCommentPressState();
        if (bubble) {
          bubble.classList.add("message__bubble--pressing");
        }
        state.commentPress = {
          bubble,
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          clientX: event.clientX,
          clientY: event.clientY,
          longPressTriggered: false,
          timerId: window.setTimeout(function () {
            const pressState = state.commentPress;
            if (!pressState || pressState.pointerId !== event.pointerId) {
              return;
            }
            pressState.longPressTriggered = true;
            openCommentMenuFromAnchor(comment.id, pressState.clientX, pressState.clientY, bubble || node);
            if (bubble) {
              bubble.classList.remove("message__bubble--pressing");
            }
          }, LONG_PRESS_DELAY_MS),
        };
      });

      node.addEventListener("pointermove", function (event) {
        const pressState = state.commentPress;
        if (!pressState || pressState.pointerId !== event.pointerId) {
          return;
        }
        pressState.clientX = event.clientX;
        pressState.clientY = event.clientY;
        if (isLongPressMovementExceeded(pressState, event.clientX, event.clientY)) {
          releaseCommentPressState();
        }
      });

      node.addEventListener("pointerup", function (event) {
        const pressState = state.commentPress;
        if (!pressState || pressState.pointerId !== event.pointerId) {
          return;
        }
        const longPressTriggered = pressState.longPressTriggered;
        releaseCommentPressState();
        if (longPressTriggered) {
          event.preventDefault();
        }
      });

      node.addEventListener("pointercancel", function (event) {
        const pressState = state.commentPress;
        if (!pressState || pressState.pointerId !== event.pointerId) {
          return;
        }
        releaseCommentPressState();
      });
      return;
    }

    node.addEventListener("contextmenu", function (event) {
      if (event.target.closest(".message__image-button")) {
        return;
      }
      event.preventDefault();
      openCommentMenu(comment.id, event.clientX, event.clientY);
    });
  }

  function updateThreadControls() {
    elements.loadOlder.hidden = !state.hasMore;
    elements.loadOlder.disabled = state.loadingOlder;
    elements.loadOlder.textContent = state.loadingOlder
      ? "Загружаем историю…"
      : "Показать ранние сообщения";
  }

  function renderComments(options) {
    const preserveScroll = Boolean(options && options.preserveScroll);
    const preserveViewport = Boolean(options && options.preserveViewport);
    const targetCommentId = Number(options && options.targetCommentId) || null;
    const highlightTarget = Boolean(options && options.highlightTarget);
    const previousHeight = preserveScroll ? elements.list.scrollHeight : 0;
    const previousTop = preserveScroll ? elements.list.scrollTop : 0;
    const stableTop = preserveViewport ? elements.list.scrollTop : 0;

    closeCommentMenu();
    elements.list.innerHTML = "";

    if (state.post) {
      renderPostCard(state.post);
    }

    if (!state.comments.length) {
      if (state.replyToCommentId !== null) {
        cancelReplying();
      }
      if (state.editingCommentId !== null) {
        cancelEditing({ clearInput: true });
      }
      clearBanner();
      renderEmptyState();
      updateThreadControls();
      scrollCommentsToBottom();
      return;
    }

    clearBanner();

    let currentDayKey = "";
    state.comments.forEach(function (comment) {
      const nextDayKey = getDateKey(comment.created_at);
      if (nextDayKey !== currentDayKey) {
        currentDayKey = nextDayKey;
        renderDayDivider(formatDayDivider(comment.created_at));
      }

      const node = elements.template.content.firstElementChild.cloneNode(true);
      const isSelf = state.currentUserId !== null && Number(comment.user_id) === Number(state.currentUserId);
      const authorText = String(comment.display_name || "").trim() || "Пользователь";
      const replyNode = node.querySelector(".message__reply");
      const mediaNode = node.querySelector(".message__media");
      const textNode = node.querySelector(".message__text");
      const menuButton = node.querySelector(".message__menu-button");

      if (isSelf) {
        node.classList.add("message--self");
      }

      const author = commentAuthorData(comment) || {
        displayName: authorText,
        avatarUrl: null,
        profileUrl: null,
      };
      node.style.setProperty("--avatar-hue", String(avatarHue(author.displayName)));
      node.dataset.commentId = String(comment.id);
      renderAvatarBadge(node.querySelector(".message__avatar"), author);
      node.querySelector(".message__author").textContent = authorText;
      node.querySelector(".message__time").textContent = formatCommentTime(comment.created_at);
      renderReplyReference(replyNode, comment.parent_comment);
      renderCommentMedia(mediaNode, comment.media, comment.id);
      textNode.textContent = comment.text || "";
      textNode.hidden = !comment.text;
      if (menuButton) {
        menuButton.hidden = true;
      }
      bindAuthorProfileHandlers(node, comment);
      bindCommentActionHandlers(node, comment);
      elements.list.appendChild(node);
    });

    if (state.editingCommentId !== null && !getCommentById(state.editingCommentId)) {
      cancelEditing({ clearInput: true });
    }
    if (state.replyToCommentId !== null && !getCommentById(state.replyToCommentId)) {
      cancelReplying();
    }
    renderReplyState();
    renderEditorState();

    updateThreadControls();
    if (preserveScroll) {
      restoreScrollAfterPrepend(previousHeight, previousTop);
    } else if (preserveViewport) {
      restoreScrollPosition(stableTop);
    } else if (targetCommentId !== null) {
      scrollToComment(targetCommentId, { highlight: highlightTarget });
    } else {
      scrollCommentsToBottom();
    }
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const rawText = await response.text();
    let payload = {};
    try {
      payload = rawText ? JSON.parse(rawText) : {};
    } catch (_err) {
      payload = {};
    }
    if (!response.ok) {
      const error = new Error(payload.message || payload.error || "Ошибка запроса");
      error.code = payload.error || "";
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  async function loadThread(options) {
    const loadOlder = Boolean(options && options.loadOlder);
    const silent = Boolean(options && options.silent);
    const mergeLatest = Boolean(options && options.mergeLatest);
    const preserveViewport = Boolean(options && options.preserveViewport);
    if (!state.postRef) {
      setBanner("Не удалось определить пост. Откройте приложение из кнопки под постом в MAX.", "error");
      elements.submit.disabled = true;
      return false;
    }

    const params = new URLSearchParams({
      limit: String(COMMENTS_PAGE_LIMIT),
    });
    if (loadOlder && state.oldestCommentId) {
      params.set("before_id", String(state.oldestCommentId));
    }

    const payload = await fetchJson(
      `/api/posts/${encodeURIComponent(state.postRef)}/comments?${params.toString()}`,
      {
        headers: buildApiHeaders(),
      }
    );
    const incomingComments = Array.isArray(payload.comments) ? payload.comments : [];
    const previousCount = state.post ? Number(state.post.comment_count || state.comments.length) : state.comments.length;
    const nextCount = payload.post ? Number(payload.post.comment_count || incomingComments.length) : incomingComments.length;
    const previousLastId = getLastCommentId(state.comments);
    const nextLastId = getLastCommentId(incomingComments);
    const previousUpdatedAt = state.post ? String(state.post.updated_at || "") : "";
    const nextUpdatedAt = payload.post ? String(payload.post.updated_at || "") : "";
    const targetCommentId = Number(payload.targetCommentId || payload.target_comment_id) || null;
    const shouldUseInitialTarget = Boolean(
      targetCommentId &&
      !loadOlder &&
      !mergeLatest &&
      !state.initialTargetScrollDone
    );

    if (
      silent &&
      !loadOlder &&
      previousCount === nextCount &&
      previousLastId === nextLastId &&
      previousUpdatedAt === nextUpdatedAt
    ) {
      return false;
    }

    applyThreadPayload(payload, { loadOlder, mergeLatest });
    renderComments({
      preserveScroll: loadOlder,
      preserveViewport,
      targetCommentId: shouldUseInitialTarget ? targetCommentId : null,
      highlightTarget: shouldUseInitialTarget && Boolean(payload.hasUnread || payload.has_unread),
    });
    if (shouldUseInitialTarget) {
      state.initialTargetScrollDone = true;
      scheduleMarkCommentsRead(getLastCommentId(state.comments));
    } else if (!loadOlder && !preserveViewport && state.stickToBottom) {
      scheduleMarkCommentsRead(getLastCommentId(state.comments));
    }
    return true;
  }

  async function pollLatestComments() {
    if (
      state.autoRefreshInFlight ||
      state.submitting ||
      state.loadingOlder ||
      state.deletingCommentId !== null ||
      !state.postRef ||
      document.visibilityState === "hidden"
    ) {
      return;
    }

    state.autoRefreshInFlight = true;
    try {
      await loadThread({
        loadOlder: false,
        silent: true,
        mergeLatest: true,
        preserveViewport: !state.stickToBottom,
      });
    } catch (error) {
      console.warn("Auto-refresh failed:", error);
    } finally {
      state.autoRefreshInFlight = false;
    }
  }

  function stopAutoRefresh() {
    if (state.autoRefreshTimer !== null) {
      window.clearInterval(state.autoRefreshTimer);
      state.autoRefreshTimer = null;
    }
  }

  function startAutoRefresh() {
    stopAutoRefresh();
    if (!state.postRef || document.visibilityState === "hidden") {
      return;
    }
    state.autoRefreshTimer = window.setInterval(function () {
      pollLatestComments();
    }, AUTO_REFRESH_INTERVAL_MS);
  }

  async function deleteComment(commentId) {
    if (state.deletingCommentId !== null) {
      return;
    }

    const targetComment = state.comments.find(function (comment) {
      return Number(comment.id) === Number(commentId);
    });
    if (!canDeleteComment(targetComment)) {
      return;
    }
    if (!window.confirm("Удалить этот комментарий?")) {
      return;
    }

    if (state.editingCommentId !== null && Number(state.editingCommentId) === Number(commentId)) {
      cancelEditing({ clearInput: true });
    }
    if (state.replyToCommentId !== null && Number(state.replyToCommentId) === Number(commentId)) {
      cancelReplying();
    }
    closeCommentMenu();
    state.deletingCommentId = commentId;
    renderComments({ preserveScroll: false });
    setBanner("Удаляем комментарий…", "loading");

    try {
      const result = await fetchJson(`/api/posts/${encodeURIComponent(state.postRef)}/comments/${commentId}`, {
        method: "DELETE",
        headers: buildApiHeaders(),
      });
      await loadThread({ loadOlder: false });
      if (result && result.warning) {
        setBanner(String(result.warning), "warning");
        haptic("warning");
      } else {
        clearBanner();
        haptic("success");
      }
    } catch (error) {
      setBanner(error.message || "Не удалось удалить комментарий.", "error");
      haptic("error");
      renderComments({ preserveScroll: false });
    } finally {
      state.deletingCommentId = null;
      renderComments({ preserveScroll: false });
    }
  }

  function syncComposerState() {
    if (!state.insideMax) {
      elements.submit.disabled = true;
      elements.input.disabled = true;
      elements.photoButton.disabled = true;
      elements.photoInput.disabled = true;
      elements.previewRemove.disabled = true;
      elements.input.placeholder = "Откройте чат внутри MAX, чтобы ответить";
      return;
    }

    elements.input.disabled = false;
    elements.photoButton.disabled = state.submitting || state.editingCommentId !== null;
    elements.photoInput.disabled = state.submitting || state.editingCommentId !== null;
    elements.previewRemove.disabled = state.submitting;
    elements.submit.disabled = state.submitting;
    if (state.editingCommentId !== null) {
      elements.input.placeholder = "Измените комментарий";
    } else if (state.replyToCommentId !== null) {
      elements.input.placeholder = "Ответ на комментарий";
    } else {
      elements.input.placeholder = "Сообщение";
    }
  }

  async function onSubmit(event) {
    event.preventDefault();
    const text = elements.input.value.trim();
    const photo = state.pendingPhoto;
    const isEditing = state.editingCommentId !== null;
    const replyToCommentId = state.replyToCommentId;
    if ((!isEditing && !text && !photo) || !state.postRef) {
      return;
    }
    if (containsForbiddenLink(text)) {
      setBanner(LINKS_BLOCKED_MESSAGE, "error");
      haptic("warning");
      return;
    }

    state.submitting = true;
    syncComposerState();
    setBanner(isEditing ? "Сохраняем изменения…" : "Отправляем сообщение…", "loading");
    haptic("light");

    try {
      if (isEditing) {
        await fetchJson(
          `/api/posts/${encodeURIComponent(state.postRef)}/comments/${encodeURIComponent(state.editingCommentId)}`,
          {
            method: "PATCH",
            headers: buildApiHeaders({
              "Content-Type": "application/json",
            }),
            body: JSON.stringify({
              text,
              initData: state.initData,
            }),
          }
        );
      } else {
        await fetchJson(`/api/posts/${encodeURIComponent(state.postRef)}/comments`, {
          method: "POST",
          headers: buildApiHeaders({
            "Content-Type": "application/json",
          }),
          body: JSON.stringify({
            text,
            initData: state.initData,
            reply_to_comment_id: replyToCommentId,
            photo: photo
              ? {
                  data_url: photo.dataUrl,
                  file_name: photo.fileName,
                  mime_type: photo.mimeType,
                  width: photo.width,
                  height: photo.height,
                  size_bytes: photo.sizeBytes,
                }
              : null,
          }),
        });
      }
      elements.input.value = "";
      updateComposerCount();
      clearPhotoSelection();
      cancelEditing();
      cancelReplying();
      await loadThread({ loadOlder: false });
      clearBanner();
      haptic("success");
    } catch (error) {
      if (error.code === COMMENT_BLOCKED_CODE) {
        showCommentBlockedDialog();
        haptic("warning");
        return;
      }
      setBanner(error.message || "Не удалось отправить комментарий.", "error");
      haptic("error");
    } finally {
      state.submitting = false;
      syncComposerState();
    }
  }

  function onPickPhoto() {
    if (!elements.photoInput || elements.photoInput.disabled) {
      return;
    }
    elements.photoInput.click();
  }

  async function onPhotoChange(event) {
    const file = event.target && event.target.files ? event.target.files[0] : null;
    if (!file) {
      return;
    }
    if (!String(file.type || "").startsWith("image/")) {
      clearPhotoSelection();
      setBanner("Можно прикрепить только изображение.", "error");
      haptic("error");
      return;
    }

    setBanner("Подготавливаем фото…", "loading");
    try {
      state.pendingPhoto = await compressImageFile(file);
      renderPhotoSelection();
      clearBanner();
      haptic("success");
    } catch (error) {
      clearPhotoSelection();
      setBanner(error.message || "Не удалось подготовить фото.", "error");
      haptic("error");
    }
  }

  async function bootstrap() {
    if (webApp) {
      state.insideMax = true;
      state.initData = webApp.initData || "";
      if (webApp.initDataUnsafe && webApp.initDataUnsafe.user && webApp.initDataUnsafe.user.id) {
        state.currentUserId = Number(webApp.initDataUnsafe.user.id);
      }
      if (webApp.ready) {
        webApp.ready();
      }
      if (webApp.expand) {
        webApp.expand();
      }
      if (webApp.BackButton && webApp.BackButton.show) {
        webApp.BackButton.show();
        webApp.BackButton.onClick(function () {
          webApp.close();
        });
      }
      if (elements.root && webApp.platform) {
        elements.root.dataset.platform = String(webApp.platform);
      }
    }

    state.postRef = getPostRef();
    updateComposerCount();
    renderPhotoSelection();

    await loadThread({ loadOlder: false }).catch(function (error) {
      setBanner(error.message || "Не удалось загрузить комментарии.", "error");
      haptic("warning");
    });

    syncComposerState();
    startAutoRefresh();
  }

  elements.form.addEventListener("submit", onSubmit);
  elements.input.addEventListener("input", function () {
    updateComposerCount();
    clearComposerError();
  });
  elements.photoButton.addEventListener("click", onPickPhoto);
  elements.photoInput.addEventListener("change", onPhotoChange);
  elements.replyCancel.addEventListener("click", cancelReplying);
  elements.editorCancel.addEventListener("click", function () {
    cancelEditing({ clearInput: true });
  });
  elements.previewRemove.addEventListener("click", function () {
    clearPhotoSelection();
    clearBanner();
  });
  elements.commentMenuReply.addEventListener("click", function () {
    if (state.contextMenuCommentId !== null) {
      beginReplyingToComment(state.contextMenuCommentId);
    }
  });
  elements.commentMenuEdit.addEventListener("click", function () {
    if (state.contextMenuCommentId !== null) {
      beginEditingComment(state.contextMenuCommentId);
    }
  });
  elements.commentMenuDelete.addEventListener("click", function () {
    if (state.contextMenuCommentId !== null) {
      deleteComment(state.contextMenuCommentId);
    }
  });
  elements.commentMenuReport.addEventListener("click", function () {
    if (state.contextMenuCommentId !== null) {
      beginReportingComment(state.contextMenuCommentId);
    }
  });
  elements.commentMenuBackdrop.addEventListener("click", closeCommentMenu);
  elements.reportForm.addEventListener("submit", submitReport);
  elements.reportCancel.addEventListener("click", closeReportModal);
  elements.reportModal.addEventListener("click", function (event) {
    if (event.target === elements.reportModal) {
      closeReportModal();
    }
  });
  elements.blockedFix.addEventListener("click", closeCommentBlockedDialog);
  elements.blockedModal.addEventListener("click", function (event) {
    if (event.target === elements.blockedModal) {
      closeCommentBlockedDialog();
    }
  });
  elements.imageViewerClose.addEventListener("click", closeImageViewer);
  elements.imageViewerPrev.addEventListener("click", function (event) {
    event.stopPropagation();
    showPrevImage();
  });
  elements.imageViewerNext.addEventListener("click", function (event) {
    event.stopPropagation();
    showNextImage();
  });
  elements.imageViewer.addEventListener("click", function (event) {
    if (event.target === elements.imageViewer) {
      closeImageViewer();
    }
  });
  elements.list.addEventListener("scroll", function () {
    releaseImagePressState();
    releaseCommentPressState();
    state.stickToBottom = isListNearBottom();
    closeCommentMenu();
  }, { passive: true });
  elements.input.addEventListener("focus", function () {
    if (!state.stickToBottom) {
      return;
    }
    window.setTimeout(function () {
      scrollCommentsToBottom();
    }, 120);
  });
  elements.input.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      if (!elements.reportModal.hidden) {
        closeReportModal();
        return;
      }
      if (!elements.blockedModal.hidden) {
        closeCommentBlockedDialog();
        return;
      }
      if (state.contextMenuCommentId !== null) {
        closeCommentMenu();
        return;
      }
      if (state.editingCommentId !== null) {
        cancelEditing({ clearInput: true });
        return;
      }
      if (state.replyToCommentId !== null) {
        cancelReplying();
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      elements.form.requestSubmit();
    }
  });
  elements.input.addEventListener("paste", function (event) {
    const clipboard = event.clipboardData || window.clipboardData;
    const pastedText = clipboard ? clipboard.getData("text") : "";
    if (!containsForbiddenLink(pastedText)) {
      return;
    }
    event.preventDefault();
    setBanner(LINKS_BLOCKED_MESSAGE, "error");
    haptic("warning");
  });
  document.addEventListener("click", function (event) {
    if (state.menuOpenedAt && Date.now() - state.menuOpenedAt < 250) {
      return;
    }
    if (
      state.contextMenuCommentId !== null &&
      !elements.commentMenu.contains(event.target) &&
      !elements.commentMenuBackdrop.contains(event.target)
    ) {
      closeCommentMenu();
    }
  });
  document.addEventListener("keydown", function (event) {
    if (!elements.imageViewer.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeImageViewer();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        showPrevImage();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        showNextImage();
      }
      return;
    }
  });
  elements.loadOlder.addEventListener("click", function () {
    if (state.loadingOlder || !state.hasMore) {
      return;
    }
    state.loadingOlder = true;
    updateThreadControls();
    setBanner("Загружаем историю…", "loading");
    loadThread({ loadOlder: true })
      .then(function () {
        clearBanner();
      })
      .catch(function (error) {
        setBanner(error.message || "Не удалось загрузить ранние сообщения.", "error");
        haptic("error");
      })
      .finally(function () {
        state.loadingOlder = false;
        updateThreadControls();
      });
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      startAutoRefresh();
      pollLatestComments();
      return;
    }
    stopAutoRefresh();
  });
  window.addEventListener("pagehide", stopAutoRefresh);

  bootstrap();
})();

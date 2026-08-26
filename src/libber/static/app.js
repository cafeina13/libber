"use strict";

const $ = (id) => document.getElementById(id);
const rows = new Map();       // spotify track id -> row element
let playlist = null;
let jobId = null;
let stream = null;
let settings = {};
let isDirect = false;   // YouTube source: nothing was matched, so no match line

const KIND_LABELS = {
  playlist: "playlist",
  album: "album",
  liked: "liked songs",
  track: "single track",
  "yt-playlist": "youtube playlist",
  "yt-video": "youtube video",
};

/* ---------------------------------------------------------------- utils */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && (data.message || data.detail)) || res.statusText);
    err.payload = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

const mmss = (ms) => {
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

function banner(el, text, kind = "") {
  el.textContent = text;
  el.className = `banner ${kind}`.trim();
  el.classList.toggle("hidden", !text);
}

/* --------------------------------------------------------------- status */

async function refreshStatus() {
  const s = await api("/api/status");
  settings = s.settings;

  $("redirect-uri").textContent = s.redirect_uri;
  $("redirect-uri-2").textContent = s.redirect_uri;
  $("setup").classList.toggle("hidden", s.has_credentials);
  $("folder-label").textContent = s.settings.output_dir;

  // Playlist contents need a signed-in user, so prompt before the failure
  // rather than after it.
  $("signin-required").classList.toggle("hidden", !s.has_credentials || s.logged_in);

  $("set-output").value = s.settings.output_dir;
  $("set-jobs").value = s.settings.concurrency;
  $("set-threshold").value = s.settings.match_threshold;
  $("thr-value").textContent = s.settings.match_threshold;
  $("set-skip").checked = s.settings.skip_low_matches;
  $("set-enrich").checked = s.settings.enrich_youtube;
  $("set-sleep").value = s.settings.sleep_between;
  $("sleep-value").textContent = `${s.settings.sleep_between}s`;
  $("set-quality").value = s.settings.audio_quality || "standard";

  // Only browsers actually installed here are offered; a Firefox fork carries
  // its profile path because yt-dlp can't locate it by name.
  const select = $("set-cookies");
  const current = `${s.settings.cookies_browser}|${s.settings.cookies_profile}`;
  select.innerHTML = "";
  const none = document.createElement("option");
  none.value = "|";
  none.textContent = "none (likely to hit the bot check)";
  select.appendChild(none);
  (s.browsers || []).forEach((b) => {
    const opt = document.createElement("option");
    opt.value = `${b.browser}|${b.profile}`;
    opt.textContent = b.label;
    select.appendChild(opt);
  });
  select.value = [...select.options].some((o) => o.value === current) ? current : "|";

  const account = $("account");
  if (s.user) {
    account.innerHTML = "";
    if (s.user.image) {
      const img = document.createElement("img");
      img.src = s.user.image;
      account.appendChild(img);
    }
    const name = document.createElement("span");
    name.className = "muted";
    name.textContent = s.user.name;
    account.appendChild(name);
  } else {
    account.innerHTML = "";
  }
  return s;
}

async function startLogin() {
  try {
    const { url } = await api("/api/login");
    const popup = window.open(url, "spotify-login", "width=520,height=720");
    const timer = setInterval(async () => {
      const s = await refreshStatus();
      if (s.logged_in) {
        clearInterval(timer);
        if (popup && !popup.closed) popup.close();
        banner($("loader-error"), "");
      } else if (popup && popup.closed) {
        clearInterval(timer);
      }
    }, 1200);
  } catch (err) {
    banner($("loader-error"), err.message, "error");
  }
}

/* ------------------------------------------------------------- playlist */

const SOURCES = {
  spotify: {
    endpoint: "/api/playlist",
    input: "playlist-url",
    button: "load-btn",
    error: "loader-error",
    empty: "Paste a Spotify playlist link into the box first.",
  },
  youtube: {
    endpoint: "/api/youtube",
    input: "yt-url",
    button: "yt-load-btn",
    error: "yt-error",
    empty: "Paste a YouTube playlist or video link into the box first.",
  },
};

async function loadFrom(which) {
  const cfg = SOURCES[which];
  const url = $(cfg.input).value.trim();
  const errorEl = $(cfg.error);
  if (!url) {
    banner(errorEl, cfg.empty, "error");
    $(cfg.input).focus();
    return;
  }
  const btn = $(cfg.button);
  btn.disabled = true;
  btn.textContent = "Loading…";
  banner(errorEl, "");
  // Clear the other card's stale error so only one is ever showing.
  Object.values(SOURCES).forEach((o) => o !== cfg && banner($(o.error), ""));

  try {
    const data = await api(cfg.endpoint, { method: "POST", body: { url } });
    renderPlaylist(data);
  } catch (err) {
    if (err.status === 401) {
      banner(errorEl, `${err.message} Use the “Sign in with Spotify” button above.`, "error");
      $("signin-required").classList.remove("hidden");
      $("signin-required").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else {
      banner(errorEl, err.message, "error");
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Load";
  }
}

function renderPlaylist(data) {
  playlist = data;
  rows.clear();
  jobId = null;

  const pl = data.playlist;
  isDirect = !!pl.direct;
  $("pl-kind").textContent = KIND_LABELS[pl.kind] || pl.kind;
  $("pl-kind").classList.toggle("yt", isDirect);
  $("pl-name").textContent = pl.name;
  $("pl-owner").textContent = pl.owner ? `by ${pl.owner}` : "";
  $("pl-cover").src = pl.image || "";
  $("pl-cover").style.visibility = pl.image ? "visible" : "hidden";

  const sync = data.sync;
  const parts = [
    `<span><b>${sync.total}</b> tracks</span>`,
    `<span class="muted"><b>${sync.new.length}</b> new</span>`,
    `<span class="muted"><b>${sync.existing.length}</b> already downloaded</span>`,
  ];
  if (sync.removed.length) {
    parts.push(`<span class="muted"><b>${sync.removed.length}</b> removed from playlist since last sync</span>`);
  }
  if (pl.skipped && pl.skipped.length) {
    parts.push(`<span class="muted"><b>${pl.skipped.length}</b> skipped (local files / episodes)</span>`);
  }
  $("sync-summary").innerHTML = parts.join("");

  const container = $("tracks");
  container.innerHTML = "";
  const template = $("track-template");

  data.tracks.forEach((track, i) => {
    const row = template.content.firstElementChild.cloneNode(true);
    row.dataset.id = track.id;
    row.querySelector(".tnum").textContent = i + 1;
    row.querySelector(".tart").src = track.cover_url || "";
    const titleEl = row.querySelector(".ttitle");
    const artistEl = row.querySelector(".tartist");
    titleEl.textContent = track.title;
    artistEl.textContent = `${track.artist} · ${mmss(track.duration_ms)}`;
    // These columns ellipsise, so keep the full text reachable on hover.
    titleEl.title = track.title;
    artistEl.title = `${track.artist} · ${mmss(track.duration_ms)}`;
    if (track.album) artistEl.title += `\n${track.album}`;
    if (track.downloaded) {
      row.dataset.status = "exists";
      row.querySelector(".tstatus").textContent = "already downloaded";
      row.querySelector(".tsel").checked = false;
    }
    container.appendChild(row);
    rows.set(track.id, row);
  });

  $("playlist").classList.remove("hidden");
  banner($("job-banner"), "");
  updateDownloadButton();
}

function selectTracks(mode) {
  rows.forEach((row) => {
    const box = row.querySelector(".tsel");
    if (mode === "all") box.checked = true;
    else if (mode === "none") box.checked = false;
    else box.checked = row.dataset.status !== "exists" && row.dataset.status !== "done";
  });
  updateDownloadButton();
}

function selectedIds() {
  return [...rows.entries()]
    .filter(([, row]) => row.querySelector(".tsel").checked)
    .map(([id]) => id);
}

function updateDownloadButton() {
  const n = selectedIds().length;
  const btn = $("download-btn");
  btn.disabled = n === 0;
  btn.textContent = n ? `Download ${n}` : "Download";
}

/* ------------------------------------------------------------------ job */

async function startDownload() {
  const ids = selectedIds();
  if (!ids.length || !playlist) return;

  $("download-btn").disabled = true;
  banner($("job-banner"), `Matching ${ids.length} tracks on YouTube Music…`);

  try {
    const data = await api("/api/jobs", {
      method: "POST",
      body: { playlist_id: playlist.playlist.id, track_ids: ids },
    });
    jobId = data.job_id;
    $("cancel-btn").classList.remove("hidden");
    listen(jobId);
  } catch (err) {
    banner($("job-banner"), err.message, "error");
    $("download-btn").disabled = false;
  }
}

function listen(id) {
  if (stream) stream.close();
  stream = new EventSource(`/api/jobs/${id}/events`);

  stream.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.event === "snapshot") {
      data.snapshot.tasks.forEach(applyTask);
    } else if (data.event === "task") {
      applyTask(data.task);
    } else if (data.event === "warning") {
      banner($("job-banner"), data.message, "error");
    } else if (data.event === "job") {
      finishJob(data);
    }
  };
  stream.onerror = () => { /* server closes the stream when the job ends */ };
}

const STATUS_TEXT = {
  pending: "queued",
  matching: "searching…",
  downloading: "downloading",
  needs_review: "needs review",
  done: "done",
  exists: "already downloaded",
  error: "failed",
  cancelled: "stopped",
};

function applyTask(task) {
  const row = rows.get(task.id);
  if (!row) return;

  row.dataset.status = task.status;
  row.querySelector(".tstatus").textContent = task.message || STATUS_TEXT[task.status] || task.status;
  row.querySelector(".tfill").style.width = `${Math.round(task.progress * 100)}%`;

  const matchEl = row.querySelector(".tmatch");
  if (isDirect) {
    matchEl.textContent = "";   // the track row already is the recording
    matchEl.removeAttribute("title");
  } else if (task.match) {
    const flags = task.match.flags.map((f) => `<span class="flag">${f}</span>`).join("");
    matchEl.innerHTML =
      `${flags}<span class="score">${task.match.score}</span> · ` +
      `${escapeHtml(task.match.title)} — ${escapeHtml(task.match.artist)}`;
    // The row truncates; the tooltip carries the whole thing, flags included,
    // since those are the part worth reading in full.
    const parts = [];
    if (task.match.flags.length) parts.push(task.match.flags.join(", "));
    parts.push(`score ${task.match.score}`);
    parts.push(`${task.match.title} — ${task.match.artist}`);
    if (task.match.duration_s) {
      const d = task.match.duration_s;
      parts.push(`${Math.floor(d / 60)}:${String(d % 60).padStart(2, "0")}`);
    }
    matchEl.title = parts.join("\n");
  } else {
    matchEl.textContent = "";
    matchEl.removeAttribute("title");
  }

  const fix = row.querySelector(".tfix");
  const fixable = task.candidates && task.candidates.length > 1 &&
    ["needs_review", "done", "error"].includes(task.status);
  fix.classList.toggle("hidden", !fixable);
  if (fixable) fix.onclick = () => togglePicker(row, task);
}

function togglePicker(row, task) {
  const open = row.querySelector(".picker");
  if (open) { open.remove(); return; }

  const picker = document.createElement("div");
  picker.className = "picker";
  task.candidates.forEach((cand) => {
    const item = document.createElement("div");
    item.className = "cand";

    const info = document.createElement("div");
    info.className = "cand-info";
    const title = document.createElement("div");
    title.className = "cand-title";
    title.textContent = cand.title;
    title.title = cand.title;
    const sub = document.createElement("div");
    sub.className = "cand-sub";
    const mins = `${Math.floor(cand.duration_s / 60)}:${String(cand.duration_s % 60).padStart(2, "0")}`;
    sub.textContent = `${cand.artist} · ${mins} · score ${cand.score}` +
      (cand.flags.length ? ` · ${cand.flags.join(", ")}` : "");
    sub.title = sub.textContent;
    info.append(title, sub);

    const actions = document.createElement("div");
    const preview = document.createElement("a");
    preview.href = cand.url;
    preview.target = "_blank";
    preview.rel = "noreferrer";
    preview.textContent = "preview";
    preview.style.marginRight = "10px";
    preview.style.fontSize = "12px";
    const use = document.createElement("button");
    use.className = "ghost tiny";
    use.textContent = "Use this";
    use.onclick = async () => {
      use.disabled = true;
      picker.remove();
      // Show something immediately rather than waiting on the first event.
      row.dataset.status = "downloading";
      row.querySelector(".tstatus").textContent = "re-downloading…";
      row.querySelector(".tfill").style.width = "0%";
      if (!stream && jobId) listen(jobId);   // reconnect if the stream dropped
      try {
        await api(`/api/jobs/${jobId}/retry`, {
          method: "POST",
          body: { track_id: task.id, video_id: cand.video_id },
        });
      } catch (err) {
        row.dataset.status = "error";
        row.querySelector(".tstatus").textContent = err.message;
        banner($("job-banner"), err.message, "error");
      }
    };
    actions.append(preview, use);
    item.append(info, actions);
    picker.appendChild(item);
  });
  row.appendChild(picker);
}

function finishJob(data) {
  const s = data.summary || {};
  const bits = [];
  if (s.done) bits.push(`${s.done} downloaded`);
  if (s.exists) bits.push(`${s.exists} already had`);
  if (s.needs_review) bits.push(`${s.needs_review} need review`);
  if (s.error) bits.push(`${s.error} failed`);

  const kind = s.error || s.needs_review ? "" : "ok";
  let text = bits.length ? bits.join(" · ") : "Nothing to do";
  if (data.m3u) text += " — .m3u8 written";
  banner($("job-banner"), `${text}. Saved to ${data.folder}`, kind);

  $("cancel-btn").classList.add("hidden");
  // The stream stays open: "Fix match" re-downloads after the job has
  // reported done, and those updates arrive on this same connection.
  updateDownloadButton();
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

/* ------------------------------------------------------------- bindings */

$("load-btn").onclick = () => loadFrom("spotify");
$("playlist-url").addEventListener("keydown", (e) => { if (e.key === "Enter") loadFrom("spotify"); });
$("yt-load-btn").onclick = () => loadFrom("youtube");
$("yt-url").addEventListener("keydown", (e) => { if (e.key === "Enter") loadFrom("youtube"); });
$("signin-btn").onclick = startLogin;
$("download-btn").onclick = startDownload;

$("cancel-btn").onclick = async () => {
  if (jobId) await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
};

document.querySelectorAll("[data-select]").forEach((btn) => {
  btn.onclick = () => selectTracks(btn.dataset.select);
});

$("tracks").addEventListener("change", (e) => {
  if (e.target.classList.contains("tsel")) updateDownloadButton();
});

$("save-creds").onclick = async () => {
  try {
    await api("/api/credentials", {
      method: "POST",
      body: { client_id: $("client-id").value, client_secret: $("client-secret").value },
    });
    await refreshStatus();
  } catch (err) {
    alert(err.message);
  }
};

document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.onclick = async () => {
    await navigator.clipboard.writeText($(btn.dataset.copy).textContent);
    btn.textContent = "copied";
    setTimeout(() => (btn.textContent = "copy"), 1200);
  };
});

$("folder-btn").onclick = () => api("/api/open-folder", { method: "POST" }).catch(() => {});

$("settings-btn").onclick = () => $("settings-panel").classList.remove("hidden");
$("set-cookies").addEventListener("change", () => saveSettings(true));

async function saveSettings(stayOpen) {
  let result;
  try {
    result = await api("/api/settings", {
      method: "POST",
      body: {
        output_dir: $("set-output").value,
        concurrency: Number($("set-jobs").value),
        match_threshold: Number($("set-threshold").value),
        skip_low_matches: $("set-skip").checked,
        enrich_youtube: $("set-enrich").checked,
        cookies_browser: $("set-cookies").value.split("|")[0],
        cookies_profile: $("set-cookies").value.split("|").slice(1).join("|"),
        sleep_between: Number($("set-sleep").value),
        audio_quality: $("set-quality").value,
      },
    });
    await refreshStatus();
  } catch (err) {
    alert(err.message);
    return;
  }

  // Whether the chosen browser can actually be read, reported straight away
  // rather than surfacing as a failed download later.
  const check = result && result.cookie_check;
  const el = $("cookie-check");
  if (check && check.configured) {
    banner(el, check.message, check.ok ? "ok" : "error");
    el.classList.add("small");
  } else {
    banner(el, "");
  }

  if (!stayOpen) $("settings-panel").classList.add("hidden");
}

$("settings-close").onclick = () => saveSettings(false);
$("set-threshold").addEventListener("input", (e) => { $("thr-value").textContent = e.target.value; });
$("set-sleep").addEventListener("input", (e) => { $("sleep-value").textContent = `${e.target.value}s`; });
$("settings-panel").addEventListener("click", (e) => {
  if (e.target.id === "settings-panel") $("settings-panel").classList.add("hidden");
});

$("logout-btn").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  await refreshStatus();
};

refreshStatus();

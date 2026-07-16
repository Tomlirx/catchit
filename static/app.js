const $ = (s, el) => (el || document).querySelector(s);
const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
const api = async (url, opts) => (await fetch(url, opts)).json();
const post = (url, body) => api(url, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const patch = (url, body) => api(url, {
  method: "PATCH", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const esc = s => (s || "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = n => (n || 0).toLocaleString();

let currentView = "discover";
let filters = { channel: "", sort: "score", pub: "" };
let channels = [];
let tasksWereRunning = false;
let toastTimer = null;

// ---------- 通用 ----------

function toast(msg, ms) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), ms || 3000);
}

function channelName(id) {
  const c = channels.find(c => c.id === id);
  return c ? c.name : "";
}

function channelOptions(selected) {
  return channels.map(c =>
    `<option value="${c.id}" ${String(selected) === String(c.id) ? "selected" : ""}>${esc(c.name)}</option>`).join("");
}

// ---------- 状态轮询 ----------

async function poll() {
  try {
    const s = await api("/api/status");
    $("#cnt-discover").textContent = s.counts.discover;
    $("#cnt-library").textContent = s.counts.library;
    $("#cnt-failed").textContent = s.counts.failed;

    const li = $("#login-indicator");
    li.textContent = (s.login.logged_in ? "🟢 " : "🔴 ") + s.login.detail;
    li.className = "login-indicator " + (s.login.logged_in ? "ok" : "bad");

    const running = Object.entries(s.tasks).filter(([, t]) => t.running);
    const bar = $("#task-bar");
    if (running.length) {
      const msgs = running.map(([name, t]) => {
        let m = t.message || name;
        if (t.progress && t.progress.found !== undefined) m += `（已发现 ${t.progress.found}）`;
        if (t.progress && t.progress.left !== undefined && t.progress.left > 0)
          m += `（队列剩余 ${t.progress.left}）`;
        return m;
      });
      $("#task-msg").textContent = msgs.join(" ｜ ");
      bar.classList.remove("hidden", "error");
      $("#btn-cancel").dataset.task = running[0][0];
      tasksWereRunning = true;
    } else {
      if (tasksWereRunning) {
        tasksWereRunning = false;
        const errs = Object.values(s.tasks).map(t => t.error).filter(Boolean);
        const msgs = Object.values(s.tasks).map(t => t.message).filter(Boolean);
        toast(errs.length ? errs.join("；") : (msgs[msgs.length - 1] || "任务完成"), 5000);
        // 任务结束刷新当前视图；但用户正在输入时不打断（避免清掉未保存的文案）
        const ae = document.activeElement;
        if (!ae || (ae.tagName !== "TEXTAREA" && ae.tagName !== "INPUT")) render();
      }
      bar.classList.add("hidden");
    }
  } catch (e) { /* 服务未就绪时忽略 */ }
}

// ---------- 卡片渲染 ----------

function statsHtml(v) {
  const plays = v.plays ? `<span>▶️ ${fmt(v.plays)}</span>` : "";
  return `<div class="meta"><span>❤️ ${fmt(v.likes)}</span>
    <span>💬 ${fmt(v.comments)}</span>${plays}</div>
    <div class="meta small">
      <span>@${esc(v.author)}</span><span>${v.taken_at_str || ""}</span>
      ${v.channel_id ? `<span>📁 ${esc(channelName(v.channel_id))}</span>` : ""}
      <a href="${v.url}" target="_blank" rel="noopener">原帖 ↗</a>
    </div>`;
}

function thumbHtml(v, withCheckbox) {
  const chk = withCheckbox ? `<input type="checkbox" class="chk" value="${v.code}">` : "";
  const badge = {
    queued: `<span class="status-badge">排队中</span>`,
    downloading: `<span class="status-badge">下载中…</span>`,
    failed: `<span class="status-badge failed">下载失败</span>`,
  }[v.dl_status] || "";
  const img = v.thumb
    ? `<img class="thumb" src="${v.thumb}" loading="lazy" alt="">`
    : `<div class="thumb-ph">🎞️</div>`;
  return `<label class="thumb-wrap">${chk}${img}${badge}</label>`;
}

function discoverCard(v) {
  return `<div class="card" data-code="${v.code}">
    ${thumbHtml(v, true)}
    <div class="info">
      <div class="title" title="${esc(v.caption)}">${esc(v.title)}</div>
      <div class="desc">${esc((v.caption || "").slice(0, 160))}</div>
      ${statsHtml(v)}
      <div class="card-actions">
        <button class="ghost act-ignore">🙈 忽略</button>
      </div>
    </div>
  </div>`;
}

function libraryCard(v) {
  const pubTags = `${v.pub_xhs ? '<span class="pub-tag xhs">已发小红书</span>' : ""}
                   ${v.pub_douyin ? '<span class="pub-tag dy">已发抖音</span>' : ""}`;
  return `<div class="card" data-code="${v.code}">
    <div class="thumb-wrap">
      <video class="thumb" src="/media/${encodeURI(v.file_path)}" poster="${v.thumb || ""}"
             controls preload="none"></video>
      <div class="pub-tags">${pubTags}</div>
    </div>
    <div class="info">
      <div class="title" title="${esc(v.caption)}">${esc(v.title)}</div>
      ${statsHtml(v)}
      <textarea class="notes" placeholder="写你的小红书/抖音文案（自动保存）…">${esc(v.notes)}</textarea>
      <div class="card-actions">
        <button class="ghost act-pub-xhs ${v.pub_xhs ? "on" : ""}">📕 小红书</button>
        <button class="ghost act-pub-dy ${v.pub_douyin ? "on" : ""}">🎵 抖音</button>
        <button class="ghost act-copy">📋 复制出处</button>
        <button class="ghost act-copy-caption">📄 复制原文案</button>
      </div>
    </div>
  </div>`;
}

function failedCard(v) {
  return `<div class="card" data-code="${v.code}">
    ${thumbHtml(v, false)}
    <div class="info">
      <div class="title" title="${esc(v.caption)}">${esc(v.title)}</div>
      <div class="error-text">${esc(v.error)}</div>
      ${statsHtml(v)}
      <div class="card-actions">
        <button class="ghost act-retry">🔄 重试</button>
        <button class="ghost act-savefrom">🌐 savefrom 下载</button>
        <button class="ghost act-ignore">🙈 忽略</button>
      </div>
    </div>
  </div>`;
}

// ---------- 视图 ----------

async function renderVideos(view) {
  const q = new URLSearchParams({ view, sort: filters.sort });
  if (filters.channel) q.set("channel", filters.channel);
  if (filters.pub && view === "library") q.set("pub", filters.pub);
  const videos = await api("/api/videos?" + q);

  const toolbarParts = [
    `<select id="f-channel"><option value="">全部频道</option>${channelOptions(filters.channel)}</select>`,
    `<select id="f-sort">
       <option value="score" ${filters.sort === "score" ? "selected" : ""}>按热度</option>
       <option value="velocity" ${filters.sort === "velocity" ? "selected" : ""}>按日增热度 🔥</option>
       <option value="likes" ${filters.sort === "likes" ? "selected" : ""}>按点赞</option>
       <option value="time" ${filters.sort === "time" ? "selected" : ""}>按发布时间</option>
       <option value="added" ${filters.sort === "added" ? "selected" : ""}>按入库时间</option>
     </select>`,
  ];
  if (view === "library") {
    toolbarParts.push(`<select id="f-pub">
      <option value="">全部状态</option>
      <option value="pending" ${filters.pub === "pending" ? "selected" : ""}>待发</option>
      <option value="published" ${filters.pub === "published" ? "selected" : ""}>两端已发</option>
    </select>`);
  }
  if (view === "discover") {
    toolbarParts.push(`<label><input type="checkbox" id="chk-all"> 全选</label>
      <span class="spacer"></span>
      <button id="btn-dl-selected" class="primary" disabled>⬇️ 下载所选（<span id="sel-count">0</span>）</button>`);
  }

  const cardFn = { discover: discoverCard, library: libraryCard, failed: failedCard }[view];
  const emptyMsg = {
    discover: "没有待处理的视频。点「抓取热门」或在上方粘贴 Instagram 链接。",
    library: "素材库是空的。在「发现」页勾选视频下载，或直接粘贴链接。",
    failed: "没有下载失败的视频 🎉",
  }[view];

  $("#view").innerHTML = `
    <div class="view-toolbar">${toolbarParts.join("")}</div>
    ${videos.length
      ? `<div class="grid">${videos.map(cardFn).join("")}</div>`
      : `<div class="empty">${emptyMsg}</div>`}`;

  $("#f-channel").onchange = e => { filters.channel = e.target.value; render(); };
  $("#f-sort").onchange = e => { filters.sort = e.target.value; render(); };
  if ($("#f-pub")) $("#f-pub").onchange = e => { filters.pub = e.target.value; render(); };
  if (view === "discover") bindDiscover();
  bindCards(videos);
}

function bindDiscover() {
  const update = () => {
    const n = $$(".chk:checked").length;
    $("#sel-count").textContent = n;
    $("#btn-dl-selected").disabled = n === 0;
    $$(".card").forEach(c => c.classList.toggle("selected", !!$(".chk:checked", c)));
  };
  $("#view").addEventListener("change", e => {
    if (e.target.classList.contains("chk")) update();
  });
  $("#chk-all").onchange = e => {
    $$(".chk").forEach(c => (c.checked = e.target.checked));
    update();
  };
  $("#btn-dl-selected").onclick = async () => {
    const codes = $$(".chk:checked").map(c => c.value);
    const r = await post("/api/download", { codes });
    if (!r.ok) return toast(r.error);
    toast(`已加入下载队列（${r.queued} 条）`);
    render();
  };
}

function bindCards(videos) {
  const byCode = Object.fromEntries(videos.map(v => [v.code, v]));
  $$(".card").forEach(card => {
    const code = card.dataset.code;
    const v = byCode[code];
    const on = (sel, fn) => { const el = $(sel, card); if (el) el.onclick = fn; };

    on(".act-ignore", async () => {
      await patch("/api/video/" + code, { ignored: 1 });
      card.remove();
      toast("已忽略，不会再出现");
    });
    on(".act-retry", async () => {
      await post("/api/download", { codes: [code] });
      toast("已重新加入下载队列");
      render();
    });
    on(".act-savefrom", async () => {
      const r = await api(`/api/video/${code}/savefrom`);
      if (r.ok) window.open(r.link, "_blank");
    });
    on(".act-pub-xhs", async e => {
      const val = v.pub_xhs ? 0 : 1;
      await patch("/api/video/" + code, { pub_xhs: val });
      v.pub_xhs = val;
      e.target.classList.toggle("on", !!val);
      toast(val ? "已标记：发过小红书" : "已取消小红书标记");
    });
    on(".act-pub-dy", async e => {
      const val = v.pub_douyin ? 0 : 1;
      await patch("/api/video/" + code, { pub_douyin: val });
      v.pub_douyin = val;
      e.target.classList.toggle("on", !!val);
      toast(val ? "已标记：发过抖音" : "已取消抖音标记");
    });
    on(".act-copy", () => {
      navigator.clipboard.writeText(`视频来源：Instagram @${v.author}\n原帖：${v.url}`);
      toast("出处已复制，发布时请注明原作者");
    });
    on(".act-copy-caption", () => {
      navigator.clipboard.writeText(v.caption || "");
      toast("原文案已复制");
    });

    const notes = $(".notes", card);
    if (notes) {
      let t = null;
      notes.oninput = () => {
        clearTimeout(t);
        t = setTimeout(async () => {
          await patch("/api/video/" + code, { notes: notes.value });
          toast("文案已保存", 1200);
        }, 800);
      };
    }
  });
}

// ---------- 设置视图 ----------

async function renderSettings() {
  const [settings, chs, status] = await Promise.all([
    api("/api/settings"), api("/api/channels"), api("/api/status")]);
  channels = chs;

  const channelRows = chs.map(c => `
    <div class="channel-block" data-id="${c.id}">
      <div class="channel-row">
        <input class="ch-name" value="${esc(c.name)}">
        <input class="ch-tags" value="${esc(c.hashtags.join(", "))}" placeholder="标签，逗号分隔">
        <button class="ghost ch-save">保存</button>
        <button class="ghost ch-del">删除</button>
      </div>
      <div class="channel-row">
        <input class="ch-accounts" value="${esc((c.accounts || []).join(", "))}"
               placeholder="👤 博主用户名，逗号分隔（可选），如：natgeowild, bbcearth——直接抓他们的近期视频">
      </div>
    </div>`).join("");

  $("#view").innerHTML = `<div class="settings">
    <section>
      <h2>Instagram 登录</h2>
      <div class="meta"><span>${status.login.logged_in ? "🟢" : "🔴"} ${esc(status.login.detail)}</span></div>
      <p class="hint">点击下方按钮会弹出浏览器窗口，请手动登录（工具不读取密码，只在本地保存登录会话）。</p>
      <br><button id="btn-login" class="primary">打开浏览器登录</button>
    </section>
    <section>
      <h2>主题频道</h2>
      ${channelRows}
      <div class="channel-row">
        <input class="ch-name" id="new-ch-name" placeholder="新频道名">
        <input class="ch-tags" id="new-ch-tags" placeholder="标签，逗号分隔，如：cats, dogs, pets">
        <button class="ghost" id="new-ch-add">＋ 添加</button>
      </div>
      <div class="channel-row">
        <input class="ch-accounts" id="new-ch-accounts"
               placeholder="👤 博主用户名，逗号分隔（可选），支持 @名字 或主页链接">
      </div>
      <p class="hint">抓取来源 = Reels 流 + Explore 页 + 每个标签的热门帖 + 每个博主的近期作品。
      标签用英文，4～10 个为宜；博主填知名账号的用户名（主页网址里的那段），他们的视频质量有保障。</p>
    </section>
    <section>
      <h2>抓取参数</h2>
      <div class="param-row">
        <label>最近 <input id="p-days" type="number" min="1" max="30" value="${settings.days}"> 天</label>
        <label>每次入库上限 <input id="p-top" type="number" min="5" max="200" value="${settings.top_n}"> 条</label>
        <label>每页滚动 <input id="p-scrolls" type="number" min="3" max="40" value="${settings.scrolls}"> 次</label>
        <label>热度门槛：点赞 ≥ <input id="p-minlikes" type="number" min="0" step="1000" value="${settings.min_likes}" style="width:90px"></label>
        <label><input id="p-follow" type="checkbox" ${settings.follow_feed ? "checked" : ""}> 扫描我的关注流</label>
        <button id="p-save" class="primary">保存参数</button>
      </div>
      <p class="hint">低于热度门槛的视频不入库，宁缺毋滥（点赞被作者隐藏、显示为 0 时，播放量达到门槛的 20 倍才算通过）。
      滚动次数越多采集越全，但耗时越长、账号风险略高。</p>
    </section>
    <section>
      <h2>版权提醒</h2>
      <p class="hint">下载的视频版权归原作者所有。转发到小红书/抖音前，建议取得授权或至少注明
      原作者与出处（素材库卡片有「复制出处」按钮）。请勿用于商业用途。</p>
    </section>
  </div>`;

  $("#btn-login").onclick = async () => {
    const r = await post("/api/login");
    toast(r.ok ? "浏览器已打开，请在窗口中登录" : r.error);
  };
  $$(".channel-block[data-id]").forEach(row => {
    const id = parseInt(row.dataset.id);
    $(".ch-save", row).onclick = async () => {
      const r = await post("/api/channels", {
        id, name: $(".ch-name", row).value,
        hashtags: $(".ch-tags", row).value.split(","),
        accounts: $(".ch-accounts", row).value.split(","),
      });
      toast(r.ok ? "频道已保存" : r.error);
      if (r.ok) loadChannels();
    };
    $(".ch-del", row).onclick = async () => {
      if (!confirm("删除该频道？（不影响已入库的视频）")) return;
      await fetch("/api/channels/" + id, { method: "DELETE" });
      render();
      loadChannels();
    };
  });
  $("#new-ch-add").onclick = async () => {
    const r = await post("/api/channels", {
      name: $("#new-ch-name").value,
      hashtags: $("#new-ch-tags").value.split(","),
      accounts: $("#new-ch-accounts").value.split(","),
    });
    toast(r.ok ? "频道已添加" : r.error);
    if (r.ok) { render(); loadChannels(); }
  };
  $("#p-save").onclick = async () => {
    await post("/api/settings", {
      days: $("#p-days").value, top_n: $("#p-top").value,
      scrolls: $("#p-scrolls").value, min_likes: $("#p-minlikes").value,
      follow_feed: $("#p-follow").checked ? 1 : 0,
    });
    toast("参数已保存");
  };
}

// ---------- 顶栏动作 ----------

async function loadChannels() {
  channels = await api("/api/channels");
  $("#scrape-channel").innerHTML = channelOptions();
}

function bindTopBar() {
  $$("#nav button").forEach(b => {
    b.onclick = () => {
      $$("#nav button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      currentView = b.dataset.view;
      render();
    };
  });

  const addUrls = async () => {
    const text = $("#url-input").value.trim();
    if (!text) return;
    const r = await post("/api/add_urls", { text });
    if (!r.ok) return toast(r.error);
    $("#url-input").value = "";
    toast(`识别到 ${r.count} 条链接，解析后自动下载入库`);
  };
  $("#btn-add-urls").onclick = addUrls;
  $("#url-input").addEventListener("keydown", e => { if (e.key === "Enter") addUrls(); });

  $("#btn-scrape").onclick = async () => {
    const r = await post("/api/scrape",
      { channel_id: parseInt($("#scrape-channel").value) || null });
    toast(r.ok ? "抓取已启动（如未登录请先到设置页登录）" : r.error);
  };

  $("#btn-cancel").onclick = async e => {
    await post("/api/cancel/" + e.target.dataset.task);
    toast("已请求取消…");
  };
}

// ---------- 入口 ----------

function render() {
  if (currentView === "settings") return renderSettings();
  return renderVideos(currentView);
}

(async function init() {
  bindTopBar();
  await loadChannels();
  await render();
  poll();
  setInterval(poll, 2000);
})();

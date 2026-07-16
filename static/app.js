const $ = (s, el) => (el || document).querySelector(s);
const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));

const statusEl = $("#status");
const btnScrape = $("#btn-scrape");
const btnDownload = $("#btn-download");
const chkAll = $("#chk-all");
let pollTimer = null;

function showStatus(msg, isError) {
  statusEl.textContent = msg;
  statusEl.classList.toggle("error", !!isError);
  statusEl.classList.remove("hidden");
}

function updateSelection() {
  const checked = $$(".chk:checked");
  $("#sel-count").textContent = checked.length;
  btnDownload.disabled = checked.length === 0;
  $$(".card").forEach(c =>
    c.classList.toggle("selected", !!$(".chk:checked", c)));
}

document.addEventListener("change", e => {
  if (e.target.classList.contains("chk")) updateSelection();
});

chkAll.addEventListener("change", () => {
  $$(".chk").forEach(c => (c.checked = chkAll.checked));
  updateSelection();
});

btnScrape.addEventListener("click", async () => {
  const r = await fetch("/api/scrape", { method: "POST" });
  const j = await r.json();
  if (!j.ok) return showStatus(j.error, true);
  btnScrape.disabled = true;
  startPolling("scrape");
});

btnDownload.addEventListener("click", async () => {
  const codes = $$(".chk:checked").map(c => c.value);
  const r = await fetch("/api/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ codes }),
  });
  const j = await r.json();
  if (!j.ok) return showStatus(j.error, true);
  btnDownload.disabled = true;
  showStatus("开始下载到 " + j.dir);
  startPolling("download");
});

function startPolling(kind) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const s = await (await fetch("/api/status")).json();
    if (kind === "scrape") {
      const sc = s.scrape;
      showStatus(sc.message + (sc.found ? `（已发现 ${sc.found} 个视频）` : ""),
                 !!sc.error);
      if (!sc.running) {
        clearInterval(pollTimer);
        btnScrape.disabled = false;
        if (sc.error) showStatus(sc.error, true);
        else location.reload(); // 重新渲染新结果
      }
    } else {
      const dl = s.download;
      let done = 0, failed = 0;
      for (const [code, item] of Object.entries(dl.items)) {
        const card = $(`.card[data-code="${code}"]`);
        if (!card) continue;
        const badge = $(".dl-badge", card);
        badge.classList.remove("hidden", "done", "failed");
        if (item.status === "downloading") badge.textContent = "下载中…";
        else if (item.status === "done") {
          badge.textContent = "✓ 已下载"; badge.classList.add("done"); done++;
        } else if (item.status === "failed") {
          failed++; badge.classList.add("failed");
          badge.innerHTML = item.savefrom
            ? `✗ 失败 <a href="${item.savefrom}" target="_blank">用 savefrom 下载</a>`
            : "✗ 失败";
          badge.title = item.detail || "";
        } else badge.textContent = "等待中…";
      }
      showStatus(`下载进度：完成 ${done} / 失败 ${failed} / 共 ${Object.keys(dl.items).length}`,
                 false);
      if (!dl.running) {
        clearInterval(pollTimer);
        updateSelection();
      }
    }
  }, 1500);
}

updateSelection();

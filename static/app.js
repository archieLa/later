"use strict";
const $ = (id) => document.getElementById(id);
let last = null;
let busy = false;
function message(text, error = false) { $("message").textContent = text; $("message").classList.toggle("error", error); }
async function api(path, method = "GET", body) {
  const response = await fetch(path, {method, headers: {"Content-Type": "application/json", "X-Later": "1"}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}
function renderLast() {
  $("last").hidden = !last;
  if (!last) return;
  $("last-description").textContent = last.description || new URL(last.url).hostname;
  $("last-url").textContent = last.url;
  $("reopen").href = last.url;
  $("star").textContent = last.starred ? "★ Unstar" : "☆ Star";
  $("star").setAttribute("aria-pressed", String(last.starred));
}
async function refresh() {
  const data = await api("/api/state");
  last = data.last; renderLast();
  $("counts").textContent = `${data.counts.unread} unread · ${data.counts.total} saved`;
  $("star-count").textContent = data.counts.starred;
}
async function show(view) {
  document.querySelectorAll(".view").forEach(el => { el.hidden = el.id !== view; });
  document.querySelectorAll("[data-view]").forEach(el => { if (el.dataset.view === view) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current"); });
  message("");
  if (view === "starred") { try { await loadStarred(); } catch (error) { message(error.message, true); } }
}
async function setStar(entry, value) {
  await api(`/api/items/${entry.id}/star`, "PATCH", {starred: value});
  await refresh();
}
async function loadStarred() {
  const data = await api("/api/starred");
  $("starred-list").replaceChildren();
  if (!data.items.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = "No favorites yet. Pick a link, then star a keeper."; $("starred-list").append(p); }
  for (const entry of data.items) {
    const card = document.createElement("article"); card.className = "saved-item";
    const title = document.createElement("h2"); title.textContent = entry.description || new URL(entry.url).hostname;
    const url = document.createElement("p"); url.className = "url"; url.textContent = entry.url;
    const actions = document.createElement("div"); actions.className = "actions";
    const link = document.createElement("a"); link.href = entry.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.className = "button"; link.textContent = "Open link ↗";
    const button = document.createElement("button"); button.textContent = "★ Unstar";
    button.addEventListener("click", async () => { button.disabled = true; try { await setStar(entry, false); await loadStarred(); } catch (error) { message(error.message, true); button.disabled = false; } });
    actions.append(link, button); card.append(title, url, actions); $("starred-list").append(card);
  }
}
async function pop(mode) {
  if (busy) return;
  busy = true;
  // Open during the click so browsers allow the tab; keep the original tab for starring.
  const tab = window.open("about:blank", "_blank");
  if (tab) tab.opener = null;
  $("pop").disabled = $("pop-starred").disabled = true;
  try {
    const data = await api("/api/pop", "POST", {mode});
    last = data.item; renderLast();
    if (tab) tab.location.replace(last.url);
    await show("read");
    message(tab ? "Picked! Your link is open in a new tab." : "Picked! Your browser blocked the new tab. Use Open again below.");
    await refresh();
  } catch (error) { if (tab) tab.close(); message(error.message, true); }
  finally { busy = false; $("pop").disabled = $("pop-starred").disabled = false; }
}
document.querySelectorAll("[data-view]").forEach(el => el.addEventListener("click", () => show(el.dataset.view)));
$("pop").addEventListener("click", () => pop($("mode").value));
$("pop-starred").addEventListener("click", () => pop("starred"));
$("star").addEventListener("click", async () => { if (!last) return; $("star").disabled = true; try { await setStar(last, !last.starred); } catch (error) { message(error.message, true); } finally { $("star").disabled = false; } });
$("add-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.target.querySelector("button"); button.disabled = true;
  try { await api("/api/items", "POST", {url: $("url").value, description: $("description").value}); event.target.reset(); await refresh(); message("Saved for later."); $("url").focus(); }
  catch (error) { message(error.message, true); } finally { button.disabled = false; }
});
$("restore-form").addEventListener("submit", async event => {
  event.preventDefault();
  const file = $("backup-file").files[0];
  if (!file) return;
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    if (file.size > 10 * 1024 * 1024) throw new Error("Backup files must be under 10 MB.");
    let backup;
    try { backup = JSON.parse(await file.text()); }
    catch { throw new Error("Choose a valid Later JSON backup file."); }
    if (!backup || backup.app !== "Later" || backup.version !== 1 || !Array.isArray(backup.items)) {
      throw new Error("Choose a supported Later backup (version 1).");
    }
    if (!window.confirm(`Restore ${backup.items.length} saved links from “${file.name}”? This replaces all current links, favorites, and read history.`)) return;
    const result = await api("/api/restore", "POST", backup);
    await refresh();
    if (!$("starred").hidden) await loadStarred();
    event.target.reset();
    message(`Backup restored. ${result.restored} saved links loaded.`);
    $("message").scrollIntoView({block: "center", behavior: "smooth"});
  } catch (error) { message(error.message, true); $("message").scrollIntoView({block: "center", behavior: "smooth"}); }
  finally { button.disabled = false; }
});
refresh().catch(error => message(error.message, true));

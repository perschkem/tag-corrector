"use strict";
/* tag-corrector – Oberfläche */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (n) => Number(n || 0).toLocaleString("de-DE");

async function api(path, body, method) {
  const opt = body !== undefined ? { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Fehler ${r.status}`);
  return data;
}

const STATUS = {
  mb_ok: "MusicBrainz ✓", mb_unsure: "unsicher", nomatch: "kein Treffer", offline: "offline",
  manual: "manuell", unknown: "unbekannt",
};
const STATUS_HELP = {
  mb_ok: "Sicher bei MusicBrainz gefunden",
  mb_unsure: "Treffer unsicher – bitte prüfen, standardmäßig abgewählt",
  nomatch: "Nicht bei MusicBrainz gefunden – Korrektur nur aus vorhandenen Tags & Ordnernamen",
  offline: "Ohne MusicBrainz – Korrektur aus vorhandenen Tags & Ordnernamen",
  manual: "Von dir manuell angepasst",
  unknown: "Album nicht bestimmbar – bitte manuell setzen",
};
const FLAGS = {
  struktur: "Struktur", tags: "Tags", jahr: "Jahr", kommentare: "Kommentare", tagmuell: "Tag-Müll",
  fremddateien: "Fremddateien", cover: "Cover", duplikat: "Duplikat", luecken: "Lücken",
};
const CATS = [
  { key: "tags", label: "Tags korrigieren", help: "Titel, Interpret, Album, Jahr, Nummern, Albuminterpret", flags: ["tags", "jahr"] },
  { key: "junk", label: "Kommentare & Tag-Müll löschen", help: "Kommentare, Encoder, URLs, ID3v1/APE-Reste", flags: ["kommentare", "tagmuell"] },
  { key: "structure", label: "Struktur & Dateinamen", help: "Interpret/Album/01 - Titel.ext, leere Ordner entfernen", flags: ["struktur"] },
  { key: "extras", label: "Fremddateien in Quarantäne", help: "Thumbs.db, .nfo, .url … werden verschoben, nicht gelöscht", flags: ["fremddateien"] },
  { key: "cover", label: "Cover laden & einbetten", help: "Großes Cover.jpg im Ordner, kleines Cover in den Dateien; zu große/kaputte eingebettete Bilder ersetzen", flags: ["cover"] },
  { key: "dups", label: "Duplikate in Quarantäne", help: "Schlechtere Kopie desselben Liedes wird verschoben", flags: ["duplikat"] },
];

const JOB_TITLES = { scan: "Sammlung wird geprüft", apply: "Änderungen werden angewendet", undo: "Lauf wird rückgängig gemacht" };

function loadPref(key, fallback) {
  try { const v = localStorage.getItem("tc-" + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function savePref(key, value) {
  try { localStorage.setItem("tc-" + key, JSON.stringify(value)); } catch { /* egal */ }
}

const S = {
  plan: null,
  details: {},           // album id -> volle Albumdaten
  sel: {},               // album id -> {on, xt:Set, xi:Set}
  miscOn: new Set(),
  cats: { ...Object.fromEntries(CATS.map((c) => [c.key, true])), touch: loadPref("touch", false) },
  openArtists: new Set(),
  openAlbums: new Set(),
  openTracks: new Set(),
  openPanels: {},        // album id -> "rematch" | "edit"
  filter: "changes",
  q: "",
  showUnchanged: false,
  poll: null,
  lastJobFinished: null,
};
const albumIndex = {};   // id -> summary

/* ======================================================================
   Einstellungen
   ====================================================================== */
async function loadSettings() {
  const st = await api("/api/settings");
  for (const [k, v] of Object.entries(st)) {
    const el = document.getElementById(k);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? "";
  }
}

function readSettings() {
  const out = {};
  for (const el of $$("#setup-form [name]")) out[el.name] = el.type === "checkbox" ? el.checked : el.value;
  return out;
}

let lsTimer;
$("#root").addEventListener("input", (e) => {
  clearTimeout(lsTimer);
  lsTimer = setTimeout(async () => {
    try {
      const list = await api("/api/ls?path=" + encodeURIComponent(e.target.value));
      $("#root-list").innerHTML = list.map((p) => `<option value="${esc(p)}/">`).join("");
    } catch { /* egal */ }
  }, 200);
});

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#setup-error").hidden = true;
  try {
    await api("/api/scan", readSettings());
    S.details = {};
    startPolling();
  } catch (err) {
    $("#setup-error").textContent = err.message;
    $("#setup-error").hidden = false;
  }
});

/* ======================================================================
   Fortschritt
   ====================================================================== */
function startPolling() {
  // Anzeige zurücksetzen, Einstellungen einklappen, Fortschritt in den Blick holen
  $("#prog-title").textContent = "Wird vorbereitet…";
  $("#prog-step").textContent = "";
  $("#prog-name").textContent = "…";
  $("#prog-dir").textContent = "";
  $("#prog-count").textContent = "0";
  $("#prog-unit").textContent = "";
  $("#prog-pct").textContent = "";
  $("#prog-eta").textContent = "";
  $("#prog-fill").style.width = "0%";
  $("#prog-turtle").style.left = "0";
  $("#setup-details").open = false;
  $("#review").hidden = true;   // alte Vorschau ist während des Laufs ungültig
  $("#progress").hidden = false;
  $("#progress").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#result").hidden = true;
  $("#btn-scan").disabled = true;
  $("#btn-apply").disabled = true;
  clearInterval(S.poll);
  S.poll = setInterval(pollStatus, 700);
  pollStatus();
}

function fmtDur(sec) {
  if (!isFinite(sec) || sec < 0) return "";
  if (sec < 60) return `${Math.round(sec)} s`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

async function pollStatus() {
  let j;
  try { j = await api("/api/status"); } catch { return; }
  if (!j.kind) { stopPolling(); return; }
  if (j.running) {
    $("#progress").hidden = false;
    $("#prog-title").textContent = JOB_TITLES[j.kind] || "Arbeite…";
    $("#prog-step").textContent = j.step ? `Schritt ${j.step} von ${j.steps}: ${j.phase}` : "";
    const pct = j.total ? Math.min(100, (j.done / j.total) * 100) : 0;
    if (j.total) {
      $("#prog-count").textContent = `${fmtN(j.done)} / ${fmtN(j.total)}`;
      $("#prog-unit").textContent = "verarbeitet";
      $("#prog-pct").textContent = `· ${pct.toFixed(pct < 10 ? 1 : 0).replace(".", ",")} %`;
    } else {
      $("#prog-count").textContent = fmtN(j.done);
      $("#prog-unit").textContent = j.phase === "Dateien suchen" ? "Lieder gefunden" : "";
      $("#prog-pct").textContent = "";
    }
    $("#prog-fill").style.width = pct + "%";
    $("#prog-bar").setAttribute("aria-valuenow", Math.round(pct));
    $("#prog-turtle").style.left = pct + "%";
    $("#prog-bar").classList.toggle("indeterminate", !j.total);
    const label = j.label || "Aktuelles Lied";
    $("#prog-label").textContent = label;
    const msg = j.msg || "";
    if (label === "Aktuelles Lied" || label === "Aktuelle Datei") {
      const [dir, name] = splitPath(msg);
      $("#prog-name").textContent = name || "…";
      $("#prog-dir").textContent = dir.replace(/\/$/, "");
    } else {
      $("#prog-name").textContent = msg || "…";
      $("#prog-dir").textContent = "";
    }
    const el = (j.now - j.phase_started);
    let eta = "";
    if (j.total && j.done > 3 && el > 3) {
      const rest = (j.total - j.done) * (el / j.done);
      eta = `noch ca. ${fmtDur(rest)}`;
    }
    $("#prog-eta").textContent = eta;
    $("#btn-skip-mb").hidden = !(j.kind === "scan" && (j.phase || "").startsWith("MusicBrainz"));
    return;
  }
  // fertig
  stopPolling();
  if (S.lastJobFinished === j.finished) return;
  S.lastJobFinished = j.finished;
  if (j.kind === "scan") {
    if (j.error) showError("Prüfung abgebrochen", j.error);
    else { await loadPlan(true); $("#setup-details").open = false; }
  } else if (j.kind === "apply" || j.kind === "undo") {
    showResult(j);
    await loadPlan(false);
    loadUndo();
  }
}

function stopPolling() {
  clearInterval(S.poll);
  S.poll = null;
  $("#progress").hidden = true;
  $("#btn-scan").disabled = false;
  $("#btn-apply").disabled = false;
}

$("#btn-skip-mb").addEventListener("click", () => api("/api/stop", { mode: "skip_mb" }));
$("#btn-abort").addEventListener("click", async () => {
  if (await ask("Vorgang abbrechen?", "<p>Bereits ausgeführte Änderungen bleiben bestehen und können über den Verlauf rückgängig gemacht werden.</p>", "Abbrechen")) {
    api("/api/stop", { mode: "abort" });
  }
});

function showError(title, msg) {
  const r = $("#result");
  r.hidden = false;
  r.innerHTML = `<h2>${esc(title)}</h2><p class="error">${esc(msg)}</p>`;
}

function showResult(j) {
  const r = $("#result");
  r.hidden = false;
  if (j.error) { showError("Vorgang abgebrochen", j.error); return; }
  const res = j.result || {};
  if (j.kind === "undo") {
    r.innerHTML = `<h2>Rückgängig gemacht</h2>
      <p>${fmtN(res.ops)} Schritte wurden zurückgedreht. Bitte die Sammlung neu prüfen.</p>
      ${res.errors?.length ? `<div class="errors">${res.errors.map(esc).join("<br>")}</div>` : ""}
      <div class="actions"><button class="btn primary" data-rescan>Neu prüfen</button></div>`;
    return;
  }
  const d = res.done || {};
  r.innerHTML = `<h2>Fertig</h2>
    <div class="result-grid">
      <div class="stat"><b>${fmtN(d.tags)}</b><span>Dateien neu getaggt</span></div>
      <div class="stat"><b>${fmtN(d.moved)}</b><span>verschoben / umbenannt</span></div>
      <div class="stat"><b>${fmtN(d.quarantined)}</b><span>in Quarantäne</span></div>
      <div class="stat"><b>${fmtN(d.covers)}</b><span>Cover gespeichert</span></div>
      <div class="stat"><b>${fmtN(d.rmdirs)}</b><span>leere Ordner entfernt</span></div>
      ${d.playlists ? `<div class="stat"><b>${fmtN(d.playlists)}</b><span>Playlisten angepasst</span></div>` : ""}
      ${d.touched ? `<div class="stat"><b>${fmtN(d.touched)}</b><span>Änderungsdatum auf jetzt</span></div>` : ""}
    </div>
    ${res.errors?.length ? `<h3>${fmtN(res.errors.length)} Probleme</h3><div class="errors">${res.errors.map(esc).join("<br>")}</div>` : `<p class="muted">Keine Fehler.</p>`}
    <div class="actions">
      <button class="btn primary" data-rescan>Neu prüfen</button>
      <button class="btn ghost danger" data-undo="${esc(res.undo)}">Diesen Lauf rückgängig machen</button>
    </div>`;
}

$("#result").addEventListener("click", (e) => {
  if (e.target.closest("[data-rescan]")) $("#setup-form").requestSubmit();
  const u = e.target.closest("[data-undo]");
  if (u) runUndo(u.dataset.undo);
});

/* ======================================================================
   Plan laden & Auswahl
   ====================================================================== */
async function loadPlan(resetSelection) {
  const p = await api("/api/plan");
  if (p.empty) return;
  S.plan = p;
  if (resetSelection) {
    S.sel = {}; S.details = {}; S.openAlbums.clear(); S.openTracks.clear(); S.openPanels = {};
    S.miscOn = new Set(p.misc.map((m) => m.id));
  } else {
    S.details = {};
  }
  for (const k of Object.keys(albumIndex)) delete albumIndex[k];
  for (const ar of p.artists) for (const a of ar.albums) {
    albumIndex[a.id] = a;
    if (!S.sel[a.id]) S.sel[a.id] = { on: a.default_on && a.has_changes, xt: new Set(), xi: new Set() };
  }
  $("#review").hidden = false;
  $("#plan-root").textContent = p.root;
  $("#stale-note").hidden = !p.stale;
  $("#btn-apply").disabled = p.stale;
  $("#setup-summary").textContent = p.root;
  renderStats();
  renderCats();
  renderTree();
  renderMisc();
  updateSelSummary();
}

function albumState(id) {
  const s = S.sel[id];
  if (!s || !s.on) return "off";
  return s.xt.size || s.xi.size ? "partial" : "on";
}

function setAlbum(id, on) {
  S.sel[id] = { on, xt: new Set(), xi: new Set() };
}

function renderStats() {
  const st = S.plan.stats;
  const unsure = (st.status.mb_unsure || 0) + (st.status.unknown || 0);
  const nomatch = (st.status.nomatch || 0);
  $("#stats").innerHTML = `
    <div class="stat"><b>${fmtN(st.files)}</b><span>Lieder geprüft</span></div>
    <div class="stat"><b>${fmtN(st.albums)}</b><span>Alben · ${fmtN(st.artists)} Interpreten</span></div>
    <div class="stat"><b>${fmtN(st.changed_tracks)}</b><span>Lieder mit Änderungen</span></div>
    <div class="stat"><b>${fmtN(st.status.mb_ok || 0)}</b><span>Alben sicher erkannt</span></div>
    <div class="stat"><b>${fmtN(unsure)}</b><span>unsicher, bitte prüfen</span></div>
    <div class="stat"><b>${fmtN(nomatch)}</b><span>ohne MusicBrainz-Treffer</span></div>
    ${st.broken?.length ? `<details class="note warn broken"><summary><b>${fmtN(st.broken.length)} Dateien sind beschädigt oder nicht lesbar</b> – sie werden nicht verändert</summary>
      <ul>${st.broken.map((b) => `<li><span class="mono">${esc(b.rel)}</span><br><small>${esc(b.error)}</small></li>`).join("")}</ul></details>` : ""}`;
}

function renderCats() {
  const fl = S.plan.stats.flags;
  $("#cats").innerHTML = CATS.map((c) => {
    const n = c.flags.reduce((a, f) => Math.max(a, fl[f] || 0), 0);
    return `<label class="cat"><input type="checkbox" data-cat="${c.key}" ${S.cats[c.key] ? "checked" : ""}>
      <span>${esc(c.label)} <span class="muted num">· ${fmtN(n)} ${n === 1 ? "Album" : "Alben"}</span><small>${esc(c.help)}</small></span></label>`;
  }).join("") + `<label class="cat touch-opt"><input type="checkbox" data-cat="touch" id="opt-touch" ${S.cats.touch ? "checked" : ""}>
      <span>Änderungsdatum auf jetzt setzen<small>Für jede geänderte oder umbenannte Datei. Ohne Haken bleibt das ursprüngliche Änderungsdatum erhalten.</small></span></label>`;
}
$("#cats").addEventListener("change", (e) => {
  const k = e.target.dataset.cat;
  if (k) {
    S.cats[k] = e.target.checked;
    if (k === "touch") savePref("touch", e.target.checked);
    updateSelSummary();
  }
});

/* ======================================================================
   Baum
   ====================================================================== */
function albumVisible(a) {
  if (S.q) {
    const q = S.q.toLowerCase();
    if (!(a.album.toLowerCase().includes(q) || a.albumartist.toLowerCase().includes(q) ||
          a.source_dirs.some((d) => d.toLowerCase().includes(q)))) return false;
  }
  switch (S.filter) {
    case "changes": return a.has_changes;
    case "unsure": return a.status === "mb_unsure" || a.status === "unknown";
    case "offline": return ["nomatch", "offline", "unknown"].includes(a.status);
    default: return true;
  }
}

function artistAgg(albums) {
  let on = 0, off = 0;
  for (const a of albums) {
    const st = albumState(a.id);
    if (st === "on") on++; else if (st === "off") off++; else { on++; off++; }
  }
  return on && off ? "partial" : on ? "on" : "off";
}

function renderTree() {
  const tree = $("#tree");
  const parts = [];
  let shown = 0;
  for (const ar of S.plan.artists) {
    const albums = ar.albums.filter(albumVisible);
    if (!albums.length) continue;
    shown++;
    parts.push(artistHTML(ar.name, albums));
  }
  const hidden = S.plan.stats.albums - S.plan.stats.changed_albums;
  tree.innerHTML = parts.join("") || `<div class="panel"><p>Keine Alben für diesen Filter.</p>${
    S.filter === "changes" ? `<p class="muted">Alles ordentlich. ${fmtN(hidden)} Alben sind bereits korrekt.</p>` : ""}</div>`;
  for (const cb of $$(".cb-artist, .cb-album", tree)) cb.indeterminate = cb.dataset.state === "partial";
  renderSelCats();
}

function artistHTML(name, albums) {
  const open = S.openArtists.has(name);
  const st = artistAgg(albums);
  const nChanged = albums.reduce((s, a) => s + a.n_changed, 0);
  const flags = [...new Set(albums.flatMap((a) => a.flags))];
  return `<div class="artist ${open ? "open" : ""}" data-artist="${esc(name)}">
    <div class="row artist-row">
      <button class="twisty" aria-label="aufklappen">▸</button>
      <input type="checkbox" class="cb-artist" data-state="${st}" ${st !== "off" ? "checked" : ""} aria-label="Interpret auswählen">
      <span class="artist-name">${esc(name)}</span>
      <span class="chips">${flags.map(chip).join("")}</span>
      <span class="meta num">${albums.length} ${albums.length === 1 ? "Album" : "Alben"} · ${fmtN(nChanged)} Lieder</span>
    </div>
    ${open ? `<div class="albums">${albums.map(albumHTML).join("")}</div>` : ""}
  </div>`;
}

const chip = (f) => `<span class="chip c-${f}">${esc(FLAGS[f] || f)}</span>`;

function albumHTML(a) {
  const open = S.openAlbums.has(a.id);
  const st = albumState(a.id);
  const d = S.details[a.id];
  return `<div class="album st-${a.status} ${open ? "open" : ""}" data-album="${a.id}">
    <div class="row album-row">
      <button class="twisty" aria-label="aufklappen">▸</button>
      <input type="checkbox" class="cb-album" data-state="${st}" ${st !== "off" ? "checked" : ""} aria-label="Album auswählen">
      <span class="pill ${a.status}" title="${esc(STATUS_HELP[a.status])}">${esc(STATUS[a.status])}</span>
      <span class="album-title">${esc(a.album)} <span class="year">${a.year ? "(" + esc(a.year) + ")" : "(Jahr ?)"}</span></span>
      <span class="chips">${a.flags.map(chip).join("")}</span>
      <span class="meta num">${fmtN(a.n_changed)} / ${fmtN(a.n_tracks)} Lieder</span>
    </div>
    ${open ? `<div class="album-detail">${d ? albumDetailHTML(d) : `<p class="muted">Lade…</p>`}</div>` : ""}
  </div>`;
}

function albumDetailHTML(a) {
  const s = S.sel[a.id];
  const moved = !(a.source_dirs.length === 1 && a.source_dirs[0] === a.dir);
  // Bei einem einzigen Quellordner die Unterschiede zum Ziel markieren
  const single = a.source_dirs.length === 1 && a.source_dirs[0] !== ".";
  const [srcMarked, dirMarked] = moved && single ? diffMarks(a.source_dirs[0], a.dir) : [null, esc(a.dir)];
  const srcs = a.source_dirs.map((x) => x === "." ? "(Wurzelordner)" : x).map(esc);
  if (srcMarked !== null) srcs[0] = srcMarked;
  const dirHint = single && caseOnly(a.source_dirs[0], a.dir) ? ` <span class="hint">nur Groß-/Kleinschreibung</span>` : "";
  const rel = a.release;
  const conf = Math.round((a.confidence || 0) * 100);
  const meterCls = conf >= 80 ? "" : conf >= 60 ? "lowish" : "low";
  const panel = S.openPanels[a.id];
  const msgs = [...a.warnings.map((w) => `<li class="w">${esc(w)}</li>`), ...a.info.map((i) => `<li class="i">${esc(i)}</li>`)];
  const visTracks = a.tracks.filter((t) => S.showUnchanged || t.has_changes);
  const hiddenN = a.tracks.length - visTracks.length;

  return `
  <dl class="paths">
    <dt>Ordner</dt><dd class="${moved ? "old" : ""}">${srcs.join("<br>")}</dd>
    ${moved ? `<dt>wird zu</dt><dd class="new"><b>${dirMarked}</b>${dirHint}</dd>` : ""}
    <dt>Album</dt><dd>${esc(a.target.albumartist)} · ${esc(a.target.album)} · ${esc(a.target.year || "Jahr unbekannt")}${a.target.compilation ? " · Sampler" : ""}${a.target.disctotal > 1 ? ` · ${a.target.disctotal} CDs` : ""}</dd>
  </dl>
  ${rel ? `<div class="release">
      <span class="meter ${meterCls}" title="Übereinstimmung ${conf}%"><i style="width:${conf}%"></i></span>
      <span class="num">${conf}%</span>
      <span>${esc(rel.artist)} – ${esc(rel.title)} <span class="muted">${esc([rel.date, rel.country, rel.formats, rel.tracks + " Tracks"].filter(Boolean).join(" · "))}</span></span>
      <a href="https://musicbrainz.org/release/${esc(rel.id)}" target="_blank" rel="noopener">auf MusicBrainz ansehen</a>
    </div>` : ""}
  <div class="album-tools">
    <button class="btn small ${panel === "rematch" ? "primary" : ""}" data-act="panel-rematch">Anderes Release wählen</button>
    <button class="btn small ${panel === "edit" ? "primary" : ""}" data-act="panel-edit">Interpret / Album / Jahr ändern</button>
    ${a.status !== "offline" && a.status !== "nomatch" ? `<button class="btn small ghost" data-act="offline">Ohne MusicBrainz</button>` : ""}
    ${Object.keys(a.overrides || {}).length ? `<button class="btn small ghost" data-act="reset">Zurücksetzen</button>` : ""}
  </div>
  ${panel === "rematch" ? rematchHTML(a) : ""}
  ${panel === "edit" ? editHTML(a) : ""}
  ${msgs.length ? `<ul class="msgs">${msgs.join("")}</ul>` : ""}
  ${a.items.length ? `<div class="items">${a.items.map((it) => `
      <label class="item"><input type="checkbox" class="cb-item" data-item="${esc(it.id)}" ${s.on && !s.xi.has(it.id) ? "checked" : ""}>
      <span>${esc(it.label)}</span></label>`).join("")}</div>` : ""}
  <div class="tracks-wrap"><table class="tracks"><tbody>
    ${visTracks.map((t) => trackHTML(a, t)).join("")}
  </tbody></table></div>
  ${hiddenN ? `<p class="muted">${hiddenN} Lieder ohne Änderungen ausgeblendet.</p>` : ""}`;
}

function rematchHTML(a) {
  const cur = a.release?.id;
  return `<div class="subpanel">
    ${a.candidates.length ? `<div><b>Gefundene Releases</b> <span class="muted">(Klick übernimmt)</span></div>
    <div class="cand-list">${a.candidates.map((c) => `
      <div class="cand ${c.id === cur ? "current" : ""}" data-mbid="${esc(c.id)}" tabindex="0">
        <span>${esc(c.artist)} – <b>${esc(c.title)}</b></span>
        <span class="muted">${esc([c.date, c.country, c.formats, c.tracks + " Tracks", c.status].filter(Boolean).join(" · "))}</span>
      </div>`).join("")}</div>` : `<p class="muted">Keine Kandidaten gespeichert.</p>`}
    <div class="inline">
      <div class="field"><label for="q-alb-${a.id}">Album</label><input id="q-alb-${a.id}" value="${esc(a.hints?.tag_album || a.hints?.folder_album || a.target.album)}"></div>
      <div class="field"><label for="q-art-${a.id}">Interpret</label><input id="q-art-${a.id}" value="${esc(a.hints?.tag_artist || a.hints?.folder_artist || "")}"></div>
      <button class="btn small" data-act="search">Suchen</button>
    </div>
    <div class="inline">
      <div class="field"><label for="q-mbid-${a.id}">MusicBrainz-Release-Link oder ID</label><input id="q-mbid-${a.id}" placeholder="https://musicbrainz.org/release/…"></div>
      <button class="btn small" data-act="mbid">Übernehmen</button>
    </div>
  </div>`;
}

function editHTML(a) {
  const o = a.overrides || {};
  return `<div class="subpanel">
    <div class="inline">
      <div class="field"><label for="o-aa-${a.id}">Albuminterpret</label><input id="o-aa-${a.id}" value="${esc(o.albumartist || a.target.albumartist)}"></div>
      <div class="field"><label for="o-al-${a.id}">Album</label><input id="o-al-${a.id}" value="${esc(o.album || a.target.album)}"></div>
      <div class="field"><label for="o-y-${a.id}">Jahr</label><input id="o-y-${a.id}" value="${esc(o.year || a.target.year || "")}" inputmode="numeric" maxlength="4"></div>
      <button class="btn small primary" data-act="override">Übernehmen</button>
    </div>
    <small>Gilt für alle Lieder dieses Albums. Titel und Nummern bleiben wie berechnet.</small>
  </div>`;
}

/* Zeichenweiser Vergleich (LCS), damit auch eine einzelne geänderte Großschreibung auffällt.
   Liefert [altHTML, neuHTML] mit <mark> um die geänderten Stellen. */
function diffMarks(a, b) {
  if (a === b) return [esc(a), esc(b)];
  const n = a.length, m = b.length;
  if (n * m > 250000) return [esc(a), `<mark>${esc(b)}</mark>`];
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const ka = new Array(n).fill(false), kb = new Array(m).fill(false);   // true = unverändert
  for (let i = 0, j = 0; i < n && j < m;) {
    if (a[i] === b[j]) { ka[i++] = kb[j++] = true; } else if (L[i + 1][j] >= L[i][j + 1]) i++; else j++;
  }
  // kurze „gleiche“ Inseln zwischen Änderungen mit markieren, sonst wird es zu zerstückelt
  const smooth = (keep) => {
    for (let i = 0; i < keep.length;) {
      if (!keep[i]) { i++; continue; }
      let k = i; while (k < keep.length && keep[k]) k++;
      if (i > 0 && k < keep.length && k - i <= 2) keep.fill(false, i, k);
      i = k;
    }
  };
  smooth(ka); smooth(kb);
  const render = (s, keep) => {
    let out = "", i = 0;
    while (i < s.length) {
      let k = i; while (k < s.length && keep[k] === keep[i]) k++;
      out += keep[i] ? esc(s.slice(i, k)) : `<mark>${esc(s.slice(i, k))}</mark>`;
      i = k;
    }
    return out;
  };
  return [render(a, ka), render(b, kb)];
}

const caseOnly = (a, b) => a !== b && a.toLowerCase() === b.toLowerCase();

function splitPath(p) {
  const i = p.lastIndexOf("/");
  return i < 0 ? ["", p] : [p.slice(0, i + 1), p.slice(i + 1)];
}

function trackHTML(a, t) {
  const s = S.sel[a.id];
  const on = s.on && !s.xt.has(t.id);
  const open = S.openTracks.has(t.id);
  const [sd, sf] = splitPath(t.src);
  const [dd, df] = splitPath(t.dst);
  let fn;
  if (t.src === t.dst) fn = `<span class="same">${esc(sf)}</span>`;
  else if (sf === df) fn = `<span class="same">${esc(sf)}</span> <span class="muted">(nur Ordner)</span>`;
  else if (t.dup) fn = `<span class="old">${esc(t.src)}</span><span class="arrow">→</span><span class="new">${esc(t.dst)}</span>`;
  else {
    // Ordneränderungen stehen schon oben beim Album, hier nur den Dateinamen vergleichen
    const [o, nw] = diffMarks(sf, df);
    fn = `<span class="old">${sd === dd ? "" : esc(sd)}${o}</span><span class="arrow">→</span><span class="new">${nw}</span>`;
    if (caseOnly(sf, df)) fn += ` <span class="hint">nur Groß-/Kleinschreibung</span>`;
  }
  const cats = (t.cats || []).map(chip).join("");
  let row = `<tr class="trk ${on ? "" : "off"} ${t.dup ? "dup" : ""}" data-track="${esc(t.id)}">
    <td><input type="checkbox" class="cb-track" ${on ? "checked" : ""} ${t.has_changes ? "" : "disabled"} aria-label="Lied auswählen"></td>
    <td class="fn">${fn}</td>
    <td><span class="chips">${cats}</span></td>
    <td class="num muted">${t.score != null ? Math.round(t.score * 100) + "%" : ""}</td>
  </tr>`;
  if (open) {
    const ch = t.changes.filter((c) => !c.minor);
    row += `<tr class="trk-detail"><td></td><td colspan="3"><div class="diff">
      ${t.dup ? `<div class="msgs"><span class="w">Duplikat von ${esc(t.dup)}. Die bessere Kopie bleibt, diese wandert in die Quarantäne.</span></div>` : ""}
      ${ch.length ? `<table>${ch.map((c) => `<tr><th>${esc(c.label)}</th><td><del>${esc(c.old ?? "–")}</del></td><td><ins>${esc(c.new)}</ins></td></tr>`).join("")}</table>` : `<span class="muted">Tags bleiben inhaltlich gleich.</span>`}
      ${t.junk.length ? `<div class="junk"><b>Wird entfernt:</b> ${t.junk.map((j) => `${esc(j.key)} = „${esc(j.value)}“`).join(" · ")}</div>` : ""}
      ${t.embed_cover ? `<div class="junk">Cover wird eingebettet.</div>` : ""}
      ${t.fix_cover && t.fix_cover.length ? `<div class="junk">Eingebettetes Cover wird ersetzt (${t.fix_cover.map(esc).join(", ")}).</div>` : ""}
      ${t.changes.some((c) => c.minor) ? `<div class="junk">MusicBrainz-IDs werden gespeichert.</div>` : ""}
      ${t.companions.length ? `<div class="junk">Begleitdatei wird mit umbenannt: ${t.companions.map((c) => esc(c.src)).join(", ")}</div>` : ""}
      ${t.warnings.length ? `<ul class="msgs">${t.warnings.map((w) => `<li class="w">${esc(w)}</li>`).join("")}</ul>` : ""}
      <div class="muted">${esc(t.kind)} · ${t.bitrate ? Math.round(t.bitrate / 1000) + " kbit/s · " : ""}${fmtLen(t.length)}</div>
    </div></td></tr>`;
  }
  return row;
}

const fmtLen = (s) => s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "";

/* ---- Interaktion im Baum --------------------------------------------- */
function rerenderAlbum(id) {
  const el = $(`.album[data-album="${id}"]`);
  if (el) {
    el.outerHTML = albumHTML(albumIndex[id]);
    const cb = $(`.album[data-album="${id}"] .cb-album`);
    if (cb) cb.indeterminate = cb.dataset.state === "partial";
  }
  const artistEl = el && $(`.artist[data-artist="${CSS.escape(albumIndex[id].albumartist)}"]`);
  if (artistEl) {
    const ar = S.plan.artists.find((x) => x.name === albumIndex[id].albumartist);
    const cb = $(".cb-artist", artistEl);
    const st = artistAgg(ar.albums.filter(albumVisible));
    cb.checked = st !== "off"; cb.indeterminate = st === "partial";
  }
  updateSelSummary();
}

async function openAlbum(id) {
  S.openAlbums.add(id);
  rerenderAlbum(id);
  if (!S.details[id]) {
    S.details[id] = await api(`/api/album/${id}`);
    rerenderAlbum(id);
  }
}

$("#tree").addEventListener("click", async (e) => {
  const t = e.target;
  const artistEl = t.closest(".artist");
  const albumEl = t.closest(".album");

  // Checkboxen
  if (t.classList.contains("cb-artist")) {
    const ar = S.plan.artists.find((x) => x.name === artistEl.dataset.artist);
    for (const a of ar.albums.filter(albumVisible)) setAlbum(a.id, t.checked);
    const scroll = window.scrollY;
    renderTree(); window.scrollTo(0, scroll); updateSelSummary();
    return;
  }
  if (t.classList.contains("cb-album")) {
    setAlbum(albumEl.dataset.album, t.checked);
    rerenderAlbum(albumEl.dataset.album);
    return;
  }
  if (t.classList.contains("cb-track") || t.classList.contains("cb-item")) {
    const id = albumEl.dataset.album;
    const s = S.sel[id];
    const isTrack = t.classList.contains("cb-track");
    const key = isTrack ? t.closest("tr").dataset.track : t.dataset.item;
    if (!s.on) {  // Album war aus -> nur dieses eine an
      const d = S.details[id];
      s.on = true;
      s.xt = new Set(d.tracks.map((x) => x.id));
      s.xi = new Set(d.items.map((x) => x.id));
    }
    const set = isTrack ? s.xt : s.xi;
    if (t.checked) set.delete(key); else set.add(key);
    const d = S.details[id];
    if (s.xt.size >= d.tracks.filter((x) => x.has_changes).length && s.xi.size >= d.items.length) setAlbum(id, false);
    rerenderAlbum(id);
    return;
  }
  if (t.closest("a")) return;

  // Aktionen im Album
  const act = t.closest("[data-act]")?.dataset.act;
  if (act && albumEl) { await albumAction(albumEl.dataset.album, act); return; }
  const cand = t.closest(".cand");
  if (cand && albumEl) { await rematch(albumEl.dataset.album, { mbid: cand.dataset.mbid }); return; }

  // Aufklappen
  const trk = t.closest("tr.trk");
  if (trk) {
    const id = trk.dataset.track;
    S.openTracks.has(id) ? S.openTracks.delete(id) : S.openTracks.add(id);
    rerenderAlbum(albumEl.dataset.album);
    return;
  }
  if (t.closest(".album-row")) {
    const id = albumEl.dataset.album;
    if (S.openAlbums.has(id)) { S.openAlbums.delete(id); rerenderAlbum(id); } else await openAlbum(id);
    return;
  }
  if (t.closest(".artist-row")) {
    const name = artistEl.dataset.artist;
    S.openArtists.has(name) ? S.openArtists.delete(name) : S.openArtists.add(name);
    const ar = S.plan.artists.find((x) => x.name === name);
    artistEl.outerHTML = artistHTML(name, ar.albums.filter(albumVisible));
    for (const cb of $$(`.artist[data-artist="${CSS.escape(name)}"] input[data-state]`)) cb.indeterminate = cb.dataset.state === "partial";
  }
});

$("#tree").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList.contains("cand")) e.target.click();
});

async function albumAction(id, act) {
  const a = S.details[id];
  if (act === "panel-rematch" || act === "panel-edit") {
    const p = act.slice(6);
    S.openPanels[id] = S.openPanels[id] === p ? null : p;
    rerenderAlbum(id);
  } else if (act === "offline") await rematch(id, { offline: true });
  else if (act === "reset") await rematch(id, { reset: true });
  else if (act === "search") await rematch(id, { query: { album: $(`#q-alb-${id}`).value, artist: $(`#q-art-${id}`).value } });
  else if (act === "mbid") await rematch(id, { mbid: $(`#q-mbid-${id}`).value });
  else if (act === "override") await rematch(id, { overrides: { albumartist: $(`#o-aa-${id}`).value, album: $(`#o-al-${id}`).value, year: $(`#o-y-${id}`).value } });
  return a;
}

async function rematch(id, body) {
  const el = $(`.album[data-album="${id}"] .album-detail`);
  if (el) el.style.opacity = .5;
  try {
    const r = await api(`/api/album/${id}/rematch`, body);
    S.details[id] = r.album;
    S.openPanels[id] = null;
    setAlbum(id, true);   // bewusst angepasst -> auswählen
    // Plan neu laden (Ziele/Duplikate können sich verschoben haben), Auswahl bleibt
    const keepOpen = new Set(S.openAlbums);
    const details = S.details;
    const scroll = window.scrollY;
    await loadPlan(false);
    S.details = details;
    for (const k of Object.keys(S.details)) if (k !== id) delete S.details[k];
    S.openAlbums = keepOpen;
    S.openArtists.add(r.summary.albumartist);
    renderTree();
    for (const aid of keepOpen) if (!S.details[aid]) openAlbum(aid);
    window.scrollTo(0, scroll);
  } catch (err) {
    if (el) el.style.opacity = 1;
    await ask("Das hat nicht geklappt", `<p class="error">${esc(err.message)}</p>`, "OK", true);
  }
}

/* ---- Filter & Toolbar -------------------------------------------------- */
$("#filter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  S.filter = b.dataset.f;
  $$("#filter button").forEach((x) => x.classList.toggle("on", x === b));
  renderTree();
});
let qTimer;
$("#q").addEventListener("input", (e) => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { S.q = e.target.value.trim(); renderTree(); }, 180);
});
$("#show-unchanged").addEventListener("change", (e) => { S.showUnchanged = e.target.checked; renderTree(); });
$("#btn-expand").addEventListener("click", () => {
  for (const ar of S.plan.artists) if (ar.albums.some(albumVisible)) S.openArtists.add(ar.name);
  renderTree();
});
$("#btn-collapse").addEventListener("click", () => { S.openArtists.clear(); S.openAlbums.clear(); renderTree(); });
/* ---- Auswahl: alle / nach Kategorie (gilt für die aktuell gefilterten Alben) ---- */
const visibleAlbums = () => Object.values(albumIndex).filter((a) => a.has_changes && albumVisible(a));

function selectAlbums(albums, on) {
  for (const a of albums) setAlbum(a.id, on);
  const scroll = window.scrollY;
  renderTree(); window.scrollTo(0, scroll); updateSelSummary();
}

function renderSelCats() {
  if (!S.plan) return;
  const vis = visibleAlbums();
  $("#sel-cats").innerHTML = Object.keys(FLAGS).map((f) => {
    const hit = vis.filter((a) => a.flags.includes(f));
    if (!hit.length) return "";
    const on = hit.filter((a) => albumState(a.id) !== "off").length;
    const st = on === hit.length ? "on" : on ? "partial" : "off";
    const tip = st === "on" ? "Klick wählt diese Alben ab" : "Klick wählt diese Alben an";
    return `<button class="chip selchip c-${f} ${st}" data-flag="${f}" title="${esc(tip)}" aria-pressed="${st === "on"}">
      ${st === "on" ? "✓ " : ""}${esc(FLAGS[f])} <span class="num">${fmtN(on)}/${fmtN(hit.length)}</span></button>`;
  }).join("") || `<span class="muted">keine</span>`;
}

$("#sel-cats").addEventListener("click", (e) => {
  const b = e.target.closest("[data-flag]");
  if (!b) return;
  const hit = visibleAlbums().filter((a) => a.flags.includes(b.dataset.flag));
  selectAlbums(hit, !hit.every((a) => albumState(a.id) !== "off"));
});
$("#btn-all-on").addEventListener("click", () => {
  selectAlbums(visibleAlbums(), true);
  if (!S.q) { S.miscOn = new Set(S.plan.misc.map((m) => m.id)); renderMisc(); updateSelSummary(); }
});
$("#btn-all-off").addEventListener("click", () => {
  selectAlbums(Object.values(albumIndex).filter(albumVisible), false);
  if (!S.q) { S.miscOn.clear(); renderMisc(); updateSelSummary(); }
});

/* ---- Bereiche: alle an / aus ---- */
function setAllCats(on) {
  for (const c of CATS) S.cats[c.key] = on;
  renderCats(); updateSelSummary();
}
$("#btn-cats-on").addEventListener("click", () => setAllCats(true));
$("#btn-cats-off").addEventListener("click", () => setAllCats(false));

/* ---- Sonstiges (außerhalb von Alben) ----------------------------------- */
function renderMisc() {
  const m = S.plan.misc;
  if (!m.length) { $("#misc").innerHTML = ""; return; }
  $("#misc").innerHTML = `<div class="panel misc">
    <h3>Außerhalb von Albumordnern</h3>
    <p class="muted">Dateien auf Wurzel- oder Interpretenebene und leere Ordner.</p>
    <div class="items">${m.map((it) => `<label class="item"><input type="checkbox" data-misc="${esc(it.id)}" ${S.miscOn.has(it.id) ? "checked" : ""}> <span>${esc(it.label)}</span></label>`).join("")}</div>
  </div>`;
}
$("#misc").addEventListener("change", (e) => {
  const id = e.target.dataset.misc;
  if (!id) return;
  e.target.checked ? S.miscOn.add(id) : S.miscOn.delete(id);
  updateSelSummary();
});

/* ======================================================================
   Anwenden
   ====================================================================== */
function selectionPayload() {
  const sel = {};
  let albums = 0, tracks = 0;
  for (const [id, s] of Object.entries(S.sel)) {
    if (!s.on || !albumIndex[id]) continue;
    sel[id] = { exclude_tracks: [...s.xt], exclude_items: [...s.xi] };
    albums++;
    tracks += Math.max(0, albumIndex[id].n_changed - s.xt.size);
  }
  return { sel, albums, tracks };
}

function updateSelSummary() {
  if (!S.plan) return;
  const { albums, tracks } = selectionPayload();
  const cats = CATS.filter((c) => S.cats[c.key]).length;
  $("#sel-summary").textContent = `${fmtN(albums)} Alben · ca. ${fmtN(tracks)} Lieder · ${S.miscOn.size} sonstige · ${cats}/${CATS.length} Bereiche aktiv`;
  renderSelCats();
}

$("#btn-apply").addEventListener("click", async () => {
  const { sel, albums } = selectionPayload();
  if (!albums && !S.miscOn.size) { await ask("Nichts ausgewählt", "<p>Wähle mindestens ein Album aus.</p>", "OK", true); return; }
  let pv;
  try { pv = await api("/api/apply/preview", { selection: sel, categories: S.cats, misc: [...S.miscOn] }); }
  catch (err) { await ask("Das hat nicht geklappt", `<p class="error">${esc(err.message)}</p>`, "OK", true); return; }
  const c = pv.counts;
  const lines = [
    [c.tags, "Lied bekommt neue Tags", "Lieder bekommen neue Tags"],
    [c.junk, "Lied: Kommentare & Tag-Müll werden entfernt", "Lieder: Kommentare & Tag-Müll werden entfernt"],
    [c.moves, "Lied wird verschoben oder umbenannt", "Lieder werden verschoben oder umbenannt"],
    [c.cover_embed, "Lied bekommt ein Cover eingebettet", "Lieder bekommen ein Cover eingebettet"],
    [c.cover_fix, "Lied: eingebettetes Cover wird verkleinert/repariert", "Lieder: eingebettetes Cover wird verkleinert/repariert"],
    [c.cover_files, "Cover wird heruntergeladen", "Cover werden heruntergeladen"],
    [c.dups, "Duplikat geht in die Quarantäne", "Duplikate gehen in die Quarantäne"],
    [c.extras, "Fremddatei geht in die Quarantäne", "Fremddateien gehen in die Quarantäne"],
    [c.carry, "Bild wird mit umgezogen", "Bilder werden mit umgezogen"],
    [pv.misc, "sonstiger Eintrag", "sonstige Einträge"],
  ].filter(([n]) => n).map(([n, one, many]) => [n, n === 1 ? one : many]);
  const ok = await ask("Änderungen jetzt anwenden?", `
    ${lines.length ? `<table class="confirm-table">${lines.map(([n, t]) => `<tr><td class="num">${fmtN(n)}</td><td>${esc(t)}</td></tr>`).join("")}</table>`
      : `<p>Mit den gewählten Bereichen gibt es nichts zu tun.</p>`}
    ${pv.unsure.length ? `<div class="note warn"><b>${fmtN(pv.unsure.length)} unsichere Alben sind ausgewählt:</b>
      <ul>${pv.unsure.slice(0, 8).map((u) => `<li>${esc(u)}</li>`).join("")}${pv.unsure.length > 8 ? `<li>… und ${fmtN(pv.unsure.length - 8)} weitere</li>` : ""}</ul></div>` : ""}
    <p>Änderungsdatum: <b>${pv.touch ? "wird auf jetzt gesetzt" : "bleibt wie bisher"}</b></p>
    <p class="muted">Nichts wird endgültig gelöscht: Fremddateien und Duplikate landen in der Quarantäne. Der Lauf kann im Verlauf rückgängig gemacht werden.</p>`, "Ja, anwenden");
  if (!ok) return;
  try {
    await api("/api/apply", { selection: sel, categories: S.cats, misc: [...S.miscOn] });
    startPolling();
  } catch (err) {
    await ask("Das hat nicht geklappt", `<p class="error">${esc(err.message)}</p>`, "OK", true);
  }
});

/* ======================================================================
   Verlauf / Undo
   ====================================================================== */
async function loadUndo() {
  const list = await api("/api/undo");
  $("#undo-list").innerHTML = list.length ? list.map((u) => `
    <div class="undo-row">
      <div class="grow">
        <b>${new Date(u.time * 1000).toLocaleString("de-DE")}</b>
        <span class="muted"> · ${fmtN(u.ops)} Schritte · ${fmtN(u.summary?.tracks || 0)} Lieder</span>
        <div class="mono muted ellipsis">${esc(u.root)}</div>
      </div>
      ${u.undone ? `<span class="pill offline">rückgängig gemacht</span>` : `<button class="btn small ghost danger" data-undo="${esc(u.name)}">Rückgängig machen</button>`}
    </div>`).join("") : `<p class="muted">Noch keine Läufe.</p>`;
}
$("#undo-list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-undo]");
  if (b) runUndo(b.dataset.undo);
});

async function runUndo(name) {
  const ok = await ask("Lauf rückgängig machen?", "<p>Alle Umbenennungen, Verschiebungen und Tag-Änderungen dieses Laufs werden zurückgedreht. Neuere Läufe solltest du vorher zurückdrehen.</p>", "Rückgängig machen");
  if (!ok) return;
  try { await api(`/api/undo/${encodeURIComponent(name)}`, {}); startPolling(); }
  catch (err) { await ask("Das hat nicht geklappt", `<p class="error">${esc(err.message)}</p>`, "OK", true); }
}

/* ======================================================================
   Dialog
   ====================================================================== */
function ask(title, html, okLabel = "OK", infoOnly = false) {
  const dlg = $("#dlg");
  $("#dlg-title").textContent = title;
  $("#dlg-text").innerHTML = html;
  $("#dlg-ok").textContent = okLabel;
  $('#dlg button[value="cancel"]').hidden = infoOnly;
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((res) => dlg.addEventListener("close", () => res(dlg.returnValue === "ok"), { once: true }));
}

/* ======================================================================
   Start
   ====================================================================== */
(async function init() {
  await loadSettings();
  await loadPlan(true).catch(() => {});
  loadUndo();
  const j = await api("/api/status").catch(() => ({}));
  if (j.running) startPolling();
  else S.lastJobFinished = j.finished;
})();

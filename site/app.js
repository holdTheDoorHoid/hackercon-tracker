/* Hackercon Tracker - public site. Plain JS, no build step.
   Reads data.json (built from data/seed/conferences.json) and cities.json (for the home-city lookup). */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const REPO = "https://github.com/holdTheDoorHoid/hackercon-tracker";
  const TYPES = { bsides: "BSides", hacker: "Hacker con", maker: "Maker Faire", hardware: "Hardware", camp: "Camp",
                  industry: "Industry", academic: "Academic", online: "Online" };
  const KINDS = { cfp: "CFP", cfv: "Call for villages / makers", vendor: "Vendor tables", sponsor: "Sponsorship", training: "Training",
                  hotel: "Hotel block", travel: "Travel", early_bird: "Early-bird tickets", custom: "Deadline" };
  const COUNTRIES = { US: "United States", CA: "Canada", MX: "Mexico", GB: "United Kingdom", IE: "Ireland", DE: "Germany", FR: "France",
                      NL: "Netherlands", BE: "Belgium", LU: "Luxembourg", CH: "Switzerland", AT: "Austria", IT: "Italy", ES: "Spain", PT: "Portugal",
                      SE: "Sweden", NO: "Norway", DK: "Denmark", FI: "Finland", IS: "Iceland", CZ: "Czechia", PL: "Poland", HR: "Croatia",
                      RS: "Serbia", HU: "Hungary", MT: "Malta", MD: "Moldova", IL: "Israel", IN: "India", SG: "Singapore", JP: "Japan",
                      KR: "South Korea", CN: "China", AU: "Australia", NZ: "New Zealand", BR: "Brazil", AR: "Argentina", MA: "Morocco", JO: "Jordan" };
  const DRIVE_H = 6, ROAD = 1.22, MPH = 58;
  const todayISO = new Date().toISOString().slice(0, 10);
  const state = { data: null, cities: [], home: null, view: "list", cal: null, map: null, layer: null,
                  f: { q: "", type: "", country: "", month: "", status: "", drive: false, past: false } };

  // ---------- helpers
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const host = (u) => { try { return new URL(u).host.replace(/^www\./, ""); } catch { return u; } };
  const cap = (s) => s.replace(/\b\w/g, (c) => c.toUpperCase());
  const countryName = (c) => COUNTRIES[c] || c || "";
  function fmtDate(iso, withYear = true) {
    if (!iso) return "?";
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC", ...(withYear ? { year: "numeric" } : {}) });
  }
  function fmtRange(s, e) {
    if (!s) return "Dates unknown";
    if (!e || e === s) return fmtDate(s);
    if (s.slice(0, 7) === e.slice(0, 7)) return `${fmtDate(s, false)}–${+e.slice(8)}, ${s.slice(0, 4)}`;
    return `${fmtDate(s, false)} – ${fmtDate(e, false)}, ${e.slice(0, 4)}`;
  }
  const addDay = (iso) => { const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + 1); return d.toISOString().slice(0, 10); };
  const daysUntil = (iso) => Math.round((Date.parse(iso) - Date.parse(todayISO)) / 864e5);
  function haversine(a, b, c, d) {
    const R = 3958.8, t = (x) => (x * Math.PI) / 180, dl = t(c - a), dn = t(d - b);
    const h = Math.sin(dl / 2) ** 2 + Math.cos(t(a)) * Math.cos(t(c)) * Math.sin(dn / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }
  function nextEdition(c, includePast) {
    const eds = c.editions.filter((e) => e.start);
    const fut = eds.filter((e) => (e.end || e.start) >= todayISO && !["cancelled", "postponed"].includes(e.status));
    if (fut.length) return fut[0];
    return includePast && eds.length ? eds[eds.length - 1] : null;
  }
  function travel(c) {
    if (c.online) return { mode: "online", label: "Online" };
    if (!state.home || c.lat == null) return null;
    const mi = haversine(state.home.lat, state.home.lon, c.lat, c.lon), road = mi * ROAD, h = road / MPH;
    const homeC = state.home.country || "US", cc = c.country || homeC, near = ["US", "CA", "MX"];
    const far = cc !== homeC && !(near.includes(cc) && near.includes(homeC));
    if (h <= DRIVE_H && !far) return { mode: "drive", hours: h, label: `~${h.toFixed(1)} h drive (${Math.round(road)} mi)` };
    return { mode: "fly", miles: mi, label: `${far ? "International flight" : "Fly"} (${Math.round(mi).toLocaleString()} mi)` };
  }
  const where = (c) => c.online ? "Online" : [c.city, c.region].filter(Boolean).join(", ") + (c.country && c.country !== "US" ? ` · ${countryName(c.country)}` : "") || "Location unknown";
  const badge = (st) => st ? `<span class="badge ${esc(st)}">${esc(st)}</span>` : "";
  const colorOf = (t) => getComputedStyle(document.documentElement).getPropertyValue(`--c-${t}`).trim() || "#888";
  const typeBadge = (t) => `<span class="badge type" style="background:${colorOf(t)}">${esc(TYPES[t] || t || "")}</span>`;
  function linkRow(c, full = false) {
    const L = c.links || {}, out = [];
    const add = (u, label) => { if (u) out.push(`<a href="${esc(u.startsWith("http") ? u : "https://" + u)}" target="_blank" rel="noopener">${label}</a>`); };
    add(L.website, "🌐 " + (full ? host(L.website) : "Site"));
    add(L.cfp, "📣 CFP"); add(L.cfv, "🏕 Villages / workshops"); add(L.sponsor, "🤝 Sponsors"); add(L.sponsor_docs, "📄 Sponsor docs"); add(L.discord, "💬 Discord");
    if (L.twitter) add(L.twitter.startsWith("http") ? L.twitter : "https://x.com/" + L.twitter.replace(/^@/, ""), "𝕏 " + (L.twitter.startsWith("http") ? "X" : L.twitter.replace(/^@/, "")));
    add(L.mastodon, "🐘 Mastodon");
    if (L.bluesky) add(L.bluesky.startsWith("http") ? L.bluesky : "https://bsky.app/profile/" + L.bluesky.replace(/^@/, ""), "🦋 Bluesky");
    if (full) { add(L.linkedin, "in LinkedIn"); add(L.instagram, "📷 Instagram"); add(L.youtube, "▶ YouTube"); }
    return out.length ? `<div class="links">${out.join("")}</div>` : "";
  }

  // ---------- filtering
  function rows(forcePast = false) {
    const f = state.f, q = f.q.trim().toLowerCase(), past = f.past || forcePast, out = [];
    for (const c of state.data.conferences) {
      if (c.archived && !past) continue;
      const ed = nextEdition(c, past);
      if (!ed && !past) continue;
      if (f.type && c.type !== f.type) continue;
      if (f.country && c.country !== f.country) continue;
      if (f.month && !(ed && +ed.start.slice(5, 7) === +f.month)) continue;
      if (f.status === "firm" && !(ed && ["announced", "confirmed"].includes(ed.status))) continue;
      if (f.status === "estimated" && !(ed && ["estimated", "tentative", "unknown"].includes(ed.status))) continue;
      const tr = travel(c);
      if (f.drive && !(tr && (tr.mode === "drive" || tr.mode === "online"))) continue;
      if (q) {
        const hay = [c.name, c.city, c.region, countryName(c.country), c.description, ed && ed.venue, ed && ed.city].join(" ").toLowerCase();
        if (!hay.includes(q)) continue;
      }
      out.push({ c, ed, tr });
    }
    out.sort((a, b) => { const x = a.ed ? a.ed.start : "9999", y = b.ed ? b.ed.start : "9999"; return x < y ? -1 : x > y ? 1 : a.c.name.localeCompare(b.c.name); });
    return out;
  }

  // ---------- views
  function renderList(rs) {
    const el = $("#list");
    if (!rs.length) { el.innerHTML = '<div class="empty">Nothing matches. Loosen the filters.</div>'; return; }
    el.innerHTML = '<div class="list">' + rs.map(({ c, ed, tr }) => `
      <div class="card" data-slug="${esc(c.slug)}" role="button" tabindex="0">
        <div class="when">${ed ? fmtRange(ed.start, ed.end) : "Dates unknown"} ${ed ? badge(ed.status) : ""}${ed && ed.start >= todayISO ? ` <span class="muted">in ${daysUntil(ed.start)} d</span>` : ""}</div>
        <h3 class="name">${esc(c.name)} ${typeBadge(c.type)}</h3>
        <div class="where">${esc(where(c))}${tr ? " · " + esc(tr.label) : ""}${c.attendance_band ? ` · ${esc(c.attendance_band)}` : ""}</div>
        ${linkRow(c)}
      </div>`).join("") + "</div>";
  }
  function renderCal(rs) {
    const events = [];
    for (const { c } of rs) for (const e of c.editions) {
      if (!e.start) continue;
      events.push({ title: c.name, start: e.start, end: e.end ? addDay(e.end) : undefined, allDay: true, color: colorOf(c.type),
                    classNames: [e.status || ""], extendedProps: { slug: c.slug } });
    }
    if (!state.cal) {
      state.cal = new FullCalendar.Calendar($("#calendar"), {
        initialView: window.innerWidth < 700 ? "listMonth" : "dayGridMonth",
        headerToolbar: { left: "prev,next today", center: "title", right: "dayGridMonth,listMonth" },
        height: "auto", dayMaxEvents: 4, events: [],
        eventClick: (i) => { i.jsEvent.preventDefault(); location.hash = "#/c/" + i.event.extendedProps.slug; },
      });
      state.cal.render();
    }
    state.cal.removeAllEvents();
    state.cal.addEventSource(events);
  }
  function renderMap(rs) {
    if (!state.map) {
      const dark = matchMedia("(prefers-color-scheme: dark)").matches;
      state.map = L.map("leaflet", { scrollWheelZoom: true }).setView([39, -96], 4);
      L.tileLayer(`https://{s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png`, {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        subdomains: "abcd", maxZoom: 18 }).addTo(state.map);
      state.layer = L.layerGroup().addTo(state.map);
    }
    state.layer.clearLayers();
    const pts = [];
    for (const { c, ed, tr } of rs) {
      if (c.lat == null) continue;
      const m = L.circleMarker([c.lat, c.lon], { radius: 7, color: "#fff", weight: 1, fillColor: colorOf(c.type), fillOpacity: 0.9 });
      m.bindPopup(`<b>${esc(c.name)}</b><br>${ed ? fmtRange(ed.start, ed.end) : "Dates unknown"} ${ed ? badge(ed.status) : ""}<br>${esc(where(c))}${tr ? "<br>" + esc(tr.label) : ""}<br><a href="#/c/${esc(c.slug)}">Details →</a>`);
      state.layer.addLayer(m); pts.push([c.lat, c.lon]);
    }
    if (state.home) state.layer.addLayer(L.circleMarker([state.home.lat, state.home.lon], { radius: 9, color: "#222", weight: 2, fillColor: "#ffd43b", fillOpacity: 1 }).bindTooltip("Home: " + state.home.label));
    setTimeout(() => { state.map.invalidateSize(); if (pts.length) state.map.fitBounds(pts, { padding: [30, 30], maxZoom: 7 }); }, 30);
  }
  function render() {
    const rs = rows();
    $("#meta").textContent = `${rs.length} of ${state.data.conferences.length} events` + (state.f.past ? "" : " with a future date");
    if (state.view === "list") renderList(rs);
    else if (state.view === "cal") renderCal(rows(true));
    else renderMap(rs);
  }
  function setView(v) {
    state.view = v;
    for (const a of document.querySelectorAll("nav.views a")) a.classList.toggle("active", a.dataset.view === v);
    for (const id of ["list", "cal", "map"]) $("#" + id).hidden = id !== v;
    render();
  }

  // ---------- detail drawer
  function facts(c) {
    const rowsHtml = [];
    const add = (k, v) => { if (v) rowsHtml.push(`<b>${k}</b><span>${v}</span>`); };
    add("Attendance", c.attendance_est ? `~${(+c.attendance_est).toLocaleString()}` : c.attendance_band);
    add("Vending", c.vending_policy ? esc(c.vending_policy) + (c.vending_notes ? " · " + esc(c.vending_notes) : "") + (c.table_cost ? ` · table ${esc(c.table_cost)}` : "") : "");
    add("Workshops", c.workshop_track ? esc(c.workshop_track) + (c.village_hosting ? ` · villages: ${esc(c.village_hosting)}` : "") : "");
    add("IoT Village", c.iot_village_partner ? "partners with IoT Village" : "");
    add("Tickets", c.ticket_price ? esc(c.ticket_price) : "");
    const mails = [["General", c.contact_email], ["CFP", c.cfp_email], ["Sponsors", c.sponsor_email]].filter(([, m]) => m);
    if (mails.length) add("Email", mails.map(([k, m]) => `${k}: <a href="mailto:${esc(m)}">${esc(m)}</a>`).join("<br>"));
    return rowsHtml.length ? `<div class="facts">${rowsHtml.join("")}</div>` : "";
  }
  function openDetail(slug) {
    const c = state.data.conferences.find((x) => x.slug === slug);
    if (!c) return;
    const tr = travel(c), eds = [...c.editions].sort((a, b) => b.year - a.year);
    $("#detail").innerHTML = `
      <button class="close ghost" id="closeDetail" aria-label="Close">✕</button>
      <h2>${esc(c.name)} ${typeBadge(c.type)}${c.archived ? ' <span class="badge cancelled">no longer running</span>' : ""}</h2>
      <div class="muted">${esc(where(c))}${tr ? " · " + esc(tr.label) : ""}</div>
      ${linkRow(c, true)}
      ${c.description ? `<p>${esc(c.description)}</p>` : ""}
      ${facts(c)}
      <h3>Editions</h3>
      <table>${eds.map((e) => `<tr><td><b>${e.year}</b></td><td>${fmtRange(e.start, e.end)} ${badge(e.status)}
        ${e.training_start ? `<div class="muted">Training ${fmtRange(e.training_start, e.training_end)}</div>` : ""}
        ${e.venue || e.city ? `<div class="muted">${esc([e.venue, e.city].filter(Boolean).join(", "))}</div>` : ""}
        ${e.url ? `<div><a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(host(e.url))}</a></div>` : ""}
        ${e.notes ? `<div class="muted">${esc(e.notes)}</div>` : ""}
        ${e.deadlines.length ? `<div>${e.deadlines.map((d) => `${esc(KINDS[d.kind] || d.kind)}: <b>${fmtDate(d.due)}</b>${d.due >= todayISO ? ` <span class="muted">(in ${daysUntil(d.due)} d)</span>` : ""}${d.url ? ` <a href="${esc(d.url)}" target="_blank" rel="noopener">link</a>` : ""}${d.notes && /^Estimated/.test(d.notes) ? ' <span class="badge estimated">estimated</span>' : ""}`).join("<br>")}</div>` : ""}
      </td></tr>`).join("")}</table>
      ${c.hints && c.hints.length ? `<h3>Dates mentioned on the website</h3><p class="muted">${c.hints.map(esc).join(" · ")}<br><small>Found automatically on ${esc(host((c.links || {}).website || ""))}${c.hints_checked ? " on " + esc(c.hints_checked) : ""}. Not verified: open the site to check.</small></p>` : ""}
      ${c.verification_notes ? `<h3>Research notes</h3><p class="muted">${esc(c.verification_notes)}</p>` : ""}
      <p class="muted"><small>Something wrong or missing? <a href="${REPO}/issues/new?title=${encodeURIComponent("Correction: " + c.name)}" target="_blank" rel="noopener">Report a correction</a>.</small></p>`;
    $("#detail").hidden = false; $("#backdrop").hidden = false;
    $("#closeDetail").onclick = () => closeDetail();
    $("#detail").scrollTop = 0;
    document.title = `${c.name} · Hackercon Tracker`;
  }
  function closeDetail(navigate = true) {
    $("#detail").hidden = true; $("#backdrop").hidden = true; document.title = "Hackercon Tracker";
    if (navigate && /^#\/c\//.test(location.hash)) history.replaceState(null, "", "#/" + state.view);
  }
  function route() {
    const h = location.hash || "#/list", m = h.match(/^#\/c\/([\w-]+)/);
    if (m) { openDetail(m[1]); return; }
    closeDetail(false);
    setView((h.match(/^#\/(list|cal|map)/) || [])[1] || "list");
  }

  // ---------- home city
  function saveHome(h) {
    state.home = h;
    try { h ? localStorage.setItem("hct.home", JSON.stringify(h)) : localStorage.removeItem("hct.home"); } catch { /* private mode */ }
    $("#homeStatus").textContent = h ? `Home: ${h.label}` : "";
    $("#homeInput").value = h ? h.label : "";
    $("#drive").disabled = !h; if (!h) { $("#drive").checked = false; state.f.drive = false; }
    render();
  }
  async function lookupHome(text) {
    const parts = text.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
    if (!parts.length) return null;
    const city = parts[0], reg = parts[1] || "";
    let best = null;
    for (const x of state.cities) {
      if (x.c !== city) continue;
      if (!reg && (!best || x.k === "us")) best = x;
      if (reg && (x.r === reg || x.k === reg || COUNTRIES[x.k.toUpperCase()]?.toLowerCase() === reg)) { best = x; break; }
    }
    if (best) return { label: cap(best.c) + (best.r ? ", " + best.r.toUpperCase() : "") + (best.k !== "us" ? ", " + countryName(best.k.toUpperCase()) : ""), lat: best.lat, lon: best.lon, country: best.k.toUpperCase() };
    const ll = text.match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
    if (ll) return { label: `${ll[1]}, ${ll[2]}`, lat: +ll[1], lon: +ll[2], country: "US" };
    const r = await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&addressdetails=1&q=${encodeURIComponent(text)}`, { headers: { Accept: "application/json" } });
    const j = await r.json();
    if (j && j[0]) return { label: j[0].display_name.split(",").slice(0, 2).map((s) => s.trim()).join(", "), lat: +j[0].lat, lon: +j[0].lon, country: ((j[0].address || {}).country_code || "us").toUpperCase() };
    return null;
  }

  // ---------- boot
  function populateFilters() {
    const cs = state.data.conferences;
    const tsel = $("#type"), csel = $("#country"), msel = $("#month");
    for (const t of Object.keys(TYPES)) if (cs.some((c) => c.type === t)) tsel.insertAdjacentHTML("beforeend", `<option value="${t}">${TYPES[t]}</option>`);
    const counts = {};
    for (const c of cs) if (c.country) counts[c.country] = (counts[c.country] || 0) + 1;
    for (const k of Object.keys(counts).sort((a, b) => counts[b] - counts[a])) csel.insertAdjacentHTML("beforeend", `<option value="${k}">${esc(countryName(k))} (${counts[k]})</option>`);
    for (let m = 1; m <= 12; m++) msel.insertAdjacentHTML("beforeend", `<option value="${m}">${new Date(Date.UTC(2026, m - 1, 1)).toLocaleDateString(undefined, { month: "long", timeZone: "UTC" })}</option>`);
  }
  function bind() {
    const f = state.f;
    $("#q").addEventListener("input", (e) => { f.q = e.target.value; render(); });
    for (const id of ["type", "country", "month", "status"]) $("#" + id).addEventListener("change", (e) => { f[id] = e.target.value; render(); });
    $("#drive").addEventListener("change", (e) => { f.drive = e.target.checked; render(); });
    $("#past").addEventListener("change", (e) => { f.past = e.target.checked; render(); });
    $("#reset").addEventListener("click", () => { Object.assign(f, { q: "", type: "", country: "", month: "", status: "", drive: false, past: false });
      for (const id of ["q", "type", "country", "month", "status"]) $("#" + id).value = ""; $("#drive").checked = $("#past").checked = false; render(); });
    $("#list").addEventListener("click", (e) => { const card = e.target.closest(".card"); if (card && !e.target.closest("a")) location.hash = "#/c/" + card.dataset.slug; });
    $("#list").addEventListener("keydown", (e) => { const card = e.target.closest(".card"); if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); location.hash = "#/c/" + card.dataset.slug; } });
    $("#backdrop").addEventListener("click", () => closeDetail());
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#detail").hidden) closeDetail(); });
    $("#homeForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const v = $("#homeInput").value.trim();
      if (!v) return;
      $("#homeStatus").textContent = "Looking up…";
      try { const h = await lookupHome(v); if (h) saveHome(h); else $("#homeStatus").textContent = "Not found. Try \"City, ST\" or \"lat, lon\"."; }
      catch { $("#homeStatus").textContent = "Lookup failed (offline?). Try \"lat, lon\"."; }
    });
    $("#homeClear").addEventListener("click", () => saveHome(null));
    window.addEventListener("hashchange", route);
  }
  async function boot() {
    const [d, c] = await Promise.all([fetch("data.json").then((r) => r.json()), fetch("cities.json").then((r) => r.json()).catch(() => [])]);
    state.data = d; state.cities = c;
    $("#foot").textContent = `${d.conferences.length} events · dataset updated ${d.generated}. `;
    populateFilters(); bind();
    try { const h = JSON.parse(localStorage.getItem("hct.home") || "null"); if (h && h.lat != null) { state.home = h; $("#homeStatus").textContent = `Home: ${h.label}`; $("#homeInput").value = h.label; $("#drive").disabled = false; } } catch { /* ignore */ }
    route();
  }
  boot().catch((e) => { $("#list").innerHTML = `<div class="empty">Could not load data.json (${esc(e.message)}). If you opened this file directly, serve the folder instead: <code>python -m http.server -d site</code></div>`; });
})();

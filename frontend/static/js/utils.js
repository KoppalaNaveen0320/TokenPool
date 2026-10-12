/* ================================================================
   TOKENPOOL — UTILS  (frontend/static/js/utils.js)
   Shared helpers: toasts, modals, tabs, icons, chart wrappers
   ================================================================ */

/* ── TOAST ── */
function toast(msg, type="default", ms=3500) {
  let c = document.querySelector(".toast-cont");
  if (!c) { c = document.createElement("div"); c.className = "toast-cont"; document.body.appendChild(c); }
  const icons = { ok:checkSVG(), err:xSVG(), warn:warnSVG(), default:infoSVG() };
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.innerHTML = (icons[type]||icons.default) + `<span>${msg}</span>`;
  c.appendChild(el);
  setTimeout(() => { el.style.cssText = "opacity:0;transform:translateY(8px);transition:all .28s"; setTimeout(() => el.remove(), 280); }, ms);
}

/* ── MODAL ── */
function openModal(id)  { const m = document.getElementById(id); if(m) m.classList.add("open"); }
function closeModal(id) { const m = document.getElementById(id); if(m) m.classList.remove("open"); }

document.addEventListener("click", e => {
  if (e.target.classList.contains("overlay")) e.target.classList.remove("open");
  const mc = e.target.closest(".modal-close");
  if (mc) mc.closest(".overlay")?.classList.remove("open");
  if (!e.target.closest(".dd-wrap")) document.querySelectorAll(".dd-menu.open").forEach(m => m.classList.remove("open"));
  const ddt = e.target.closest("[data-dd]");
  if (ddt) { const m = document.getElementById(ddt.dataset.dd); if(m){ const o=m.classList.contains("open"); document.querySelectorAll(".dd-menu.open").forEach(x=>x.classList.remove("open")); if(!o) m.classList.add("open"); } }
});

/* ── TABS ── */
function initTabs() {
  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      const grp = btn.closest("[data-tabs]") || btn.parentElement;
      grp.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.dataset.tab;
      const pane_parent = btn.closest("[data-tab-parent]") || btn.parentElement.nextElementSibling || document;
      pane_parent.querySelectorAll(".tab-pane").forEach(p => p.classList.toggle("active", p.id === target));
    });
  });
}

/* ── TOGGLES ── */
function initToggles() {
  document.querySelectorAll(".tog").forEach(t => t.addEventListener("click", () => t.classList.toggle("on")));
}

/* ── STATUS BADGE ── */
function sbadge(status) {
  const map = { COMPLETED:"badge-green", ACTIVE:"badge-green", CONNECTED:"badge-green", HEALTHY:"badge-green", AVAILABLE:"badge-green", PAID:"badge-green",
    PENDING:"badge-yellow", PAUSED:"badge-yellow", MEDIUM:"badge-yellow", LIMITED:"badge-yellow",
    FAILED:"badge-red", SUSPENDED:"badge-red", HIGH:"badge-red", DISCONNECTED:"badge-red",
    LOW:"badge-blue", DEMO:"badge-blue", PROCESSING:"badge-yellow" };
  return `<span class="badge ${map[status]||"badge-gray"}">${status}</span>`;
}

/* ── MODEL AVATAR COLOR ── */
function mcol(letter) {
  const m = {N:"#0d9488",S:"#2563eb",A:"#7c3aed",V:"#d97706",C:"#dc2626",G:"#16a34a",O:"#f59e0b",M:"#8b5cf6"};
  return m[(letter||"N").toUpperCase()] || "#64748b";
}

/* ── FORMATTERS ── */
function fINR(v)  { return "₹" + Number(v).toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2}); }
function fNum(v)  { return Number(v).toLocaleString("en-IN"); }
function fDate(s) { return new Date(s).toLocaleDateString("en-IN",{day:"2-digit",month:"short",year:"numeric"}); }
function greeting(){ const h=new Date().getHours(); return h<12?"Good morning":h<17?"Good afternoon":"Good evening"; }

/* ── CHART HELPERS (require Chart.js CDN) ── */
function lineChart(id, labels, data, color="#0d9488") {
  const el = document.getElementById(id);
  if (!el || !window.Chart) return null;
  const ex = Chart.getChart(el); if(ex) ex.destroy();
  return new Chart(el, {
    type:"line",
    data:{ labels, datasets:[{ data, borderColor:color, backgroundColor:color+"18", borderWidth:2.5, fill:true, tension:.42, pointRadius:0, pointHoverRadius:4, pointHoverBackgroundColor:color }] },
    options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{display:false}, tooltip:{ mode:"index", intersect:false }},
      scales:{ x:{grid:{display:false},ticks:{font:{size:10},color:"#94a3b8",maxRotation:0}}, y:{grid:{color:"#f1f5f9"},ticks:{font:{size:10},color:"#94a3b8"},border:{dash:[4,4]}} }}
  });
}

function barChart(id, labels, data, color="#0d9488") {
  const el = document.getElementById(id);
  if (!el || !window.Chart) return null;
  const ex = Chart.getChart(el); if(ex) ex.destroy();
  return new Chart(el, {
    type:"bar",
    data:{ labels, datasets:[{ data, backgroundColor:color, borderRadius:4, barPercentage:.55 }] },
    options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{display:false}},
      scales:{ x:{grid:{display:false},ticks:{font:{size:10},color:"#94a3b8"}}, y:{grid:{color:"#f1f5f9"},ticks:{font:{size:10},color:"#94a3b8"},border:{dash:[4,4]}} }}
  });
}

function donutChart(id, labels, data, colors) {
  const el = document.getElementById(id);
  if (!el || !window.Chart) return null;
  const ex = Chart.getChart(el); if(ex) ex.destroy();
  return new Chart(el,{ type:"doughnut", data:{ labels, datasets:[{ data, backgroundColor:colors, borderWidth:0, hoverOffset:4 }] },
    options:{ responsive:true, maintainAspectRatio:false, cutout:"76%", plugins:{ legend:{display:false} }}
  });
}

/* ── INLINE SVG ICONS ── */
function checkSVG()  { return `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5"/></svg>`; }
function xSVG()      { return `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12"/></svg>`; }
function warnSVG()   { return `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"/></svg>`; }
function infoSVG()   { return `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z"/></svg>`; }

/* ── HTML ESCAPING ── */
function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
window.escHtml = escHtml;

/* ── GLOBAL INIT ── */
document.addEventListener("DOMContentLoaded", () => { initTabs(); initToggles(); });

/* online-mode.js — Helen-Server admin panel master toggle.
 *
 * Mounts a single "Online Mode" card and binds a click handler that
 * flips the gate via the admin endpoints. Refreshes every 8s so a
 * flip from another admin's browser is reflected here too.
 *
 * Depends on the page-global `api(method, path, body)` helper that
 * sits in admin/index.html. No other globals required.
 */

(function () {
  "use strict";

  const REFRESH_MS = 8000;

  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === "style") Object.assign(n.style, attrs[k]);
        else if (k === "onclick") n.addEventListener("click", attrs[k]);
        else n.setAttribute(k, attrs[k]);
      }
    }
    (children || []).forEach((c) =>
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c),
    );
    return n;
  }

  function makeCard() {
    const card = el("div", { class: "card narrow", id: "onlineModeCard" });
    card.innerHTML = `
      <h3>
        <div class="title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" width="20" height="20">
            <circle cx="12" cy="12" r="9"/>
            <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>
          </svg>
          وضع الإنترنت
          <span id="omChip" class="chip">—</span>
        </div>
      </h3>
      <div class="kv" style="margin-bottom:8px">
        <div>الحالة</div><div id="omState">—</div>
        <div>آخر تغيير</div><div id="omChanged">—</div>
        <div>الخدمات</div><div id="omSvcs" style="font-size:11px">—</div>
      </div>
      <div class="toolbar" style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn primary" id="omEnableBtn">تفعيل</button>
        <button class="btn danger" id="omDisableBtn"
                style="display:none">إيقاف</button>
        <button class="btn ghost tiny" id="omRefreshBtn">تحديث</button>
      </div>
      <div id="omErr" style="color:var(--err);font-size:12px;
                              margin-top:6px;display:none"></div>
    `;
    return card;
  }

  function fmtTs(t) {
    if (!t) return "—";
    const d = new Date(t * 1000);
    return d.toLocaleString();
  }

  async function refresh() {
    const r = await window.api("GET", "/api/online-mode/status");
    const errBox = document.getElementById("omErr");
    if (errBox) errBox.style.display = "none";

    if (!r.ok) {
      if (errBox) {
        errBox.textContent = "فشل قراءة الحالة: " + (r.status || "?");
        errBox.style.display = "block";
      }
      return;
    }
    const data = r.data || {};
    const enabled = !!data.enabled;
    const chip = document.getElementById("omChip");
    chip.textContent = enabled ? "ON" : "OFF";
    chip.style.background = enabled ? "#1f4b33" : "#5c2237";
    chip.style.color = enabled ? "var(--ok)" : "var(--err)";

    document.getElementById("omState").textContent = enabled
      ? "مفعَّل — يصل للإنترنت"
      : "متوقف — LAN فقط";
    document.getElementById("omChanged").textContent = fmtTs(
      data.last_change_at,
    );

    const svcsEl = document.getElementById("omSvcs");
    const svcs = data.services || [];
    if (svcs.length === 0) {
      svcsEl.textContent = "لا توجد خدمات مسجَّلة";
    } else {
      svcsEl.textContent = svcs
        .map((s) => `${s.name}:${s.running ? "✓" : "·"}`)
        .join("  ");
    }

    document.getElementById("omEnableBtn").style.display = enabled
      ? "none"
      : "inline-block";
    document.getElementById("omDisableBtn").style.display = enabled
      ? "inline-block"
      : "none";
  }

  async function flip(action) {
    const errBox = document.getElementById("omErr");
    errBox.style.display = "none";
    const reason = window.prompt(
      action === "enable"
        ? "سبب التفعيل (اختياري):"
        : "سبب الإيقاف (اختياري):",
      "",
    );
    if (reason === null) return; // user cancelled
    const r = await window.api(
      "POST",
      "/api/admin/online-mode/" + action,
      { reason: reason || null },
    );
    if (!r.ok) {
      errBox.textContent =
        "فشل العملية: " +
        (r.status || "?") +
        " — " +
        (typeof r.data === "string" ? r.data : JSON.stringify(r.data));
      errBox.style.display = "block";
      return;
    }
    await refresh();
  }

  function mount() {
    // Insert as the first card in the Overview tab so it's the very
    // first thing an admin sees on login.
    const tab = document.getElementById("tab-overview");
    if (!tab) return;
    const firstSection = tab.querySelector("section.row");
    if (!firstSection) return;
    const card = makeCard();
    firstSection.insertBefore(card, firstSection.firstChild);

    document
      .getElementById("omEnableBtn")
      .addEventListener("click", () => flip("enable"));
    document
      .getElementById("omDisableBtn")
      .addEventListener("click", () => flip("disable"));
    document
      .getElementById("omRefreshBtn")
      .addEventListener("click", refresh);

    refresh();
    setInterval(refresh, REFRESH_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();

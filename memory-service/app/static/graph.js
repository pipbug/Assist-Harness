/* Memory graph: no dependencies. Layout is computed up front, then drawn as SVG; memory text is only
   ever inserted with textContent. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const svg = $("graph");
  const NS = "http://www.w3.org/2000/svg";
  const LABELS = JSON.parse($("g-cats-data").textContent);
  const el = (name, attrs = {}, parent = null) => {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  };
  const html = (name, text, cls) => {
    const node = document.createElement(name);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const short = (s, n) => (s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s);

  let data = null;
  let nodes = [], edges = [], byId = new Map(), near = new Map();
  let selected = null, query = "", userMoved = false;
  const pos = new Map(); // remembered positions, so toggles and the slider don't scramble the layout
  const view = { k: 1, x: 0, y: 0 };
  const edgeLayer = el("g", {}, svg), nodeLayer = el("g", {}, svg);

  const size = () => ({ w: svg.clientWidth || 300, h: svg.clientHeight || 400 });

  // small stable hash, so the same memories always start from the same places
  const seed = (id) => {
    let h = 2166136261;
    for (let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 16777619);
    return (h >>> 0) / 4294967296;
  };

  function build() {
    const min = parseFloat($("g-sim").value);
    const withCats = $("g-cats").checked, withTags = $("g-tags").checked;
    nodes = []; edges = []; byId = new Map();
    const add = (n) => { nodes.push(n); byId.set(n.id, n); return n; };
    for (const m of data.memories) {
      const name = m.text.replace(/^Kira(?:['’]s)?\s+/i, "");
      add({ id: m.id, kind: "mem", label: short(name, 20), full: m.text, m, r: 9 });
    }
    const hub = (id, kind, label) => byId.get(id) || add({ id, kind, label: short(label, 22), full: label, r: kind === "cat" ? 12 : 8 });
    for (const m of data.memories) {
      if (withCats && m.category) {
        hub("c:" + m.category, "cat", LABELS[m.category] || m.category);
        edges.push({ a: m.id, b: "c:" + m.category, kind: "member", w: 0.5 });
      }
      if (withTags) for (const t of m.tags) {
        hub("t:" + t, "tag", "#" + t);
        edges.push({ a: m.id, b: "t:" + t, kind: "member", w: 0.4 });
      }
    }
    for (const l of data.links) if (l.score >= min) edges.push({ a: l.a, b: l.b, kind: "sim", score: l.score, w: l.score });
    near = new Map(nodes.map((n) => [n.id, new Map()]));
    for (const e of edges) { near.get(e.a).set(e.b, e); near.get(e.b).set(e.a, e); }
    const spread = 40 * Math.sqrt(nodes.length + 1);
    for (const n of nodes) {
      const p = pos.get(n.id);
      if (p) { n.x = p.x; n.y = p.y; continue; }
      // new node: start beside something already placed, else on a ring
      const anchor = [...near.get(n.id).keys()].map((id) => pos.get(id)).find(Boolean);
      const angle = seed(n.id) * Math.PI * 2, rad = anchor ? 40 : spread * (0.3 + 0.7 * seed(n.id + "r"));
      n.x = (anchor ? anchor.x : 0) + Math.cos(angle) * rad;
      n.y = (anchor ? anchor.y : 0) + Math.sin(angle) * rad;
    }
  }

  function layout(steps) {
    const n = nodes.length;
    if (n === 0) return;
    const { w, h } = size();
    const aspect = Math.max(0.4, Math.min(2.5, w / h)); // fill a portrait box with a portrait layout
    const vx = new Float64Array(n), vy = new Float64Array(n);
    const index = new Map(nodes.map((node, i) => [node.id, i]));
    const E = edges.map((e) => ({ i: index.get(e.a), j: index.get(e.b), len: e.kind === "sim" ? 120 - 70 * e.score : 75, w: e.w }));
    for (let s = 0; s < steps; s++) {
      const cool = 1 - s / steps;
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          let dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 1) { dx = seed(nodes[i].id) - 0.5; dy = seed(nodes[j].id) - 0.5; d2 = 1; }
          if (d2 > 160000) continue;
          const f = 2600 / d2;
          vx[i] += dx * f; vy[i] += dy * f; vx[j] -= dx * f; vy[j] -= dy * f;
        }
        vx[i] -= nodes[i].x * 0.03 / aspect; vy[i] -= nodes[i].y * 0.03 * aspect;
      }
      for (const e of E) {
        const a = nodes[e.i], b = nodes[e.j];
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
        const f = ((d - e.len) / d) * 0.12 * e.w;
        vx[e.i] += dx * f; vy[e.i] += dy * f; vx[e.j] -= dx * f; vy[e.j] -= dy * f;
      }
      for (let i = 0; i < n; i++) {
        const v = Math.hypot(vx[i], vy[i]), cap = 24 * cool + 1, c = v > cap ? cap / v : 1;
        nodes[i].x += vx[i] * c; nodes[i].y += vy[i] * c;
        vx[i] *= 0.55; vy[i] *= 0.55;
      }
    }
    for (const node of nodes) pos.set(node.id, { x: node.x, y: node.y });
  }

  function fit() {
    if (!nodes.length) return;
    const { w, h } = size();
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const n of nodes) { x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x); y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y); }
    const padX = 64, padY = 40;
    view.k = Math.max(0.1, Math.min(1.6, (w - 2 * padX) / Math.max(x1 - x0, 1), (h - 2 * padY) / Math.max(y1 - y0, 1)));
    view.x = w / 2 - view.k * (x0 + x1) / 2;
    view.y = h / 2 - view.k * (y0 + y1) / 2;
    userMoved = false;
    place();
  }

  function draw() {
    edgeLayer.replaceChildren();
    nodeLayer.replaceChildren();
    for (const e of edges) {
      e.el = el("line", { class: "g-edge " + e.kind }, edgeLayer);
      if (e.kind === "sim") e.el.setAttribute("stroke-width", (1 + (e.score - 0.45) * 8).toFixed(2));
    }
    for (const n of nodes) {
      n.el = el("g", { class: "g-node " + n.kind, tabindex: "0", role: "button" }, nodeLayer);
      n.el.dataset.id = n.id;
      n.el.setAttribute("aria-label", (n.kind === "cat" ? "Category " : n.kind === "tag" ? "Tag " : "Memory: ") + n.full);
      el("title", {}, n.el).textContent = n.full;
      el("circle", { class: "hit", r: 22 }, n.el);
      if (n.kind === "mem") el("circle", { class: "shape", r: n.r }, n.el);
      else if (n.kind === "cat") el("rect", { class: "shape", x: -n.r, y: -n.r, width: 2 * n.r, height: 2 * n.r, rx: 5 }, n.el);
      else el("rect", { class: "shape", x: -n.r + 1, y: -n.r + 1, width: 2 * n.r - 2, height: 2 * n.r - 2, transform: "rotate(45)" }, n.el);
      n.text = el("text", { y: n.r + 14 }, n.el);
      n.text.textContent = n.label;
      n.tw = 0;
    }
    for (const n of nodes) n.tw = n.text.getComputedTextLength();
    highlight();
    listLinks();
  }

  // move everything to its screen position, then keep only labels that don't overlap
  function place() {
    for (const n of nodes) {
      n.sx = n.x * view.k + view.x; n.sy = n.y * view.k + view.y;
      n.el.setAttribute("transform", `translate(${n.sx.toFixed(1)},${n.sy.toFixed(1)})`);
    }
    for (const e of edges) {
      const a = byId.get(e.a), b = byId.get(e.b);
      e.el.setAttribute("x1", a.sx); e.el.setAttribute("y1", a.sy);
      e.el.setAttribute("x2", b.sx); e.el.setAttribute("y2", b.sy);
    }
    const focus = selected ? near.get(selected) : null;
    const rank = (n) => (n.id === selected ? 0 : focus && focus.has(n.id) ? 1 : n.el.classList.contains("dim") ? 9 : n.kind === "cat" ? 2 : n.kind === "mem" ? 4 : 3);
    const taken = nodes.map((n) => [n.sx - n.r, n.sy - n.r, n.sx + n.r, n.sy + n.r, n]);
    for (const n of [...nodes].sort((a, b) => rank(a) - rank(b))) {
      const half = n.tw / 2 + 2;
      const box = [n.sx - half, n.sy + n.r + 3, n.sx + half, n.sy + n.r + 18, n];
      const clash = rank(n) === 9 || taken.some((t) => t[4] !== n && box[0] < t[2] && box[2] > t[0] && box[1] < t[3] && box[3] > t[1]);
      const show = n.id === selected || !clash;
      n.el.classList.toggle("quiet", !show);
      if (show) taken.push(box);
    }
  }

  function highlight() {
    const focus = selected ? near.get(selected) : null;
    const q = query.toLowerCase();
    const hit = (n) => !q || n.full.toLowerCase().includes(q) || (n.m && n.m.tags.some((t) => t.includes(q)));
    for (const n of nodes) {
      n.lit = focus ? n.id === selected || focus.has(n.id) : hit(n);
      n.el.classList.toggle("dim", !n.lit);
      n.el.classList.toggle("sel", n.id === selected);
      n.el.setAttribute("aria-pressed", n.id === selected ? "true" : "false");
    }
    for (const e of edges) {
      e.el.classList.toggle("dim", focus ? e.a !== selected && e.b !== selected : !(byId.get(e.a).lit && byId.get(e.b).lit));
    }
    place();
  }

  function select(id) {
    selected = id && byId.has(id) ? id : null;
    highlight();
    panel();
  }

  function panel() {
    const box = $("g-panel");
    box.replaceChildren();
    const n = selected && byId.get(selected);
    box.hidden = !n;
    if (!n) return;
    box.append(html("h2", n.full));
    if (n.m) {
      const meta = html("p", undefined, "meta");
      if (n.m.category) meta.append(html("span", LABELS[n.m.category] || n.m.category, "pill"));
      for (const t of n.m.tags) meta.append(html("span", "#" + t, "pill faint"));
      meta.append(html("span", `${n.m.source} · ${n.m.updated}`, "src"));
      box.append(meta);
    }
    const list = [...near.get(n.id)].map(([id, e]) => ({ o: byId.get(id), e }))
      .filter((x) => x.o.kind === "mem").sort((a, b) => (b.e.score || 0) - (a.e.score || 0));
    box.append(html("h3", n.m ? (list.length ? "Similar memories" : "No similar memories at this setting") : `${list.length} ${list.length === 1 ? "memory" : "memories"}`));
    const ul = html("ul");
    for (const { o, e } of list) {
      const li = html("li"), b = html("button");
      b.type = "button";
      b.append(html("span", o.full));
      if (e.score) b.append(html("span", e.score.toFixed(2), "g-score"));
      b.addEventListener("click", () => select(o.id));
      li.append(b); ul.append(li);
    }
    box.append(ul);
    const actions = html("div", undefined, "actions");
    const a = html("a", n.m ? "Edit memory" : "Open in list", "button-link primary");
    a.href = n.m ? "/m/" + encodeURIComponent(n.id)
      : n.kind === "cat" ? "/?category=" + encodeURIComponent(n.id.slice(2)) : "/?tag=" + encodeURIComponent(n.id.slice(2));
    const close = html("button", "Close", "quiet-btn");
    close.type = "button";
    close.addEventListener("click", () => select(null));
    actions.append(a, close);
    box.append(actions);
  }

  function listLinks() {
    const ul = $("g-links");
    ul.replaceChildren();
    const sims = edges.filter((e) => e.kind === "sim");
    for (const e of sims) {
      const li = html("li", `${byId.get(e.a).full} — ${byId.get(e.b).full} `);
      li.append(html("span", e.score.toFixed(2), "g-score"));
      ul.append(li);
    }
    $("g-table").hidden = !sims.length;
    const shown = data.memories.length;
    const count = shown < data.total ? `${shown} most recent of ${data.total} memories` : `${shown} ${shown === 1 ? "memory" : "memories"}`;
    $("g-note").textContent = `${count}, ${sims.length} similarity ${sims.length === 1 ? "link" : "links"}. Solid lines join similar memories (thicker is closer); dashed lines show category and tag.`;
  }

  function rebuild(steps) {
    build();
    if (selected && !byId.has(selected)) selected = null;
    layout(steps);
    draw();
    panel();
    if (!userMoved) fit();
  }

  // ---- pan, pinch, tap ----
  const zoomAt = (cx, cy, factor) => {
    const k = Math.max(0.1, Math.min(5, view.k * factor));
    view.x = cx - (cx - view.x) * (k / view.k); view.y = cy - (cy - view.y) * (k / view.k);
    view.k = k; userMoved = true;
    place();
  };
  const zoomCentre = (f) => { const { w, h } = size(); zoomAt(w / 2, h / 2, f); };
  const pointers = new Map();
  let moved = false, downId = null;
  const local = (ev) => { const r = svg.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };
  svg.addEventListener("pointerdown", (ev) => {
    if (ev.pointerType === "mouse" && ev.button !== 0) return;
    if (!pointers.size) { moved = false; downId = ev.target.closest(".g-node")?.dataset.id || null; }
    else moved = true; // second finger: a pinch is never a tap
    const p = local(ev);
    pointers.set(ev.pointerId, { ...p, ox: p.x, oy: p.y });
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener("pointermove", (ev) => {
    const p = pointers.get(ev.pointerId);
    if (!p) return;
    const now = local(ev);
    if (pointers.size === 2) {
      const other = [...pointers].find(([id]) => id !== ev.pointerId)[1];
      const before = Math.hypot(p.x - other.x, p.y - other.y), after = Math.hypot(now.x - other.x, now.y - other.y);
      const mx = (now.x + other.x) / 2, my = (now.y + other.y) / 2;
      view.x += (now.x - p.x) / 2; view.y += (now.y - p.y) / 2;
      if (before > 0) zoomAt(mx, my, after / before);
    } else if (moved || Math.hypot(now.x - p.ox, now.y - p.oy) > 8) {
      if (!moved) { moved = true; svg.classList.add("panning"); }
      view.x += now.x - p.x; view.y += now.y - p.y; userMoved = true;
      place();
    }
    p.x = now.x; p.y = now.y;
  });
  const up = (ev) => {
    if (!pointers.delete(ev.pointerId) || pointers.size) return;
    svg.classList.remove("panning");
    if (ev.type === "pointerup" && !moved) select(downId === selected ? null : downId);
  };
  svg.addEventListener("pointerup", up);
  svg.addEventListener("pointercancel", up);
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const p = local(ev);
    zoomAt(p.x, p.y, Math.exp(-Math.max(-60, Math.min(60, ev.deltaY)) * 0.004));
  }, { passive: false });
  svg.addEventListener("keydown", (ev) => {
    const id = ev.target.closest(".g-node")?.dataset.id;
    if (id && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); select(id === selected ? null : id); }
    if (ev.key === "Escape") select(null);
  });
  $("g-in").addEventListener("click", () => zoomCentre(1.4));
  $("g-out").addEventListener("click", () => zoomCentre(1 / 1.4));
  $("g-fit").addEventListener("click", fit);

  // ---- controls ----
  for (const id of ["g-cats", "g-tags"]) $(id).addEventListener("change", () => data && rebuild(150));
  let timer = 0;
  $("g-sim").addEventListener("input", () => {
    $("g-sim-out").textContent = parseFloat($("g-sim").value).toFixed(2);
    clearTimeout(timer);
    timer = setTimeout(() => data && rebuild(120), 80);
  });
  $("g-q").addEventListener("input", () => {
    query = $("g-q").value.trim();
    if (!data) return;
    selected = null;
    highlight(); panel();
  });
  new ResizeObserver(() => { if (data && nodes.length) userMoved ? place() : fit(); }).observe(svg);

  const message = (text) => { $("g-msg").textContent = text; $("g-msg").hidden = !text; };
  fetch("/graph/data", { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then((r) => {
      if (r.status === 401) { location.href = "/login"; return null; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then((d) => {
      if (!d) return;
      data = d;
      if (!d.memories.length) { message("No memories yet. They appear here as they are saved."); return; }
      message("");
      rebuild(d.memories.length > 150 ? 220 : 400);
      const focus = new URLSearchParams(location.search).get("focus");
      if (focus) select(focus);
    })
    .catch(() => message("The graph could not be loaded. Reload to try again."));
})();

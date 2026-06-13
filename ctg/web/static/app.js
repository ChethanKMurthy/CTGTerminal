const $ = (s) => document.querySelector(s);
const fmt = (n, d = 0) => (n == null || isNaN(n)) ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d });
const cls = (n) => n > 0 ? "pos" : (n < 0 ? "neg" : "dim");
const sgn = (n, d = 2) => (n > 0 ? "+" : "") + fmt(n, d);
async function api(p) { try { const r = await fetch(p); return await r.json(); } catch (e) { return null; } }

// --- tabs ---
document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll("#tabs button").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active"); $("#" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "graph") loadGraph();
});

// --- header: regime + caps ---
async function loadHeader() {
  const r = await api("/api/regime");
  if (r && r.label) {
    $("#regime-bar").innerHTML = `Regime <b>${r.label}</b> · risk <b>${r.risk_score}</b>/100 · `
      + `VIX <b>${r.india_vix ?? "—"}</b> · trend <b>${r.trend}</b> · breadth <b>${(r.breadth_above_ma50 != null ? Math.round(r.breadth_above_ma50 * 100) + "%" : "—")}</b>`;
  }
  const h = await api("/api/health");
  if (h) {
    const order = ["llm", "gemini", "groq", "fred", "reddit", "telegram", "email"];
    $("#caps").innerHTML = order.map(k =>
      `<span class="cap ${h.capabilities[k] ? "on" : "off"}">${k}${h.capabilities[k] ? " ✓" : ""}</span>`).join("");
  }
}

// --- signals ---
async function loadSignals() {
  const d = await api("/api/signals");
  if (!d || !d.signals.length) { $("#signals-body").innerHTML = `<p class="dim">No open signals yet — run a cycle (see System tab) or wait for the EOD run.</p>`; return; }
  $("#signals-body").innerHTML = d.signals.map(s => {
    const facs = Object.entries(s.drivers || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
      .map(([k, v]) => `<span class="fct ${v > 0 ? "pos" : "neg"}">${k} ${sgn(v)}</span>`).join("");
    const scen = (s.scenarios || []).map(x =>
      `<div><b>${x.name}</b><br>${Math.round((x.prob || 0) * 100)}%<br><span class="${cls(x.target_move_pct)}">${sgn(x.target_move_pct, 1)}%</span></div>`).join("");
    return `<div class="card">
      <span class="dir ${s.direction}">${s.direction}</span>
      <div class="sym">${s.symbol}</div><div class="co">${s.company || ""} · ${s.sector || ""}</div>
      <div class="conv-bar"><div class="conv-fill" style="width:${s.conviction}%"></div></div>
      <div class="spark">conviction ${s.conviction}/100</div>
      <div class="metrics">
        <div><span>EXP RETURN</span><b class="${cls(s.expected_return)}">${sgn(s.expected_return)}%</b></div>
        <div><span>TAIL RISK</span><b class="neg">${sgn(s.tail_risk, 1)}%</b></div>
        <div><span>HORIZON</span><b>${s.horizon || "~1m"}</b></div>
        <div><span>LAST</span><b>${fmt(s.last, 1)}</b></div>
      </div>
      <div class="thesis">${s.thesis || ""}</div>
      <div class="factors">${facs}</div>
      <div class="scen">${scen}</div>
    </div>`;
  }).join("");
}

// --- flow ---
async function loadFlow() {
  const d = await api("/api/flow"); if (!d) return;
  const m = d.metrics || {};
  const fiiC = cls(m.fii_net_latest), diiC = cls(m.dii_net_latest);
  let deals = (d.deal_pressure || []).map(x =>
    `<tr><td>${x.symbol}</td><td class="${cls(x.net_value_cr)}">${sgn(x.net_value_cr, 1)} cr</td><td>${x.n_deals}</td><td class="${cls(x.net_value_cr)}">${x.direction}</td></tr>`).join("");
  const view = (d.view || {});
  const fno = d.fno || {};
  const fnoRow = (who, o) => o ? `<tr><td>${who}</td>
      <td class="${cls(o.idx_fut_net)}">${sgn(o.idx_fut_net, 0)}</td>
      <td>${o.idx_fut_long_pct ?? "—"}%</td>
      <td class="${cls(o.idx_fut_net_chg)}">${o.idx_fut_net_chg != null ? sgn(o.idx_fut_net_chg, 0) : "—"}</td>
      <td class="${cls(o.idx_opt_directional)}">${sgn(o.idx_opt_directional, 0)}</td></tr>` : "";
  $("#flow-body").innerHTML = `
    <div class="panel">
      <h3 class="dim">Cash Flows (₹ crore)</h3>
      <div class="kpis">
        <div class="kpi"><div class="l">FII net (latest)</div><div class="v ${fiiC}">${sgn(m.fii_net_latest, 0)}</div></div>
        <div class="kpi"><div class="l">DII net (latest)</div><div class="v ${diiC}">${sgn(m.dii_net_latest, 0)}</div></div>
        <div class="kpi"><div class="l">FII 5d</div><div class="v ${cls(m.fii_net_5d)}">${sgn(m.fii_net_5d, 0)}</div></div>
        <div class="kpi"><div class="l">DII 5d</div><div class="v ${cls(m.dii_net_5d)}">${sgn(m.dii_net_5d, 0)}</div></div>
      </div>
      <p class="thesis"><b>Read:</b> ${view.summary || view.forced_flows || "—"} <span class="tag">${view.source || ""}</span></p>
      <h3 class="dim" style="margin-top:14px">F&amp;O Participant Positioning ${fno.date ? "· " + fno.date : ""}</h3>
      ${fno.headline ? `<p class="thesis"><b>${fno.headline}</b></p>` : ""}
      <table><thead><tr><th>Participant</th><th>Idx Fut Net</th><th>Long%</th><th>Δ Net</th><th>Idx Opt Dir.</th></tr></thead>
        <tbody>${fno.available ? (fnoRow("FII", fno.fii) + fnoRow("DII", fno.dii) + fnoRow("Pro", fno.pro) + fnoRow("Client", fno.client)) : '<tr><td colspan=5 class="dim">No F&O participant data yet</td></tr>'}</tbody></table>
    </div>
    <div class="panel">
      <h3 class="dim">Bulk / Block Deal Pressure (Nifty 50)</h3>
      <table><thead><tr><th>Symbol</th><th>Net value</th><th>Deals</th><th>Direction</th></tr></thead>
      <tbody>${deals || '<tr><td colspan=4 class="dim">No Nifty-50 institutional deals in window</td></tr>'}</tbody></table>
    </div>`;
}

// --- options ---
async function loadOptions() {
  const d = await api("/api/options"); if (!d) return;
  $("#options-body").innerHTML = ["NIFTY", "BANKNIFTY"].map(k => {
    const m = d[k]; if (!m) return `<div class="panel"><h3>${k}</h3><p class="dim">no chain</p></div>`;
    const gc = m.gamma_regime === "negative" ? "neg" : "pos";
    return `<div class="panel"><h3>${k} <span class="dim">exp ${m.expiry}</span></h3>
      <div class="kpis">
        <div class="kpi"><div class="l">Spot</div><div class="v">${fmt(m.spot, 0)}</div></div>
        <div class="kpi"><div class="l">PCR (OI)</div><div class="v ${m.pcr_oi > 1 ? "pos" : "neg"}">${m.pcr_oi}</div></div>
        <div class="kpi"><div class="l">Max Pain</div><div class="v">${fmt(m.max_pain, 0)}</div></div>
        <div class="kpi"><div class="l">ATM IV</div><div class="v">${m.atm_iv ?? "—"}</div></div>
      </div>
      <table>
        <tr><td>Dealer gamma regime</td><td class="${gc}"><b>${m.gamma_regime}</b></td></tr>
        <tr><td>OI support / resistance</td><td>${fmt(m.support_oi_strike, 0)} / ${fmt(m.resistance_oi_strike, 0)}</td></tr>
        <tr><td>IV skew (put−call)</td><td class="${cls(m.iv_skew_put_minus_call)}">${sgn(m.iv_skew_put_minus_call, 2)}</td></tr>
        <tr><td>Total CE / PE OI</td><td>${fmt(m.total_ce_oi)} / ${fmt(m.total_pe_oi)}</td></tr>
      </table></div>`;
  }).join("");
}

// --- option trades ---
window.optCharts = window.optCharts || {};
async function loadOptionTrades() {
  const d = await api("/api/option_trades"); if (!d) return;
  const pending = [];  // {id, payoff} charts to build after DOM insert
  const blocks = ["NIFTY", "BANKNIFTY"].map(u => {
    const o = d[u];
    if (!o || !o.available) return `<div class="panel"><h3>${u}</h3><p class="dim">No live chain yet</p></div>`;
    const v = o.views || {};
    const cards = (o.suggestions || []).map((s, i) => {
      const cid = `pay_${u}_${i}`;
      if (s.payoff && s.payoff.length) pending.push({ id: cid, payoff: s.payoff, be: s.breakevens });
      const legs = s.legs.map(l =>
        `<tr><td class="${l.action === 'BUY' ? 'pos' : 'neg'}">${l.action}</td><td>${l.type}</td><td>${fmt(l.strike, 0)}</td><td>₹${l.ltp}</td></tr>`).join("");
      const netCls = s.net_premium >= 0 ? "pos" : "neg";
      const netLbl = s.net_premium >= 0 ? "credit" : "debit";
      return `<div class="card" style="margin-top:10px">
        <div class="sym">${s.strategy}</div>
        <div class="co">stance: ${s.stance} · fit ${s.fit_score}/100 · R:R ${s.risk_reward ?? "—"}</div>
        <table style="margin:8px 0"><thead><tr><th>Action</th><th>Type</th><th>Strike</th><th>LTP</th></tr></thead><tbody>${legs}</tbody></table>
        <div class="metrics">
          <div><span>NET (${netLbl})</span><b class="${netCls}">₹${fmt(Math.abs(s.net_premium))}</b></div>
          <div><span>MAX PROFIT</span><b class="pos">${s.max_profit == null ? "unlimited" : "₹" + fmt(Math.abs(s.max_profit))}</b></div>
          <div><span>MAX LOSS</span><b class="neg">${s.max_loss == null ? "large/undef" : "₹" + fmt(Math.abs(s.max_loss))}</b></div>
          <div><span>BREAKEVEN</span><b>${(s.breakevens || []).map(b => fmt(b, 0)).join(" / ")}</b></div>
        </div>
        <canvas id="${cid}" height="70" style="margin-top:8px"></canvas>
        <div class="thesis">${s.rationale || ""}</div>
      </div>`;
    }).join("");
    return `<div class="panel">
      <h3 class="dim">${u} · spot ${fmt(o.spot, 0)} · exp ${o.expiry} (${o.days_to_expiry}d) · lot ${o.lot_size}</h3>
      <div class="spark">View <b class="${cls(v.dir_score)}">${v.direction}</b> (${sgn(v.dir_score, 2)}) · IV ${v.vol_regime} (VIX ${v.india_vix ?? "—"}) · dealer gamma ${v.gamma_regime} · 1σ ±${fmt(o.expected_move_1sigma, 0)} · PCR ${o.pcr} · S/R ${fmt(o.support, 0)}/${fmt(o.resistance, 0)}</div>
      ${cards || '<p class="dim">No high-fit setup right now</p>'}
      <h3 class="dim" style="margin-top:14px">All 24 NSE strategies · relevancy to current view</h3>
      <div class="catalog">${(o.catalog || []).map(c => {
        const r = c.relevancy;
        const klass = r == null ? "off" : (r >= 70 ? "hi" : (r > 0 ? "mid" : "lo"));
        const tag = r == null ? "n/a" : (r <= 0 ? "—" : r);
        return `<span class="catchip ${klass}" title="${c.stance} · ${c.style}">${c.name} <b>${tag}</b></span>`;
      }).join("")}</div>
    </div>`;
  }).join("");
  $("#optrades-body").innerHTML = `<div class="grid2">${blocks}</div>
    <p class="thesis" style="margin-top:10px">⚠️ Free option data is ~10-min delayed (last snapshot after close). Premiums move fast — these are research setups, not executable quotes. Options carry high, sometimes unlimited, risk. Not investment advice.</p>`;
  // build payoff charts (P&L at expiry vs underlying)
  pending.forEach(p => {
    const el = document.getElementById(p.id); if (!el) return;
    if (window.optCharts[p.id]) window.optCharts[p.id].destroy();
    window.optCharts[p.id] = new Chart(el, {
      type: "line",
      data: {
        labels: p.payoff.map(x => x.s),
        datasets: [{
          data: p.payoff.map(x => x.pnl), pointRadius: 0, borderWidth: 2, tension: 0,
          segment: { borderColor: ctx => (ctx.p0.parsed.y >= 0 ? "#1ec98b" : "#ff5c6c") },
          fill: { target: { value: 0 }, above: "rgba(30,201,139,.10)", below: "rgba(255,92,108,.10)" },
        }]
      },
      options: {
        plugins: { legend: { display: false }, tooltip: { callbacks: { title: c => "Spot " + c[0].label, label: c => "P&L ₹" + Number(c.parsed.y).toLocaleString("en-IN") } } },
        scales: {
          x: { ticks: { color: "#7d8aa0", maxTicksLimit: 6 }, grid: { color: "#1e2838" } },
          y: { ticks: { color: "#7d8aa0", maxTicksLimit: 4, callback: v => "₹" + (v / 1000).toFixed(0) + "k" }, grid: { color: ctx => ctx.tick.value === 0 ? "#3a4a66" : "#161d2a" } }
        }
      }
    });
  });
}

// --- portfolio ---
let eqChart;
async function loadPortfolio() {
  const d = await api("/api/portfolio"); if (!d) return;
  const b = d.book;
  $("#pf-summary").innerHTML = `
    <div class="kpi"><div class="l">Equity</div><div class="v">₹${fmt(b.equity)}</div></div>
    <div class="kpi"><div class="l">Total Return</div><div class="v ${cls(b.total_return_pct)}">${sgn(b.total_return_pct)}%</div></div>
    <div class="kpi"><div class="l">Cash</div><div class="v">₹${fmt(b.cash)}</div></div>
    <div class="kpi"><div class="l">Deployed</div><div class="v">₹${fmt(b.positions_value)}</div></div>
    <div class="kpi"><div class="l">Positions</div><div class="v">${(b.holdings || []).length}</div></div>`;
  const ec = d.equity_curve || [];
  const labels = ec.map(p => (p.ts || "").slice(5, 16)), vals = ec.map(p => p.equity);
  if (eqChart) eqChart.destroy();
  eqChart = new Chart($("#equityChart"), {
    type: "line", data: { labels, datasets: [{ data: vals, borderColor: "#ffb000", backgroundColor: "rgba(255,176,0,.08)", fill: true, tension: .25, pointRadius: 0, borderWidth: 2 }] },
    options: { plugins: { legend: { display: false } }, scales: { x: { ticks: { color: "#7d8aa0", maxTicksLimit: 8 }, grid: { color: "#1e2838" } }, y: { ticks: { color: "#7d8aa0" }, grid: { color: "#1e2838" } } } }
  });
  $("#pf-holdings").innerHTML = `<table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>Last</th><th>Value</th><th>P&L</th></tr></thead><tbody>` +
    (b.holdings || []).map(h => `<tr><td>${h.symbol}</td><td>${fmt(h.qty, 1)}</td><td>${fmt(h.avg_price, 1)}</td><td>${fmt(h.last, 1)}</td><td>₹${fmt(h.value)}</td><td class="${cls(h.pnl)}">${sgn(h.pnl, 0)}</td></tr>`).join("") +
    (b.holdings && b.holdings.length ? "" : '<tr><td colspan=6 class="dim">No open positions</td></tr>') + `</tbody></table>`;
}

// --- risk ---
async function loadRisk() {
  const d = await api("/api/risk"); if (!d) return;
  const v = d.var || {};
  const stress = (d.stress_tests || []).map(s =>
    `<div class="stress-row"><span>${s.scenario}</span><b class="${cls(s.pnl_rupees)}">₹${sgn(s.pnl_rupees, 0)}</b></div>`).join("");
  const sect = Object.entries(d.sector_exposure_pct || {}).map(([k, val]) =>
    `<div class="stress-row"><span>${k}</span><b class="${cls(val)}">${sgn(val, 1)}%</b></div>`).join("");
  $("#risk-body").innerHTML = `
    <div class="panel">
      <h3 class="dim">Exposure &amp; VaR</h3>
      <div class="kpis">
        <div class="kpi"><div class="l">Gross exposure</div><div class="v">${d.gross_exposure_pct ?? "—"}%</div></div>
        <div class="kpi"><div class="l">1-day VaR 95%</div><div class="v neg">${v.available ? "₹" + fmt(v.var_1d_rupees) : "—"}</div></div>
        <div class="kpi"><div class="l">VaR % gross</div><div class="v neg">${v.available ? v.var_1d_pct_of_gross + "%" : "—"}</div></div>
        <div class="kpi"><div class="l">Ann. vol</div><div class="v">${v.available ? v.ann_vol_pct + "%" : "—"}</div></div>
      </div>
      <h3 class="dim">Sector concentration</h3>${sect || '<p class="dim">flat</p>'}
    </div>
    <div class="panel">
      <h3 class="dim">Scenario Stress Tests (P&amp;L impact)</h3>
      ${stress || '<p class="dim">no positions to stress</p>'}
      <p class="thesis">Estimated mark-to-market hit per scenario using sector→macro sensitivities + market beta. Fortress check: no single shock should be uncomfortable.</p>
    </div>`;
}

// --- evals (Karpathy) ---
async function loadEvals() {
  const d = await api("/api/evals"); if (!d) return;
  const e = d.latest_eval;
  const lw = d.learned_weights || {};
  const weightRows = Object.entries(lw).map(([k, w]) =>
    `<div class="stress-row"><span>${k}</span><b>${w}</b></div>`).join("");
  let evalBlock;
  if (!e || e.status !== "ok") {
    evalBlock = `<p class="dim">${(e && e.note) || "Accumulating snapshots. Accuracy is scored once forward prices exist (≈1 week of daily EOD cycles). " + d.n_snapshots + " snapshots so far."}</p>`;
  } else {
    evalBlock = `<div class="kpis">
        <div class="kpi"><div class="l">Composite IC</div><div class="v ${cls(e.composite_ic)}">${e.composite_ic}</div></div>
        <div class="kpi"><div class="l">Hit rate</div><div class="v">${Math.round(e.hit_rate * 100)}%</div></div>
        <div class="kpi"><div class="l">Evaluated</div><div class="v">${e.n_evaluated}</div></div>
        <div class="kpi"><div class="l">Mean fwd ret</div><div class="v ${cls(e.mean_fwd_ret_pct)}">${sgn(e.mean_fwd_ret_pct)}%</div></div>
      </div>
      <h3 class="dim">Per-factor Information Coefficient</h3>
      ${Object.entries(e.factor_ic || {}).map(([k, ic]) => `<div class="stress-row"><span>${k}</span><b class="${cls(ic)}">${sgn(ic, 3)}</b></div>`).join("")}`;
  }
  $("#evals-body").innerHTML = `
    <div class="panel"><h3 class="dim">Forward-return scoring</h3>${evalBlock}</div>
    <div class="panel"><h3 class="dim">Adaptive factor weights</h3>
      ${weightRows || '<p class="dim">using base weights until the loop learns</p>'}
      <p class="thesis">Weights drift toward whatever factor is actually predicting forward returns now. Snapshots stored: <b>${d.n_snapshots}</b>.</p>
    </div>`;
}

// --- graph ---
let graphLoaded = false;
async function loadGraph() {
  if (graphLoaded) return; graphLoaded = true;
  const d = await api("/api/graph"); if (!d) return;
  const colors = { stock: "#4aa8ff", sector: "#ffb000", index: "#ff5c6c", macro: "#1ec98b", commodity: "#c792ea", fx: "#89ddff", theme: "#7d8aa0" };
  const nodes = d.nodes.map(n => ({
    id: n.id, label: n.label || n.id, shape: n.kind === "stock" ? "dot" : "box",
    color: colors[n.kind] || "#888", value: n.kind === "index" ? 40 : (n.kind === "sector" ? 25 : 12),
    font: { color: "#d7e0ee", size: 12 }
  }));
  const edges = d.edges.map(e => ({
    from: e.source, to: e.target,
    color: { color: e.relation === "correlated" ? "#26344a" : "#3a4a66", opacity: .6 },
    width: Math.max(0.5, Math.abs(e.weight) * 2), arrows: e.relation === "correlated" ? "" : "to"
  }));
  new vis.Network($("#graph-canvas"),
    { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) },
    { physics: { stabilization: true, barnesHut: { gravitationalConstant: -8000, springLength: 120 } }, interaction: { hover: true } });
}

// --- news ---
async function loadNews() {
  const d = await api("/api/news"); if (!d) return;
  $("#news-body").innerHTML = (d.news || []).map(n =>
    `<div class="newsitem"><a href="${n.url}" target="_blank">${n.title}</a><div class="src">${n.source} · ${(n.ts || "").slice(0, 16)}</div></div>`).join("")
    || '<p class="dim">No news yet</p>';
}

// --- health ---
async function loadHealth() {
  const h = await api("/api/health"); if (!h) return;
  const hb = Object.entries(h.ingest_heartbeats || {}).map(([k, v]) =>
    `<tr><td>${k}</td><td>${v ? v.count + " rows" : '<span class="dim">never</span>'}</td><td class="dim">${v ? v.ts : "—"}</td></tr>`).join("");
  const rc = Object.entries(h.row_counts || {}).map(([k, v]) => `<tr><td>${k}</td><td>${fmt(v)}</td></tr>`).join("");
  $("#health-body").innerHTML = `<div class="grid2">
    <div class="panel"><h3 class="dim">Ingestion heartbeats</h3><table><thead><tr><th>Source</th><th>Last pull</th><th>When</th></tr></thead><tbody>${hb}</tbody></table></div>
    <div class="panel"><h3 class="dim">Warehouse row counts</h3><table><tbody>${rc}</tbody></table></div></div>`;
}

async function refreshAll() {
  loadHeader(); loadSignals(); loadFlow(); loadOptions(); loadOptionTrades(); loadPortfolio();
  loadRisk(); loadEvals(); loadNews(); loadHealth();
}
refreshAll();
setInterval(refreshAll, 60000);  // live refresh every minute

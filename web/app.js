/* AI Hummingbot Brain — frontend. Plain vanilla JS, no build step. */
(() => {
  const $ = (id) => document.getElementById(id);

  // --- configurable API base (set via window.API_BASE) ---
  const API_BASE = window.API_BASE || '';

  // --- helpers ---
  const fmtMoney = (n) => {
    if (n === undefined || n === null || Number.isNaN(n)) return "$—";
    const abs = Math.abs(n);
    const digits = abs >= 100 ? 2 : abs >= 1 ? 2 : 4;
    return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  };
  const fmtPct = (n) => (n === undefined || n === null || Number.isNaN(n)) ? "—" : (n * 100).toFixed(1) + "%";
  const fmtNum = (n, d = 4) => (n === undefined || n === null || Number.isNaN(n)) ? "—" : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

  async function api(path, opts = {}) {
    const r = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!r.ok && r.status !== 423) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  // --- state ---
  const state = {
    snapshots: {},
    tickers: {},
    portfolio: null,
    health: null,
    strategies: [],
    opportunities: [],
    hermes: null,
    readiness: null,
  };

  // --- render: pills ---
  // V5.3 — reflect Auto Strategy toggle from settings (works before first scan SSE)
  function renderAutoStrategyChip() {
    const s = state.health && state.health.settings;
    if (!s) return;
    const on = !!s.auto_strategy_selection;
    const chip = document.getElementById("autoStrategyChip");
    const stateEl = document.getElementById("autoStrategyState");
    if (chip) {
      chip.classList.toggle("is-on", on);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (stateEl) {
      // Preserve "ON → <selected>" if a scan event has populated it; only overwrite if generic / em-dash.
      const current = stateEl.textContent || "";
      if (!current || current === "—" || current.startsWith("OFF") || current.startsWith("ON")) {
        const live = state.autoStrategy;
        if (on) {
          stateEl.textContent = (live && live.selected) ? ("ON → " + live.selected) : "ON";
        } else {
          stateEl.textContent = "OFF (manual)";
        }
      }
    }
  }

  function renderPills() {
    const h = state.health;
    if (!h) return;
    renderAutoStrategyChip();
    const paperPill = $("paperPill");
    const mode = (h.settings && h.settings.execution_mode) || h.execution_mode || "paper";
    if (paperPill) {
      if (mode === "paper") {
        paperPill.textContent = "PAPER MODE — OKX LOCKED";
        paperPill.className = "pill pill-warn";
      } else if (mode === "okx_demo") {
        const ok = state.readiness && state.readiness.can_execute;
        paperPill.textContent = ok ? "OKX DEMO — READY" : "OKX DEMO — GATE LOCKED";
        paperPill.className = ok ? "pill pill-live" : "pill pill-warn";
      } else if (mode === "okx_live") {
        const ok = state.readiness && state.readiness.can_execute;
        paperPill.textContent = ok ? "OKX LIVE — READY" : "OKX LIVE — GATE LOCKED";
        paperPill.className = ok ? "pill pill-off" : "pill pill-warn";
      }
    }
    const aiPill = $("aiPill");
    const dataPill = $("dataPill");
    if (h.ai_key_present && (h.ai_status?.available !== false)) {
      aiPill.textContent = "AI: ONLINE";
      aiPill.className = "pill pill-live";
    } else if (h.ai_key_present) {
      aiPill.textContent = "AI: KEY OK / unverified";
      aiPill.className = "pill";
    } else {
      aiPill.textContent = "AI: OFFLINE (no API key)";
      aiPill.className = "pill pill-off";
    }
    if (h.market_status?.live) {
      dataPill.textContent = "Data: LIVE (OKX)";
      dataPill.className = "pill pill-live";
    } else {
      dataPill.textContent = "Data: OFFLINE — NO LIVE DATA";
      dataPill.className = "pill pill-off";
    }
  }

  function renderStrategies() {
    const settings = state.health?.settings || {};
    const active = settings.active_strategy || "commander_blend";
    const list = state.strategies?.length ? state.strategies : (state.health?.strategies || []);
    const select = $("strategySelect");
    if (select) select.value = active;
    const drawerSelect = $("set-active_strategy");
    if (drawerSelect) drawerSelect.value = active;
    const current = list.find((x) => x.id === active);
    $("activeStrategyName").textContent = current ? current.name : active;

    const wrap = $("strategyArsenal");
    if (!wrap) return;
    wrap.innerHTML = list.map((m) => {
      const cls = m.id === active ? "strategy-card active" : "strategy-card";
      return `<button class="${cls}" data-strategy-id="${m.id}" data-testid="button-strategy-${m.id}">
        <span class="strategy-card-top">
          <strong>${m.name}</strong>
          <em>${m.risk}</em>
        </span>
        <span class="strategy-tagline">${m.tagline}</span>
      </button>`;
    }).join("");
    wrap.querySelectorAll("[data-strategy-id]").forEach((btn) => {
      btn.addEventListener("click", () => setStrategy(btn.getAttribute("data-strategy-id")));
    });
  }

  // --- render: portfolio ---
  function renderPortfolio(p) {
    if (!p) return;
    state.portfolio = p;
    $("equity").textContent = fmtMoney(p.equity);
    $("cash").textContent = fmtMoney(p.cash_usdt);
    $("realized").textContent = fmtMoney(p.realized_pnl);
    $("realized").classList.toggle("pos", p.realized_pnl > 0);
    $("realized").classList.toggle("neg", p.realized_pnl < 0);
    $("unreal").textContent = fmtMoney(p.unrealized_pnl);
    $("unreal").classList.toggle("pos", p.unrealized_pnl > 0);
    $("unreal").classList.toggle("neg", p.unrealized_pnl < 0);
    $("fees").textContent = fmtMoney(p.fees_paid);
    $("tradesToday").textContent = p.trades_today ?? 0;
    // V5.4 — paper profile banner + cash reserve warning
    const profile = p.paper_profile || (state.health?.settings?.paper_profile) || "live_readiness";
    const profEl = $("paperProfileBanner");
    if (profEl) {
      profEl.classList.toggle("profile-banner-readiness", profile === "live_readiness");
      profEl.classList.toggle("profile-banner-learning", profile === "learning");
      const lbl = $("paperProfileLabel");
      if (lbl) lbl.textContent = profile;
      profEl.lastChild && (profEl.lastChild.textContent = profile === "live_readiness"
        ? " \u2014 conservative defaults; eligible only for live readiness."
        : " \u2014 looser exploration; samples do NOT count toward live readiness.");
    }
    const cashWarn = $("cashWarning");
    if (cashWarn) cashWarn.classList.toggle("hidden", !p.cash_reserve_breached);
    const wrap = $("openPositions");
    if (!p.open_positions?.length) {
      wrap.innerHTML = '<div class="empty">No open positions.</div>';
    } else {
      wrap.innerHTML = p.open_positions.map((pos) => {
        const last = state.tickers[pos.symbol]?.last ?? pos.entry_price;
        const pnl = (last - pos.entry_price) * pos.qty;
        const cls = pnl >= 0 ? "pos" : "neg";
        // V6.4 — explicit Direction label so the operator can see at a glance
        // that every open paper position is a LONG (spot buy). This build is
        // spot-only; short selling is not wired in the paper or live tester.
        const side = String(pos.side || "long").toUpperCase();
        const sideCls = side === "SHORT" ? "side-badge side-short" : "side-badge side-long";
        return `<div class="position" data-testid="row-position-${escapeHtml(pos.symbol || "")}">
          <span class="sym">${pos.symbol} <span class="${sideCls}">${side}</span></span>
          <span>qty ${fmtNum(pos.qty, 6)}</span>
          <span>entry ${fmtNum(pos.entry_price, 4)} → ${fmtNum(last, 4)}</span>
          <span>SL ${fmtNum(pos.stop_loss, 4)} · TP ${fmtNum(pos.take_profit, 4)} · trail ${fmtNum(pos.trailing_stop, 4)}</span>
          <span class="pnl ${cls}" title="Direction: ${side} (spot buy) · open paper position">${fmtMoney(pnl)}</span>
        </div>`;
      }).join("");
    }
  }

  // --- render: agents ---
  function renderAgents(verdict) {
    if (!verdict || !verdict.symbol) return;
    // V6.4 — the committee always votes on a *candidate* setup (the thing the
    // bot is *considering opening*), not on an already-open position. Make
    // that explicit and tag the candidate direction. In this spot-only build
    // there is no short candidate; every committee vote is on a LONG setup.
    // V8 — explicit direction labelling for the committee verdict.
    //   LONG (spot buy)    : tradeable in this build
    //   SHORT_UNAVAILABLE  : valid signal but not executable in spot-only V8
    //   NO_TRADE           : committee is going to wait / skip
    const dirCode = String(verdict.direction_code || "").toUpperCase();
    const candSide = String(verdict.side || verdict.candidate_side || "long").toUpperCase();
    let chosenLabel;
    let sideCls;
    if (dirCode === "SHORT_UNAVAILABLE" || candSide === "SHORT") {
      sideCls = "side-badge side-short";
      chosenLabel = `Chosen: SHORT signal observed — not executable in spot-only V8`;
    } else if (dirCode === "NO_TRADE" || String(verdict.consensus || "").toLowerCase() !== "proceed") {
      sideCls = "side-badge side-na";
      chosenLabel = `Chosen: NO TRADE on ${escapeHtml(verdict.symbol || "—")}`;
    } else {
      sideCls = "side-badge side-long";
      chosenLabel = `Chosen: LONG ${escapeHtml(verdict.symbol || "—")} (spot buy)`;
    }
    const sideBadge = `<span class="${sideCls}">${candSide}</span>`;
    const symEl = $("consensusSym");
    if (symEl) symEl.innerHTML = `${escapeHtml(verdict.symbol)} ${sideBadge} <span class="muted">candidate setup</span>`;
    const candLine = $("consensusCandidateLine");
    if (candLine) {
      candLine.textContent = `${chosenLabel}. The committee votes on whether to OPEN this setup — it is not a verdict on any already-open position.`;
    }
    const counts = verdict.vote_counts;
    const countText = counts ? ` · ${counts.proceed}P/${counts.wait}W/${counts.skip}S` : "";
    $("consensusVote").textContent = (verdict.consensus || "—") + countText;
    $("consensusConf").textContent = (verdict.consensus_confidence ?? 0).toFixed(3);

    const agents = verdict.agents || [];
    for (const v of agents) {
      const card = document.querySelector(`.agent[data-agent="${v.agent}"]`);
      if (!card) continue;
      card.classList.remove("proceed", "wait", "skip", "error");
      const cls = v.ok ? (v.vote || "wait") : "error";
      card.classList.add(cls);
      $("model-" + v.agent).textContent = v.model || "—";
      $("vote-" + v.agent).className = "agent-vote " + (v.ok ? v.vote : "");
      $("vote-" + v.agent).textContent = v.ok
        ? `${(v.vote || "wait").toUpperCase()}  ·  conf ${Number(v.confidence ?? 0).toFixed(2)}  ·  ${v.latency_ms}ms`
        : "ERROR";
      $("reason-" + v.agent).textContent = v.ok
        ? (v.reason || "—") + (v.risk_notes ? `  ·  risk: ${v.risk_notes}` : "")
        : (v.error || "AI offline");
    }
    if (verdict.mode === "indicator_only") {
      // Indicate that committee is bypassed
      $("consensusVote").textContent = verdict.consensus + " (indicator-only)";
    }
  }

  // --- render: market scan ---
  // V8 — added Direction column. Spot-only build, so every row resolves to
  // either LONG / SHORT signal (not executable) / NO TRADE.
  function renderMarket() {
    const tbody = $("marketBody");
    const symbols = Object.keys(state.tickers);
    if (!symbols.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">Loading market data…</td></tr>';
      return;
    }
    // Build a lookup from opportunities for fast direction_label retrieval.
    const dirBySymbol = {};
    for (const o of (state.opportunities || [])) {
      if (o && o.symbol) dirBySymbol[o.symbol] = o;
    }
    tbody.innerHTML = symbols.map((sym) => {
      const t = state.tickers[sym];
      const s = state.snapshots[sym];
      const o = dirBySymbol[sym];
      const change = t.open24h ? ((t.last - t.open24h) / t.open24h) * 100 : 0;
      const changeCls = change >= 0 ? "pos" : "neg";
      const bias = s?.bias || "—";
      let dirHtml;
      const dirCode = o ? o.direction_code : null;
      if (dirCode === "LONG") {
        dirHtml = `<span class="side-badge side-long">LONG</span>`;
      } else if (dirCode === "SHORT_UNAVAILABLE" || (!o && bias === "bearish")) {
        dirHtml = `<span class="side-badge side-short" title="SHORT signal observed — not executable in spot-only V8">SHORT n/a</span>`;
      } else if (dirCode === "NO_TRADE" || !o) {
        dirHtml = `<span class="side-badge side-na">NO TRADE</span>`;
      } else {
        dirHtml = `<span class="side-badge side-long">LONG</span>`;
      }
      return `<tr>
        <td><strong>${sym}</strong></td>
        <td>${fmtNum(t.last, 4)}</td>
        <td class="pnl ${changeCls}">${change.toFixed(2)}%</td>
        <td class="bias-${bias}">${bias}</td>
        <td>${dirHtml}</td>
        <td>${s ? s.rsi14.toFixed(1) : "—"}</td>
        <td>${s ? s.vol_ratio.toFixed(2) + "×" : "—"}</td>
        <td>${s ? s.confidence.toFixed(2) : "—"}</td>
      </tr>`;
    }).join("");
  }

  function renderOpportunities(items) {
    const wrap = $("opportunities");
    if (!wrap) return;
    state.opportunities = items || state.opportunities || [];
    const rows = state.opportunities.slice(0, 8);
    if (!rows.length) {
      wrap.innerHTML = '<div class="empty">Watching for the next clean setup…</div>';
      return;
    }
    wrap.innerHTML = rows.map((o) => {
      const cls = o.status === "ready" ? "ready" : o.status === "watch" ? "watch" : "avoid";
      const htf = o.higher_timeframe ? `${o.higher_timeframe.timeframe} ${o.higher_timeframe.bias}` : "HTF —";
      const reasons = (o.reasons || []).slice(0, 2).join(" · ");
      // V6.4 — candidate direction. Backend only emits long-spot candidates
      // today; if a future build adds a short side it will surface here. If
      // the bias is bearish we explicitly say SHORT is unavailable instead of
      // silently hiding the asymmetry from the operator.
      // V8 — prefer the explicit direction_label/direction_code emitted by
      // the backend (LONG / SHORT_UNAVAILABLE / NO_TRADE). Fall back to the
      // older bias-based heuristic for older payloads.
      const dirCode = String(o.direction_code || "").toUpperCase();
      const dirLabel = o.direction_label;
      const rawSide = String(o.side || o.candidate_side || "long").toLowerCase();
      const isAvoidBearish = o.status === "avoid" && (o.bias === "bearish");
      let sideHtml;
      if (dirCode === "SHORT_UNAVAILABLE" || rawSide === "short") {
        sideHtml = `<span class="side-badge side-short">SHORT signal</span> <span class="side-na" title="This spot-only build does not short.">not executable in spot-only V8</span>`;
      } else if (dirCode === "NO_TRADE") {
        sideHtml = `<span class="side-badge side-na">NO TRADE</span>`;
      } else if (dirCode === "LONG") {
        sideHtml = `<span class="side-badge side-long">${escapeHtml(dirLabel || "LONG (spot buy)")}</span>`;
      } else if (isAvoidBearish) {
        sideHtml = `<span class="side-badge side-long">LONG (spot buy)</span> <span class="side-na" title="This spot-only build does not short. Bearish setups are avoided/watched, never sold short.">SHORT unavailable in this spot-only build</span>`;
      } else {
        sideHtml = `<span class="side-badge side-long">LONG (spot buy)</span>`;
      }
      return `<div class="opportunity ${cls}" data-testid="row-opportunity-${escapeHtml(o.symbol || "")}">
        <div class="opp-top">
          <strong>${o.symbol}</strong>
          <span>${String(o.status || "watch").toUpperCase()} · ${(Number(o.score || 0) * 100).toFixed(0)}%</span>
        </div>
        <div class="opp-side">Candidate direction: ${sideHtml}</div>
        <div class="opp-mid">${o.setup || "—"} · ${htf}</div>
        <div class="opp-meta">RSI ${o.rsi14 ? Number(o.rsi14).toFixed(1) : "—"} · Vol ${o.vol_ratio ? Number(o.vol_ratio).toFixed(2) + "×" : "—"}</div>
        <div class="opp-reasons">${reasons || "No clean trigger yet."}</div>
      </div>`;
    }).join("");
  }

  function renderHermes(h) {
    if (!h) return;
    state.hermes = h;
    $("hermesGoal").textContent = h.goal || "Hermes is paper-training on live-data decisions.";
    const dq = h.data_quality || {};
    $("hermesDataQuality").textContent = dq.score !== undefined ? `${dq.score}/100` : "—";
    $("hermesDataQuality").classList.toggle("pos", !!dq.ok);
    $("hermesDataQuality").classList.toggle("neg", dq.ok === false);
    const issue = dq.issues?.length ? dq.issues[0] : (dq.rule || "Live OKX data only. No simulated data.");
    $("hermesDataRule").textContent = `${dq.source || "OKX public REST"} · ${issue}`;
    $("hermesClosed").textContent = h.closed ?? 0;
    $("hermesSample").textContent = `${h.decisions ?? 0} decisions · ${h.entries ?? 0} entries · ${h.open_entries ?? 0} open`;
    $("hermesWinRate").textContent = h.closed ? ((h.win_rate || 0) * 100).toFixed(1) + "%" : "—";
    $("hermesPnl").textContent = `Net paper P/L ${fmtMoney(h.total_pnl_usdt || 0)}`;
    $("hermesPnl").classList.toggle("pos", (h.total_pnl_usdt || 0) > 0);
    $("hermesPnl").classList.toggle("neg", (h.total_pnl_usdt || 0) < 0);
    $("hermesReco").textContent = h.recommendation || "Collecting evidence…";

    const models = (h.model_scores || []).slice(0, 5);
    $("hermesModels").innerHTML = models.length ? models.map((m) => `
      <div class="score-row">
        <strong>${m.agent || "AI"}</strong>
        <span>${m.model || "unknown"}</span>
        <em>${((m.accuracy || 0) * 100).toFixed(0)}% · ${m.avg_latency_ms || 0}ms · calls ${m.calls || 0}</em>
      </div>`).join("") : '<div class="empty">No graded trades yet.</div>';

    const strategies = (h.strategy_scores || []).slice(0, 5);
    $("hermesStrategies").innerHTML = strategies.length ? strategies.map((s) => `
      <div class="score-row">
        <strong>${s.name || "strategy"}</strong>
        <span>${s.trades || 0} trades · ${s.wins || 0}W/${s.losses || 0}L</span>
        <em>${((s.win_rate || 0) * 100).toFixed(0)}% · ${fmtMoney(s.net_pnl_usdt || 0)}</em>
      </div>`).join("") : '<div class="empty">No graded trades yet.</div>';

    const lessons = (h.latest_lessons || []).slice(0, 6);
    $("hermesLessons").innerHTML = lessons.length ? lessons.map((l) => `<div class="lesson">${l}</div>`).join("") : '<div class="empty">Hermes will write lessons after trades close.</div>';
  }

  // --- render: demo/live gate ---
  function renderGate(r) {
    if (!r) return;
    state.readiness = r;
    const mode = r.execution_mode || "paper";
    const badge = $("gateBadge");
    if (badge) {
      badge.classList.remove("paper", "demo", "live", "ok");
      const cls = mode === "paper" ? "paper" : mode === "okx_demo" ? "demo" : "live";
      badge.classList.add(cls);
      if (r.can_execute) badge.classList.add("ok");
      badge.textContent = r.can_execute ? `${mode} · ready` : `${mode} · locked`;
    }
    const summary = $("gateSummary");
    if (summary) {
      // V6.3 — the OKX full live gate stays locked even when the tiny
      // tester is armed; surface both states so the operator isn't left
      // wondering why "locked" is shown while a tester order is allowed.
      const lt = r.live_tester || {};
      const testerArmed = (lt.tester_state === "armed") || (lt.allowed && !lt.kill_switch);
      if (mode === "paper") summary.textContent = "Paper mode — OKX execution is disabled. Switch to okx_demo in Settings after the gate passes.";
      else if (r.can_execute && mode === "okx_demo") summary.textContent = "OKX demo gate is OPEN. Demo orders may run (spot only, capped).";
      else if (r.can_execute && mode === "okx_live") summary.textContent = "OKX live gate is OPEN. Real-money orders are SPOT-ONLY and capped by live_max_order_usdt.";
      else if (testerArmed && mode === "okx_live") summary.textContent = `${mode} full live gate is LOCKED, but the V6 tiny tester is ARMED (USDT-capped, override active). Full live remains blocked until the requirements below are satisfied.`;
      else if (testerArmed) summary.textContent = `${mode} gate is LOCKED, but the V6 tiny tester is ARMED. Full execution remains blocked until requirements below are satisfied.`;
      else summary.textContent = `${mode} gate is LOCKED until requirements below are satisfied.`;
    }
    const gate = r.training_gate || {};
    const req = gate.requirements || {};
    // V5.4 — readiness is judged on ELIGIBLE trades only.
    const closedTotal = gate.closed || 0;
    const closedEligible = gate.closed_eligible ?? closedTotal;
    const unverified = gate.unverified_exits || 0;
    const needClosed = req.min_closed_trades || 0;
    $("gateClosed").textContent = `${closedEligible} / ${closedTotal}`;
    $("gateClosed").classList.toggle("pos", closedEligible >= needClosed);
    $("gateClosed").classList.toggle("neg", closedEligible < needClosed);
    $("gateClosedReq").textContent = unverified > 0
      ? `need ${needClosed} eligible — ${unverified} unverified excluded`
      : `need ${needClosed} eligible`;
    // Show the eligible KPI on the portfolio card too.
    const eligEl = $("eligibleClosed");
    if (eligEl) {
      eligEl.textContent = `${closedEligible} / ${closedTotal}`;
      eligEl.classList.toggle("pos", closedEligible > 0 && closedEligible >= needClosed);
      eligEl.classList.toggle("neg", unverified > 0);
    }
    const unvBanner = $("unverifiedExitBanner");
    if (unvBanner) unvBanner.classList.toggle("hidden", unverified <= 0);
    const wr = (gate.win_rate || 0) * 100;
    const needWr = (req.min_win_rate || 0) * 100;
    $("gateWinRate").textContent = `${wr.toFixed(1)}%`;
    $("gateWinRate").classList.toggle("pos", wr >= needWr);
    $("gateWinRate").classList.toggle("neg", wr < needWr);
    $("gateWinReq").textContent = `need ${needWr.toFixed(0)}%`;
    const pnl = gate.total_pnl_usdt || 0;
    $("gatePnl").textContent = fmtMoney(pnl);
    $("gatePnl").classList.toggle("pos", pnl >= (req.min_total_pnl_usdt || 0));
    $("gatePnl").classList.toggle("neg", pnl < (req.min_total_pnl_usdt || 0));
    $("gatePnlReq").textContent = `need >= ${fmtMoney(req.min_total_pnl_usdt || 0)}`;
    const dq = gate.data_quality_score || 0;
    const needDq = req.min_data_quality_score || 0;
    $("gateDQ").textContent = `${dq}/100`;
    $("gateDQ").classList.toggle("pos", dq >= needDq);
    $("gateDQ").classList.toggle("neg", dq < needDq);
    $("gateDQReq").textContent = `need ${needDq}/100`;
    const okx = r.okx || {};
    const okxEl = $("gateOkx");
    let okxText = "not configured";
    if (okx.configured && okx.authenticated) okxText = "authenticated";
    else if (okx.configured) okxText = "configured (not authed)";
    okxEl.textContent = okxText;
    okxEl.classList.toggle("pos", !!okx.authenticated);
    okxEl.classList.toggle("neg", okx.configured && !okx.authenticated);
    $("gateOkxMode").textContent = okx.mode ? `${okx.mode}${okx.demo ? " (demo)" : ""}` : (mode === "paper" ? "paper only" : "—");
    $("gateMaxOrder").textContent = fmtMoney(r.live_max_order_usdt || 0);
    const lev = r.leverage || {};
    if (lev.enabled) {
      $("gateLeverageState").textContent = `ON · ${Number(lev.multiplier || 1).toFixed(1)}× (cap ${Number(lev.max_multiplier || 3).toFixed(0)}×)`;
    } else {
      $("gateLeverageState").textContent = "off (spot only)";
    }
    const reasonsEl = $("gateReasons");
    const reasons = (r.reasons || []).slice(0, 12);
    if (!reasons.length && r.can_execute) {
      reasonsEl.innerHTML = '<li class="ok">All gates open. Execution is permitted within the policy caps.</li>';
    } else if (!reasons.length) {
      reasonsEl.innerHTML = '<li>Awaiting evidence…</li>';
    } else {
      reasonsEl.innerHTML = reasons.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("");
    }
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }

  // --- V6.1: OKX auth diagnostic block ---
  function renderOkxDiagnostic(diag) {
    const block = $("okxDiagBlock");
    if (!block) return;
    if (!diag || (diag.private_auth_ok && !diag.okx_msg && diag.http_status < 400 && !diag.clock_skew_warning)) {
      block.classList.add("hidden");
      return;
    }
    block.classList.remove("hidden");
    const set = (id, txt) => { const el = $(id); if (el) el.textContent = txt; };
    const ok = !!diag.private_auth_ok;
    const badge = $("okxDiagBadge");
    if (badge) {
      badge.classList.remove("chip-ok", "chip-warn", "chip-err");
      badge.classList.add(ok ? "chip-ok" : "chip-err");
      badge.textContent = ok ? "private auth ok" : "private auth failing";
    }
    set("okxDiagHttp", String(diag.http_status || "—"));
    set("okxDiagCode", diag.okx_code || "—");
    set("okxDiagMsg", diag.okx_msg || "—");
    // V6.2 — surface the OKX REST host that was actually hit so the
    // operator can tell at a glance whether OKX_BASE_URL is taking
    // effect (us.okx.com vs www.okx.com vs app.okx.com).
    set("okxDiagBaseUrl", diag.base_url || "—");
    const srcEl = $("okxDiagBaseUrlSrc");
    if (srcEl) {
      if (diag.base_url) {
        srcEl.textContent = diag.base_url_overridden
          ? " (from OKX_BASE_URL env)"
          : " (default \u2014 set OKX_BASE_URL to override)";
      } else {
        srcEl.textContent = "";
      }
    }
    set("okxDiagPath", diag.request_path || "—");
    set("okxDiagTs", diag.timestamp_used || "—");
    set("okxDiagLocal", diag.local_clock_utc || "—");
    set("okxDiagDemoHdr", diag.demo_header_used ? "yes (x-simulated-trading=1)" : "no (live)");
    const skew = diag.clock_skew_ms;
    const skewEl = $("okxDiagSkew");
    if (skewEl) {
      if (skew === null || skew === undefined) {
        skewEl.textContent = "—";
        skewEl.classList.remove("neg", "pos");
      } else {
        skewEl.textContent = `${skew} ms`;
        skewEl.classList.toggle("neg", !!diag.clock_skew_warning);
        skewEl.classList.toggle("pos", !diag.clock_skew_warning);
      }
    }
    const fp = diag.credentials_fingerprint || {};
    const fpTxt = fp.api_key
      ? `key ${fp.api_key} · secret ${fp.api_secret} · passphrase ${fp.passphrase} · demo=${fp.demo}`
      : "—";
    set("okxDiagFp", fpTxt);
    const causes = Array.isArray(diag.likely_causes) ? diag.likely_causes : [];
    const steps = Array.isArray(diag.next_steps) ? diag.next_steps : [];
    const causesEl = $("okxDiagCauses");
    if (causesEl) {
      causesEl.innerHTML = causes.length
        ? causes.map((c) => `<li>${escapeHtml(String(c))}</li>`).join("")
        : '<li class="ok">No specific cause flagged.</li>';
    }
    const stepsEl = $("okxDiagSteps");
    if (stepsEl) {
      stepsEl.innerHTML = steps.length
        ? steps.map((c) => `<li>${escapeHtml(String(c))}</li>`).join("")
        : '<li>—</li>';
    }
  }

  // --- V6: OKX real account panel ---
  function renderOkxAccount(snap) {
    if (!snap) return;
    state.okx_account = snap;
    const set = (id, txt) => { const el = $(id); if (el) el.textContent = txt; };
    set("okxConfigured", snap.configured ? "yes" : "no");
    set("okxAuthed", snap.authenticated ? "yes" : "no");
    const modeEl = $("okxMode");
    if (modeEl) {
      modeEl.textContent = snap.mode || "paper";
      modeEl.classList.toggle("neg", (snap.mode || "paper") === "okx_live");
      modeEl.classList.toggle("pos", (snap.mode || "") === "okx_demo");
    }
    set("okxUsdtTotal", fmtMoney(snap.usdt_total || 0));
    set("okxUsdtFree", fmtMoney(snap.usdt_available || 0));
    set("okxUsdtFrozen", fmtMoney(snap.usdt_frozen || 0));
    set("okxTotalEq", fmtMoney(snap.total_eq_usd || 0));
    set("okxAccountPerms", (snap.permissions || []).join(", ") || "—");
    // V6.2 — show the OKX REST host that this account snapshot used.
    set("okxAccountBaseUrl", snap.base_url || "—");
    // V6.1: surface OKX code/msg inline with the last error when present.
    let lastErr = snap.last_error || "—";
    if (snap.okx_code || snap.okx_msg) {
      lastErr = `code=${snap.okx_code || "—"} · ${snap.okx_msg || lastErr}`;
    }
    set("okxAccountLastError", lastErr);
    const list = $("okxAssetList");
    if (list) {
      const assets = (snap.assets || []).slice(0, 16);
      if (!assets.length) {
        list.innerHTML = '<div class="empty">No non-zero asset balances.</div>';
      } else {
        list.innerHTML = assets.map((a) => `
          <div class="position" data-testid="row-okx-asset-${escapeHtml(a.ccy || "")}">
            <div class="position-head">
              <strong>${escapeHtml(a.ccy || "—")}</strong>
              <span class="muted">${(a.usd_value || 0).toFixed(2)} USD</span>
            </div>
            <div class="position-body">
              total ${Number(a.total || 0).toFixed(8)} · free ${Number(a.available || 0).toFixed(8)} · frozen ${Number(a.frozen || 0).toFixed(8)}
            </div>
          </div>
        `).join("");
      }
    }
  }

  // --- V6: Live Tester panel ---
  function renderLiveTester(t) {
    if (!t) return;
    state.live_tester = t;
    const set = (id, txt) => { const el = $(id); if (el) el.textContent = txt; };
    const enabled = !!t.enabled;
    // V6.3 — the readiness payload now carries a derived lifecycle state
    // (disabled / locked / armed / kill_switch). Prefer it; fall back to
    // the legacy enabled/disabled toggle for older payload shapes.
    const r = state.readiness && state.readiness.live_tester;
    const allowed = !!(r && r.allowed);
    const lifecycle = (r && r.tester_state)
      || (t.kill_switch ? "kill_switch" : (enabled ? (allowed ? "armed" : "locked") : "disabled"));
    const badge = $("liveTesterBadge");
    if (badge) {
      badge.classList.remove("chip-warn", "chip-ok", "chip-err");
      if (lifecycle === "kill_switch" || t.kill_switch) {
        badge.classList.add("chip-err");
        badge.textContent = "kill switch";
      } else if (lifecycle === "armed") {
        badge.classList.add("chip-ok");
        badge.textContent = "armed";
      } else if (lifecycle === "locked" || (enabled && !allowed)) {
        badge.classList.add("chip-warn");
        badge.textContent = "locked";
      } else if (enabled) {
        badge.classList.add("chip-ok");
        badge.textContent = "enabled";
      } else {
        badge.classList.add("chip-warn");
        badge.textContent = "disabled";
      }
    }
    set("ltStatus", lifecycle);
    set("ltMaxOrder", fmtMoney(t.max_order_usdt || 0));
    set("ltDailyCap", fmtMoney(t.daily_loss_cap_usdt || 0));
    // V8 — caps too large warning. Hard-coded against the documented tiny-test
    // safety envelope (max_order_usdt ≤ 5, daily_loss_cap_usdt ≤ 3). Above
    // these thresholds the tester is no longer a "tiny test" — we say so out
    // loud so the operator can dial it back before anything fires.
    const capsBanner = $("ltCapsWarning");
    if (capsBanner) {
      const m = Number(t.max_order_usdt || 0);
      const d = Number(t.daily_loss_cap_usdt || 0);
      const tooBigOrder = m > 5;
      const tooBigLoss = d > 3;
      if (tooBigOrder || tooBigLoss) {
        const parts = [];
        if (tooBigOrder) parts.push(`LIVE_MAX_ORDER_USDT is ${m.toFixed(2)} (tiny-test recommendation: ≤ 5)`);
        if (tooBigLoss) parts.push(`LIVE_DAILY_LOSS_CAP_USDT is ${d.toFixed(2)} (tiny-test recommendation: ≤ 3)`);
        capsBanner.classList.remove("hidden");
        capsBanner.innerHTML = `⚠️ Caps above tiny-test envelope. ${parts.join(" · ")}. Lower the caps in <code>.env</code> or the settings drawer before enabling live mode.`;
      } else {
        capsBanner.classList.add("hidden");
        capsBanner.textContent = "";
      }
    }
    set("ltTradesToday", `${t.trades_today || 0} / ${t.max_trades_per_day || 0}`);
    set("ltRealizedToday", fmtMoney(t.realized_pnl_today || 0));
    set("ltOpenCount", `${t.open_position_count || 0} / ${t.max_open_positions || 0}`);
    set("ltStopMode", t.stop_mode || "bot_managed");
    // V9 — dynamic native-protection banner. Reflects the env flag plus
    // the configured mode. Per-position state is shown in the position card.
    {
      const npEnabled = !!t.native_protection_enabled;
      const npMode = String(t.native_protection_mode || "oco").toLowerCase();
      const npDry = !!t.native_protection_dry_run;
      const stateEl = $("ltNativeProtState");
      const modeEl = $("ltNativeProtMode");
      const copyEl = $("ltNativeProtCopy");
      const banner = $("ltNativeProtBanner");
      if (stateEl) stateEl.textContent = npEnabled ? "ENABLED" : "DISABLED";
      if (modeEl) modeEl.textContent = npEnabled
        ? `(mode: ${npMode}${npDry ? ", DRY-RUN" : ""})`
        : "";
      if (copyEl) {
        copyEl.textContent = npEnabled
          ? "After a buy fills, the tester additionally places OCO/conditional sell algos on OKX so triggers fire exchange-side even if this bot stops. Bot-managed exits remain armed in parallel — they are NOT replaced. If native placement fails, the bot watcher is the only safety net; do not exit the process while a live position is open."
          : "All exits are bot-managed. If this process stops, exits will not trigger. Set LIVE_NATIVE_PROTECTION_ENABLED=true (and configure LIVE_NATIVE_PROTECTION_MODE) in .env to additionally attach OKX-native TP/SL.";
      }
      if (banner) {
        // Visual emphasis: green-ish when native is enabled & not dry-run.
        if (npEnabled && !npDry) {
          banner.style.borderLeft = "3px solid var(--ok, #34d399)";
          banner.style.background = "rgba(52, 211, 153, 0.06)";
        } else {
          banner.style.borderLeft = "3px solid var(--warn)";
          banner.style.background = "rgba(245,158,11,0.06)";
        }
      }
    }
    const killLine = $("ltKillSwitchLine");
    if (killLine) {
      killLine.classList.toggle("hidden", !t.kill_switch);
      const rEl = $("ltKillReason");
      if (rEl) rEl.textContent = t.kill_switch_reason || "manual";
    }
    const overrideLine = $("ltOverrideLine");
    if (overrideLine) overrideLine.classList.toggle("hidden", !t.override_active);
    // V6.3 — distinguish "armed and waiting" from "locked" so the panel
    // stops reading as a failure state when the tester is actually ready.
    // V8 — the override token explicitly suppresses the training-gate copy
    // (see live_tester.gate_check). If the override is active AND the tester
    // is in the ARMED lifecycle we make that completely unambiguous in the UI.
    const heading = $("ltReasonsHeading");
    if (heading) {
      if (lifecycle === "armed") {
        heading.textContent = t.override_active
          ? "ARMED (override): waiting for qualified LONG spot signal"
          : "Live tester status";
      } else {
        heading.textContent = "Why the live tester is locked";
      }
    }
    // Build the reasons list.
    const reasonsEl = $("ltReasons");
    const reasons = (r && r.blocked_reasons) || [];
    const readyMsg = (r && r.ready_message) || "Tiny tester armed; waiting for a qualified signal.";
    if (reasonsEl) {
      const list = Array.isArray(reasons) ? reasons : [];
      if (lifecycle === "armed") {
        reasonsEl.innerHTML = `<li class="ok">${escapeHtml(readyMsg)}</li>`;
      } else if (!list.length && enabled && !t.kill_switch) {
        reasonsEl.innerHTML = '<li class="ok">Live tester gate is open. Tiny entries are permitted within caps.</li>';
      } else if (!list.length) {
        reasonsEl.innerHTML = '<li>Set LIVE_TESTER_ENABLED=true and provide OKX credentials to begin.</li>';
      } else {
        reasonsEl.innerHTML = list.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("");
      }
    }
    // Positions — V7: surface BOT-MANAGED protection prominently with TP/SL/trail values.
    const posEl = $("ltPositions");
    if (posEl) {
      const open = t.open_positions || [];
      if (!open.length) {
        posEl.innerHTML = '<div class="empty">No live tester positions.</div>';
      } else {
        posEl.innerHTML = open.map((p) => {
          const slPx = p.stop_loss_price ? Number(p.stop_loss_price).toFixed(6) : "—";
          const tpPx = p.take_profit_price ? Number(p.take_profit_price).toFixed(6) : "—";
          const trail = p.trailing_stop_pct ? `${Number(p.trailing_stop_pct).toFixed(2)}%` : "off";
          const entry = Number(p.avg_entry_price || 0);
          const slPct = (entry > 0 && p.stop_loss_price > 0) ? ((entry - p.stop_loss_price) / entry * 100).toFixed(2) : null;
          const tpPct = (entry > 0 && p.take_profit_price > 0) ? ((p.take_profit_price - entry) / entry * 100).toFixed(2) : null;
          const stale = p.needs_reconcile ? '<span class="chip chip-warn" title="OKX did not return fills yet — bot will not re-enter until reconciled.">needs reconcile</span>' : "";
          // V6.4 — every live-tester open position is a spot LONG.
          const ltSide = String(p.side || "long").toUpperCase();
          const ltSideCls = ltSide === "SHORT" ? "side-badge side-short" : "side-badge side-long";
          // V7 — Protection mode badge. Always bot_managed in V6/V7.
          const sl_mode = p.sl_mode || "bot_managed";
          const protBadge = sl_mode === "bot_managed"
            ? '<span class="chip chip-warn" title="Exits are watched and submitted by THIS bot, not by OKX. OKX order history will show TP/SL as blank. Bot must remain running.">PROT: BOT-MANAGED</span>'
            : `<span class="chip chip-ok">PROT: ${escapeHtml(sl_mode)}</span>`;
          // V9 — protection status matrix. Now considers the OKX native
          // algo state in addition to bot-managed exits.
          //   EXCHANGE-NATIVE ACTIVE       : native algo placed and live on OKX
          //   EXCHANGE-NATIVE PENDING      : native placement in flight
          //   NATIVE FAILED – BOT FALLBACK : native rejected; bot-managed still armed
          //   BOT-MANAGED ACTIVE           : bot exits armed, no native attempted
          //   UNPROTECTED                  : no SL/TP/trail and no native — critical
          const hasBotSL = !!p.stop_loss_price;
          const hasBotTP = !!p.take_profit_price;
          const hasTrail = !!p.trailing_stop_pct;
          const isOpen = (p.status === "open" || p.status === "needs_reconcile");
          const np = (p.native_protection || {});
          const npStatus = (np.status || "none").toLowerCase();
          const npEnabled = !!np.enabled;
          let v9ProtStatus, v9ProtCls;
          if (npEnabled && npStatus === "active") {
            v9ProtStatus = "EXCHANGE-NATIVE ACTIVE";
            v9ProtCls = "chip-ok";
          } else if (npEnabled && npStatus === "dry_run") {
            v9ProtStatus = "EXCHANGE-NATIVE DRY-RUN";
            v9ProtCls = "chip-warn";
          } else if (npEnabled && npStatus === "pending") {
            v9ProtStatus = "EXCHANGE-NATIVE PENDING";
            v9ProtCls = "chip-warn";
          } else if (npEnabled && npStatus === "failed") {
            v9ProtStatus = "NATIVE FAILED – BOT FALLBACK";
            v9ProtCls = "chip-err";
          } else if (hasBotSL || hasBotTP || hasTrail) {
            v9ProtStatus = "BOT-MANAGED ACTIVE";
            v9ProtCls = "chip-warn";
          } else if (isOpen) {
            v9ProtStatus = "UNPROTECTED";
            v9ProtCls = "chip-err";
          } else {
            v9ProtStatus = "BOT-MANAGED MISSING";
            v9ProtCls = "chip-warn";
          }
          const v8ProtBadge = `<span class="chip ${v9ProtCls}" title="V9 protection status">${v9ProtStatus}</span>`;
          // Algo id strip — only render when we have ids to show.
          const algoOco = np.oco_algo_id && np.oco_algo_id !== "DRY_RUN" ? np.oco_algo_id : "";
          const algoTp = np.tp_algo_id || "";
          const algoSl = np.sl_algo_id || "";
          let nativeIdsHtml = "";
          if (npEnabled && (algoOco || algoTp || algoSl)) {
            const parts = [];
            if (algoOco) parts.push(`<span class="pill-mono" title="OCO algoId on OKX">OCO: ${escapeHtml(algoOco)}</span>`);
            if (algoTp) parts.push(`<span class="pill-mono" title="TP algoId on OKX">TP: ${escapeHtml(algoTp)}</span>`);
            if (algoSl) parts.push(`<span class="pill-mono" title="SL algoId on OKX">SL: ${escapeHtml(algoSl)}</span>`);
            nativeIdsHtml = `<div class="v7-strip" style="margin-top:4px;">${parts.join(" ")}</div>`;
          }
          // V9 — OKX-side inventory snapshot for this base ccy. Shows the
          // free/frozen split + any open sells / algos OKX still has on the
          // book. Helps the user spot "DOGE frozen 100" without a separate
          // OKX tab. Snapshot is best-effort — absent = backend didn't probe yet.
          const inv = p.okx_inventory || null;
          let inventoryHtml = "";
          if (inv && (inv.free !== undefined || inv.frozen !== undefined || (inv.open_sells || []).length || (inv.open_algos || []).length)) {
            const free = Number(inv.free || 0);
            const frozen = Number(inv.frozen || 0);
            const total = Number(inv.total || free + frozen);
            const sells = Array.isArray(inv.open_sells) ? inv.open_sells.length : 0;
            const algos = Array.isArray(inv.open_algos) ? inv.open_algos.length : 0;
            const ccy = escapeHtml(String(inv.base_ccy || p.base_ccy || ""));
            inventoryHtml = `<div class="v7-strip" style="margin-top:4px;font-size:11px;" title="OKX-side balance snapshot for this base currency.">
              <span class="pill-mono">OKX ${ccy}: total ${total.toFixed(6)}</span>
              <span class="pill-mono">free ${free.toFixed(6)}</span>
              <span class="pill-mono${frozen > 0 ? " chip-warn" : ""}">frozen ${frozen.toFixed(6)}</span>
              <span class="pill-mono">open sells: ${sells}</span>
              <span class="pill-mono">open algos: ${algos}</span>
            </div>`;
          }
          // V9.1 — inventory-mismatch warning. If the backend's stale-journal
          // repair populated inventory_sellable_qty (because journal qty > OKX
          // total), surface a yellow chip explaining the bot will use the OKX
          // inventory for exits, not the original journal qty. PnL cost basis
          // (filled_qty + avg_entry_price) is preserved on the position.
          const invSellable = (p.inventory_sellable_qty !== undefined && p.inventory_sellable_qty !== null)
            ? Number(p.inventory_sellable_qty) : null;
          const grossQty = (p.gross_filled_qty !== undefined && p.gross_filled_qty !== null)
            ? Number(p.gross_filled_qty) : Number(p.filled_qty || 0);
          const sellableQty = (p.sellable_qty !== undefined && p.sellable_qty !== null)
            ? Number(p.sellable_qty) : grossQty;
          const feeDeductedBase = Number(p.fee_deducted_from_base || 0);
          const baseCcyDisp = escapeHtml(String(p.base_ccy || (inv && inv.base_ccy) || ""));
          let inventoryMismatchHtml = "";
          if (invSellable !== null && invSellable > 0 && sellableQty > invSellable * 1.001) {
            const repairNote = escapeHtml(String(p.inventory_repair_note || ""));
            inventoryMismatchHtml = `<div class="v7-prot-warn" style="border-color:var(--warning);color:var(--warning);background:rgba(250,204,21,0.08);margin-top:6px;">
              ⚠️ <strong>Journal quantity exceeds OKX inventory; using sellable balance for exits.</strong>
              Journal sellable: <code>${sellableQty.toFixed(8)}</code> &middot;
              OKX inventory: <code>${invSellable.toFixed(8)}</code>${repairNote ? ` &middot; <span class="muted small">${repairNote}</span>` : ""}
            </div>`;
          }
          // V9 — environment_warnings chips (e.g. "existing-algo-warn-other",
          // "adopted-existing-algo", "frozen-balance-detected"). These are
          // soft warnings the backend attaches when it spots non-fatal
          // conditions during reconcile/probe.
          const envWarns = Array.isArray(np.environment_warnings) ? np.environment_warnings : [];
          let envWarnsHtml = "";
          if (envWarns.length) {
            envWarnsHtml = `<div class="v7-strip" style="margin-top:4px;">` + envWarns.map((w) =>
              `<span class="chip chip-warn" title="OKX environment warning">${escapeHtml(String(w))}</span>`
            ).join(" ") + `</div>`;
          }
          let nativeErrHtml = "";
          if (npEnabled && npStatus === "failed" && (np.last_error_code || np.last_error_msg)) {
            nativeErrHtml = `<div class="v7-prot-warn" style="border-color:var(--danger);color:var(--danger);background:rgba(248,113,113,0.08);margin-top:6px;">
              <strong>Native protection failed</strong> — OKX code <code>${escapeHtml(String(np.last_error_code || "-"))}</code>: ${escapeHtml(String(np.last_error_msg || "unknown"))}. Bot-managed exits remain armed.
            </div>`;
          } else if (npEnabled && npStatus === "dry_run") {
            nativeErrHtml = `<div class="v7-prot-warn" style="margin-top:6px;"><strong>Native protection: DRY RUN</strong> — LIVE_NATIVE_PROTECTION_DRY_RUN=true. No order-algo call was sent to OKX.</div>`;
          }
          const unprotectedBanner = (v9ProtStatus === "UNPROTECTED")
            ? `<div class="v7-prot-warn" style="border-color:var(--danger);color:var(--danger);background:rgba(248,113,113,0.08);">
                  🚨 <strong>UNPROTECTED LIVE POSITION</strong>: no stop-loss, no take-profit, no trailing stop is recorded for this position.
                  Close manually on OKX or engage the kill switch immediately. The bot has no exit plan for this position.
               </div>`
            : "";
          return `
            <div class="position lt-position" data-testid="row-lt-position-${escapeHtml(p.symbol || "")}">
              <div class="position-head">
                <strong>${escapeHtml(p.symbol || "—")}
                  <span class="${ltSideCls}" title="Direction: ${ltSide} (OKX spot buy). Open live position.">${ltSide}</span>
                  ${v8ProtBadge} ${protBadge} ${stale}
                </strong>
                <span class="muted small">${escapeHtml(p.status || "open")}</span>
              </div>
              ${unprotectedBanner}
              ${inventoryMismatchHtml}
              <div class="lt-pos-grid">
                <div><span class="v7-strip-label">Gross qty</span><strong title="Base asset filled by OKX before any base-asset fee deduction. Preserves PnL cost basis.">${grossQty.toFixed(8)}</strong></div>
                <div><span class="v7-strip-label">Sellable qty</span><strong title="Journal qty available to sell after base-asset fee deduction. Exits clamp this against OKX free balance.">${sellableQty.toFixed(8)}${invSellable !== null && invSellable > 0 && invSellable < sellableQty ? ` <span class="muted small" title="OKX inventory is lower; exits will use ${invSellable.toFixed(8)}">(OKX: ${invSellable.toFixed(8)})</span>` : ""}</strong></div>
                <div><span class="v7-strip-label">Entry</span><strong>${Number(p.avg_entry_price || 0).toFixed(6)}</strong></div>
                <div><span class="v7-strip-label">Quote</span><strong>$${Number(p.quote_spent_usdt || p.requested_quote_usdt || 0).toFixed(2)}</strong></div>
                <div><span class="v7-strip-label">Fee deducted</span><strong>${feeDeductedBase > 0 ? `${feeDeductedBase.toFixed(8)} ${baseCcyDisp || escapeHtml(String(p.entry_fee_ccy || ""))}` : (Number(p.entry_fee || 0) > 0 ? `${Number(p.entry_fee).toFixed(8)} ${escapeHtml(String(p.entry_fee_ccy || "USDT"))}` : "—")}</strong></div>
                <div><span class="v7-strip-label">Stop loss</span><strong class="v7-bad">${slPx}${slPct ? ` <span class="muted small">(−${slPct}%)</span>` : ""}</strong></div>
                <div><span class="v7-strip-label">Take profit</span><strong class="v7-good">${tpPx}${tpPct ? ` <span class="muted small">(+${tpPct}%)</span>` : ""}</strong></div>
                <div><span class="v7-strip-label">Trailing</span><strong>${trail}</strong></div>
              </div>
              ${nativeIdsHtml}
              ${inventoryHtml}
              ${envWarnsHtml}
              ${nativeErrHtml}
              <div class="v7-prot-warn">
                ${(npEnabled && npStatus === "active")
                  ? `✅ <strong>Native TP/SL is live on OKX.</strong> Triggers fire even if this bot stops. Bot-managed watcher also remains armed for trailing stops and edge-case exits.`
                  : (npEnabled && (npStatus === "failed" || npStatus === "pending"))
                    ? `⚠️ Native exchange protection is <strong>${escapeHtml(npStatus)}</strong>. Bot-managed exits are the active safety net — <strong>the bot must remain running</strong>.`
                    : `⚠️ Exits are <strong>not attached to the OKX order</strong>. OKX order history will show <code>TP | SL</code> as blank. The bot watches price every tick and submits the exit order itself — <strong>the bot must remain running</strong>. Enable <code>LIVE_NATIVE_PROTECTION_ENABLED=true</code> in .env to additionally attach exchange-side OCO sells.`}
              </div>
            </div>
          `;
        }).join("");
      }
    }

    // V7 — Last live order panel + reconciliation drift detector.
    renderLastLiveOrder(t);
    renderLiveDriftWarning(t);
    // V9 — unattended readiness panel + recent events feed.
    renderUnattendedReadiness(t);
    renderLiveEvents(t);
  }

  // V9 — unattended-mode readiness banner. The list of checks is emitted
  // by live_tester.summary().unattended_readiness.checks; the banner copy
  // follows the verbatim user spec ("SAFE-ish for unattended tiny test" /
  // "DO NOT LEAVE UNATTENDED").
  function renderUnattendedReadiness(t) {
    const panel = $("ltUnattendedPanel");
    if (!panel) return;
    const u = (t && t.unattended) || {};
    const ready = (t && t.unattended_readiness) || {};
    // Hide the panel completely when unattended mode is OFF. The operator
    // doesn't need a 12-row checklist when they're sitting in front of the bot.
    if (!u.enabled) {
      panel.classList.add("hidden");
      return;
    }
    panel.classList.remove("hidden");
    const bannerEl = $("ltUnattendedBanner");
    const timerEl = $("ltUnattendedTimer");
    const noteEl = $("ltUnattendedNote");
    const checksEl = $("ltUnattendedChecks");
    const overallPass = !!ready.overall_pass && !u.expired;
    if (bannerEl) bannerEl.textContent = ready.banner
      || (overallPass ? "SAFE-ish for unattended tiny test" : "DO NOT LEAVE UNATTENDED");
    // Border / background colour reflects pass/fail.
    panel.style.borderLeft = overallPass ? "3px solid var(--ok, #34d399)" : "3px solid var(--danger, #f87171)";
    panel.style.background = overallPass ? "rgba(52,211,153,0.06)" : "rgba(248,113,113,0.06)";
    // Timer: "started 02:13 · expires 2026-05-31 18:00 UTC · ~3.4h left"
    if (timerEl) {
      if (u.started_at && u.expires_at) {
        const exp = new Date(Number(u.expires_at) * 1000);
        const now = Date.now() / 1000;
        const leftH = (Number(u.expires_at) - now) / 3600;
        const expStr = exp.toISOString().replace("T", " ").slice(0, 16) + " UTC";
        if (u.expired) {
          timerEl.textContent = `EXPIRED at ${expStr}`;
        } else if (leftH < 0) {
          timerEl.textContent = `expires ${expStr} · expired`;
        } else {
          timerEl.textContent = `expires ${expStr} · ~${leftH.toFixed(1)}h left`;
        }
      } else {
        timerEl.textContent = `max ${Number(u.max_hours || 0)}h — not yet armed`;
      }
    }
    if (noteEl) {
      noteEl.textContent = ready.note || "Risk-reduced tiny live test envelope. This is not safe automation.";
    }
    if (checksEl) {
      const checks = Array.isArray(ready.checks) ? ready.checks : [];
      if (!checks.length) {
        checksEl.innerHTML = '<li>No readiness checks reported.</li>';
      } else {
        checksEl.innerHTML = checks.map((c) => {
          const ok = !!c.pass;
          const cls = ok ? "ok" : "";
          const icon = ok ? "✅" : "❌";
          const id = escapeHtml(String(c.id || "check"));
          const msg = escapeHtml(String(c.message || (ok ? "pass" : "fail")));
          // Per-position sub-list (used by native_verified_for_open).
          let sub = "";
          if (Array.isArray(c.per_position) && c.per_position.length) {
            sub = `<ul style="margin:4px 0 4px 18px;">` + c.per_position.map((pp) => {
              const pOk = !!pp.pass;
              return `<li class="${pOk ? "ok" : ""}">${pOk ? "✅" : "❌"} <code>${escapeHtml(String(pp.position_id || pp.symbol || "?"))}</code> — ${escapeHtml(String(pp.message || ""))}</li>`;
            }).join("") + `</ul>`;
          }
          return `<li class="${cls}">${icon} <strong>${id}</strong> — ${msg}${sub}</li>`;
        }).join("");
      }
    }
  }

  // V9 — recent live-tester event log (rolling 50 entries from the
  // backend). Used to surface placed/triggered/cancelled and kill-switch
  // / unattended-armed lifecycle entries without scrolling server logs.
  function renderLiveEvents(t) {
    const el = $("ltEvents");
    if (!el) return;
    const evs = (t && Array.isArray(t.events)) ? t.events : [];
    if (!evs.length) {
      el.innerHTML = '<div class="empty">No events recorded yet.</div>';
      return;
    }
    // Newest first; cap at 20 rows for the UI.
    const rows = evs.slice(-20).reverse().map((e) => {
      const ts = e.ts ? new Date(Number(e.ts) * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "—";
      const kind = String(e.kind || "event");
      const msg = String(e.message || "");
      let chipCls = "chip";
      if (kind.includes("failed") || kind === "kill_switch_engaged" || kind === "unattended_expired") chipCls = "chip chip-err";
      else if (kind.includes("placed") || kind === "live_order_opened" || kind === "unattended_armed") chipCls = "chip chip-ok";
      else if (kind.includes("triggered") || kind === "live_exit_fired") chipCls = "chip chip-warn";
      return `<div class="qsig-row" style="gap:8px;align-items:center;">
        <span class="${chipCls}">${escapeHtml(kind)}</span>
        <span class="muted small mono">${escapeHtml(ts)}</span>
        <span>${escapeHtml(msg)}</span>
      </div>`;
    }).join("");
    el.innerHTML = rows;
  }

  function renderLastLiveOrder(t) {
    const el = $("ltLastOrder");
    if (!el) return;
    const attempts = (t && t.attempts) || [];
    if (!attempts.length) {
      el.innerHTML = '<div class="empty">No live order attempts yet this session.</div>';
      return;
    }
    const a = attempts[attempts.length - 1];
    const ok = !!a.ok;
    const phase = a.phase || (ok ? "opened" : "refused");
    const cls = ok ? "chip-ok" : "chip-warn";
    const filled = Number(a.filled_qty || 0);
    const avgPx = Number(a.avg_px || 0);
    const fee = Number(a.fee || 0);
    const tsLine = a.ts ? new Date(a.ts * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "—";
    const reasons = a.reasons || (a.reason ? [a.reason] : []);
    const needsRec = !!a.needs_reconcile;
    // Determine whether protective watcher is alive: at least one OPEN live position exists with sl_mode=bot_managed.
    const protectiveActive = !!(t && (t.open_positions || []).some((p) =>
      (p.status === "open" || p.status === "needs_reconcile") && (p.sl_mode || "bot_managed") === "bot_managed"
    ));
    el.innerHTML = `
      <div class="qsig-row">
        <strong>${escapeHtml(a.symbol || "—")}</strong>
        <span class="chip ${cls}">${escapeHtml(phase)}</span>
        ${needsRec ? '<span class="chip chip-warn">needs reconcile</span>' : ""}
        ${protectiveActive ? '<span class="chip chip-ok" title="Bot is watching price ticks and will submit exit orders.">protective watcher: ACTIVE</span>'
                          : '<span class="chip chip-warn" title="No open bot-managed position — nothing to watch. If you still have a position on OKX, see the drift warning below.">protective watcher: idle</span>'}
      </div>
      <div class="lt-pos-grid">
        <div><span class="v7-strip-label">Filled qty</span><strong>${filled.toFixed(6)}</strong></div>
        <div><span class="v7-strip-label">Avg fill</span><strong>${avgPx ? avgPx.toFixed(6) : "—"}</strong></div>
        <div><span class="v7-strip-label">Fee</span><strong>${fee.toFixed(6)} ${escapeHtml(String(a.entry_fee_ccy || a.fee_ccy || "USDT"))}</strong></div>
        <div><span class="v7-strip-label">Sent</span><strong>$${Number(a.intended_quote_usdt || 0).toFixed(2)}</strong></div>
        <div><span class="v7-strip-label">OKX order id</span><strong class="mono small">${escapeHtml(a.okx_order_id || "—")}</strong></div>
        <div><span class="v7-strip-label">Time</span><strong class="mono small">${tsLine}</strong></div>
      </div>
      ${reasons.length ? `<ul class="reasons-list compact">${reasons.slice(0, 6).map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>` : ""}
    `;
  }

  function renderLiveDriftWarning(t) {
    const el = $("ltDriftAlert");
    if (!el) return;
    // Heuristic V7 drift detector: if OKX account shows holdings of a non-USDT base ccy but
    // we have no matching open bot-managed position, alert the user. We can’t prove ownership
    // from balance alone, but a positive base balance with NO matching watcher is risky
    // because the bot will not exit it.
    const acct = state.okx_account || {};
    const assets = acct.assets || acct.balances || [];
    const open = (t && t.open_positions) || [];
    const openBases = new Set(open.map((p) => (p.base_ccy || "").toUpperCase()).filter(Boolean));
    const orphans = [];
    for (const a of assets) {
      const ccy = String(a.ccy || a.currency || "").toUpperCase();
      const total = Number(a.total || a.balance || 0);
      if (!ccy || ccy === "USDT" || total <= 0) continue;
      // Only warn if there is enough value to matter (e.g. > 0.01 of base unit — deliberately
      // loose; the warning is informational, not blocking).
      if (!openBases.has(ccy)) orphans.push(`${ccy} (${total})`);
    }
    if (!orphans.length) {
      el.classList.add("hidden");
      el.innerHTML = "";
      return;
    }
    el.classList.remove("hidden");
    el.innerHTML = `
      <strong>⚠️ Possible unmanaged live holding.</strong>
      OKX shows non-USDT balance with no matching bot-managed live position
      &mdash; the bot will <em>not</em> watch or exit these holdings.
      Affected: <code>${orphans.map(escapeHtml).join(", ")}</code>.
      If these came from the live tester, restart the bot to attempt reconciliation,
      or close manually on OKX. If they predate the bot, ignore.
    `;
  }

  // ============================================================
  // V7 — Scanner Activity, Why-No-Trade, Market Intel, Pro Header
  // ============================================================
  function _ago(ts) {
    if (!ts) return "never";
    const s = Math.max(0, Math.round(ts));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    return Math.floor(s / 3600) + "h ago";
  }

  function renderScannerActivity(d) {
    if (!d) return;
    state.scanner = d;
    const set = (id, txt) => { const el = $(id); if (el != null) el.textContent = txt; };

    set("scanScannedCount", `${d.scanned_count || 0} / ${d.max_scan_symbols || 0}`);
    set("scanCleanCount", String(d.clean_count || 0));
    set("scanSkippedCount", String(d.skipped_count || 0));
    set("scanLastAge", d.last_scan_age_seconds != null ? _ago(d.last_scan_age_seconds) : "never");
    set("scanDuration", d.last_scan_duration_ms ? `${Math.round(d.last_scan_duration_ms)} ms` : "—");
    set("scanTotal", `${d.scans_total || 0} scans`);
    set("scanWarming", d.warming ? "warming…" : "primed");
    set("scanQueueText", d.rate_limited
      ? `OKX rate-limit cooldown (${Math.round(d.rate_limit_remaining_seconds || 0)}s left)`
      : (d.warming ? "priming cache…" : "queue idle"));

    // V8 — next scan ETA and universe size. The backend ScannerActivity
    // snapshot exposes next_scan_eta_seconds + total_universe_size; if a
    // build does not yet emit them we fall back to scan_interval_seconds.
    let etaTxt = "—";
    const etaRaw = (d.next_scan_eta_seconds != null) ? Number(d.next_scan_eta_seconds) : null;
    if (etaRaw != null) {
      etaTxt = (etaRaw <= 0) ? "now" : (etaRaw < 60 ? `${Math.round(etaRaw)}s` : `${Math.floor(etaRaw / 60)}m ${Math.round(etaRaw % 60)}s`);
    } else if (d.scan_interval_seconds) {
      etaTxt = `every ${d.scan_interval_seconds}s`;
    }
    set("scanNextEta", etaTxt);
    const universeSize = (d.total_universe_size != null)
      ? d.total_universe_size
      : ((d.core_symbols || []).length + (d.discovered_symbols || []).length);
    const capN = Number(d.max_scan_symbols || 0);
    set("scanUniverseSize", capN ? `${universeSize} / ${capN}` : String(universeSize));

    // Universe chips
    const coreEl = $("scanCoreList");
    if (coreEl) coreEl.textContent = (d.core_symbols || []).join(", ") || "—";
    const discEl = $("scanDiscoveredList");
    if (discEl) discEl.textContent = (d.discovered_symbols || []).join(", ") || "—";

    // Top candidates table
    const candEl = $("scanCandidates");
    if (candEl) {
      const rows = d.top_candidates || [];
      if (!rows.length) {
        candEl.innerHTML = '<div class="empty">No ranked candidates this scan.</div>';
      } else {
        candEl.innerHTML = `
          <table class="pro-table">
            <thead><tr><th>Symbol</th><th>Strategy</th><th class="num">Score</th><th class="num">Conf</th><th>Signal</th><th>Status</th></tr></thead>
            <tbody>${rows.map((r) => {
              const cls = (r.rejected_reason || r.status === "skipped") ? "row-warn" : "row-ok";
              const signal = String(r.signal || "—").toLowerCase();
              const sigBadge = signal === "buy" ? '<span class="side-badge side-long" title="Spot buy (LONG)">LONG</span>'
                : (signal === "sell" ? '<span class="side-badge side-short" title="Short unavailable">SHORT n/a</span>' : '<span class="muted">—</span>');
              const status = r.rejected_reason ? escapeHtml(r.rejected_reason) : (r.status || "eligible");
              return `<tr class="${cls}">
                <td><strong>${escapeHtml(r.symbol || "—")}</strong></td>
                <td class="muted">${escapeHtml(r.strategy || r.strategy_id || "—")}</td>
                <td class="num">${Number(r.score || 0).toFixed(3)}</td>
                <td class="num">${Number(r.confidence || 0).toFixed(2)}</td>
                <td>${sigBadge}</td>
                <td class="muted">${escapeHtml(status)}</td>
              </tr>`;
            }).join("")}</tbody>
          </table>`;
      }
    }

    // Skipped table
    const skipEl = $("scanSkipped");
    if (skipEl) {
      const skipped = d.skipped_symbols || {};
      const keys = Object.keys(skipped);
      if (!keys.length) {
        skipEl.innerHTML = '<div class="empty">No symbols skipped this scan.</div>';
      } else {
        skipEl.innerHTML = `<ul class="reasons-list compact">${keys.slice(0, 12).map((k) =>
          `<li><strong>${escapeHtml(k)}</strong> — <span class="muted">${escapeHtml(String(skipped[k]))}</span></li>`).join("")}</ul>`;
      }
    }

    // Last qualified signal
    const qsEl = $("scanQualifiedSignal");
    if (qsEl) {
      const qs = d.last_qualified_signal;
      if (!qs) {
        qsEl.innerHTML = '<div class="empty">No qualified signal yet this session.</div>';
      } else {
        qsEl.innerHTML = `
          <div class="qsig-row"><strong>${escapeHtml(qs.symbol || "—")}</strong>
            <span class="side-badge side-long">LONG</span>
            <span class="muted">· ${escapeHtml(qs.strategy_name || qs.strategy_id || "—")}</span>
          </div>
          <div class="qsig-row muted small">
            setup: ${escapeHtml(qs.setup_type || "—")} · score ${Number(qs.score || 0).toFixed(3)}
            · conf ${Number(qs.confidence || 0).toFixed(2)} · mode ${escapeHtml(qs.mode || "—")}
          </div>
          <div class="qsig-row muted small">reasons: ${(qs.reasons || []).map(escapeHtml).join("; ") || "—"}</div>`;
      }
    }

    // Last live attempt.
    // V9 — fallback: if the scanner-activity payload doesn't include
    // last_live_attempt (older payloads, or scanner hasn't recorded one),
    // fall back to the last entry in live_tester.attempts so the panel
    // doesn't go stale.
    const laEl = $("scanLastLiveAttempt");
    if (laEl) {
      let la = d.last_live_attempt;
      if (!la && state.live_tester && Array.isArray(state.live_tester.attempts) && state.live_tester.attempts.length) {
        la = state.live_tester.attempts[state.live_tester.attempts.length - 1];
      }
      if (!la) {
        laEl.innerHTML = '<div class="empty">No live tester order attempts yet this session.</div>';
      } else {
        const ok = !!la.ok;
        const cls = ok ? "chip chip-ok" : "chip chip-warn";
        const reasons = la.reasons || (la.reason ? [la.reason] : []);
        laEl.innerHTML = `
          <div class="qsig-row"><strong>${escapeHtml(la.symbol || "—")}</strong>
            <span class="${cls}">${ok ? "submitted" : "refused"}</span>
            <span class="muted">· phase: ${escapeHtml(la.phase || "—")}</span>
          </div>
          ${reasons.length ? `<ul class="reasons-list compact">${reasons.slice(0, 6).map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>` : ""}`;
      }
    }

    // "Why no live trade?" blockers panel
    renderBlockers(d.blockers || {});

    // No-trade summary banner
    const summary = d.last_no_trade_reason || "";
    const banner = $("scanNoTradeSummary");
    if (banner) {
      if (summary) {
        banner.textContent = summary;
        banner.classList.remove("hidden");
      } else {
        banner.classList.add("hidden");
      }
    }
  }

  function renderBlockers(b) {
    const el = $("blockerList");
    if (!el) return;
    const order = [
      "full_live_gate", "tiny_live_tester", "no_qualified_signal",
      "existing_position", "confidence_below_threshold",
      "cooldown", "daily_caps", "insufficient_balance",
    ];
    const items = order.map((k) => {
      const row = b[k] || {};
      const blocked = !!row.blocked;
      const dot = blocked ? "dot-red" : "dot-green";
      const status = blocked ? "blocked" : "clear";
      let detail = "";
      if (k === "full_live_gate" || k === "tiny_live_tester" || k === "daily_caps") {
        const arr = row.reasons || [];
        detail = arr.length ? arr.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("") : "";
        if (k === "tiny_live_tester" && row.state) {
          detail = `<li class="muted">state: ${escapeHtml(row.state)}</li>` + detail;
        }
      } else if (k === "existing_position") {
        const arr = row.symbols || [];
        detail = arr.length ? `<li>${arr.map(escapeHtml).join(", ")}</li>` : "";
      } else if (k === "confidence_below_threshold") {
        detail = `<li>current ${row.current ?? "—"} · required ≥ ${row.required ?? "—"}</li>`;
      } else if (k === "no_qualified_signal" || k === "cooldown" || k === "insufficient_balance") {
        detail = row.reason ? `<li>${escapeHtml(row.reason)}</li>` : "";
      }
      return `<div class="blocker-row ${blocked ? 'is-blocked' : 'is-clear'}">
        <div class="blocker-head">
          <span class="blocker-dot ${dot}"></span>
          <span class="blocker-label">${escapeHtml(row.label || k)}</span>
          <span class="blocker-status">${status}</span>
        </div>
        ${detail ? `<ul class="reasons-list compact">${detail}</ul>` : ''}
      </div>`;
    });
    el.innerHTML = items.join("");
  }

  async function refreshScanner() {
    try {
      const j = await api("/api/scanner");
      renderScannerActivity(j.scanner || {});
      // Market intel source
      const mi = j.market_intel || {};
      const provEl = $("miProvider");
      if (provEl) provEl.textContent = String(mi.provider || "none");
      const noteEl = $("miNote");
      if (noteEl) noteEl.textContent = String(mi.note || "");
    } catch (e) {
      console.warn("scanner refresh failed", e);
    }
  }

  // Auto-refresh scanner every 4s as a safety net even if SSE drops.
  setInterval(refreshScanner, 4000);

  async function refreshOkxAccount() {
    try {
      const j = await api("/api/okx/account");
      renderOkxAccount(j.account || {});
      // V6.1: auto-render the diagnostic block whenever the account
      // endpoint includes one (i.e. configured but not authenticated).
      if (j && j.diagnostic) {
        renderOkxDiagnostic(j.diagnostic);
      } else if (j.account && j.account.authenticated) {
        const block = $("okxDiagBlock");
        if (block) block.classList.add("hidden");
      }
    } catch (e) {
      console.warn("okx account fetch failed", e);
    }
  }
  async function runOkxDiagnostic() {
    const btn = $("testOkxAuthBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
    try {
      const j = await api("/api/okx/diagnostics");
      renderOkxDiagnostic(j.diagnostic || {});
      // Refresh balance afterwards in case auth now works.
      await refreshOkxAccount();
    } catch (e) {
      console.warn("okx diagnostics failed", e);
      renderOkxDiagnostic({
        private_auth_ok: false,
        okx_msg: String(e && e.message || e),
        likely_causes: ["Browser could not reach /api/okx/diagnostics."],
        next_steps: ["Confirm the backend is running and reachable."],
      });
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Test OKX private auth"; }
    }
  }
  async function refreshLiveTester() {
    try {
      const j = await api("/api/live/tester");
      renderLiveTester(j.tester || {});
    } catch (e) {
      console.warn("live tester fetch failed", e);
    }
  }

  // --- render: trades ---
  function renderTrades(trades) {
    const tbody = $("tradesBody");
    if (!trades?.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">No closed trades yet.</td></tr>';
      return;
    }
    tbody.innerHTML = trades.map((t) => {
      const cls = t.pnl_usdt >= 0 ? "pos" : "neg";
      const dt = new Date(t.closed_ts * 1000).toLocaleTimeString();
      // V6.4 — uppercase side so the long/short column is unmistakable in the
      // log; cramped lowercase 'long' was easy to miss in the V6.3 screenshot.
      // V8 — trades always label as LONG (spot buy) in the spot-only build so
      // we never show a bare ambiguous "BUY" in the trade log. If a future
      // build emits short trades, render those distinctly.
      const rawTradeSide = String(t.side || "long").toLowerCase();
      const tSide = rawTradeSide === "short"
        ? "SHORT"
        : rawTradeSide === "buy"
          ? "LONG (spot buy)"
          : rawTradeSide.toUpperCase() === "LONG"
            ? "LONG (spot buy)"
            : rawTradeSide.toUpperCase();
      const tSideCls = rawTradeSide === "short" ? "side-badge side-short" : "side-badge side-long";
      return `<tr>
        <td><strong>${t.symbol}</strong></td>
        <td><span class="${tSideCls}">${tSide}</span></td>
        <td>${fmtNum(t.qty, 6)}</td>
        <td>${fmtNum(t.entry_price, 4)}</td>
        <td>${fmtNum(t.exit_price, 4)}</td>
        <td class="pnl ${cls}">${fmtMoney(t.pnl_usdt)}</td>
        <td>${fmtMoney(t.fees_paid)}</td>
        <td>${t.exit_reason}</td>
        <td>${dt}</td>
      </tr>`;
    }).join("");
  }

  // --- event console ---
  const consoleEl = $("console");
  function logEvent(ev) {
    const now = new Date(ev.ts * 1000).toLocaleTimeString();
    const line = document.createElement("div");
    const payload = typeof ev.data === "object" ? JSON.stringify(ev.data).slice(0, 220) : String(ev.data);
    line.innerHTML = `<span class="ev-time">${now}</span>  <span class="ev-type">${ev.type}</span>  ${payload}`;
    consoleEl.prepend(line);
    while (consoleEl.children.length > 80) consoleEl.removeChild(consoleEl.lastChild);
  }

  // --- V5.1 renderers ---
  function renderAutoStrategy(d) {
    if (!d) return;
    state.autoStrategy = d;
    const chip = $("autoStrategyChip");
    const stateEl = $("autoStrategyState");
    const reasonEl = $("autoStrategyReason");
    const bestEl = $("bestCandidate");
    if (stateEl) {
      const on = !!d.enabled;
      stateEl.textContent = on ? ("ON → " + (d.selected || "—")) : "OFF (manual)";
      if (chip) {
        chip.classList.toggle("is-on", on);
        chip.setAttribute("aria-pressed", on ? "true" : "false");
      }
    }
    if (reasonEl) reasonEl.textContent = d.reason || "—";
    if (bestEl) {
      const b = d.best_candidate;
      if (b && b.symbol) {
        const score = (b.score !== undefined && b.score !== null) ? Number(b.score).toFixed(2) : "—";
        bestEl.textContent = `${b.symbol} · ${b.strategy || "—"} · score ${score}` +
                             (b.note ? `  (${b.note})` : "");
      } else {
        bestEl.textContent = "—";
      }
    }
  }

  function renderUniverse(d) {
    if (!d) return;
    state.universe = d;
    const countEl = $("universeCount");
    const discEl = $("universeDiscovered");
    const cleanEl = $("universeClean");
    const skippedEl = $("universeSkipped");
    const skippedDetailEl = $("universeSkippedDetail");
    const scanned = Array.isArray(d.scanned) ? d.scanned : (Array.isArray(d.symbols) ? d.symbols : []);
    if (countEl) countEl.textContent = String(d.total || scanned.length || "—");
    if (discEl) {
      const discovered = Array.isArray(d.discovered) ? d.discovered : [];
      if (discovered.length === 0) {
        discEl.textContent = d.discovery_enabled === false ? "discovery disabled" : "none yet — OKX tickers warming up";
      } else {
        const shown = discovered.slice(0, 8).join(", ");
        const extra = discovered.length > 8 ? ` +${discovered.length - 8} more` : "";
        discEl.textContent = `${discovered.length} dynamic (${shown}${extra})`;
      }
    }
    // V5.2 — clean/skipped diagnostics
    const clean = Array.isArray(d.clean) ? d.clean : [];
    const skipped = (d.skipped && typeof d.skipped === "object") ? d.skipped : {};
    const skippedKeys = Object.keys(skipped);
    if (cleanEl) cleanEl.textContent = String(clean.length || "—");
    if (skippedEl) skippedEl.textContent = String(skippedKeys.length);
    if (skippedDetailEl) {
      if (skippedKeys.length === 0) {
        skippedDetailEl.textContent = d.rate_limited ? "(OKX rate-limit cooldown)" : "";
      } else {
        const preview = skippedKeys.slice(0, 4).map(k => `${k}: ${skipped[k]}`).join(" · ");
        const extra = skippedKeys.length > 4 ? ` (+${skippedKeys.length - 4} more)` : "";
        const rl = d.rate_limited ? " [rate-limited]" : "";
        skippedDetailEl.textContent = `— ${preview}${extra}${rl}`;
      }
    }
  }

  function renderEntryKind(d) {
    if (!d) return;
    state.entryKind = d;
    const chip = $("entryKindChip");
    const label = $("entryKindLabel");
    if (!chip || !label) return;
    chip.classList.remove("is-on", "is-training", "is-sample");
    const kind = (d.kind || "").toString();
    let txt = "none yet";
    if (kind === "standard") {
      txt = `STANDARD · ${d.symbol || "—"}`;
      chip.classList.add("is-on");
    } else if (kind === "training") {
      txt = `TRAINING · ${d.symbol || "—"}`;
      chip.classList.add("is-training");
    } else if (kind === "learning_sample") {
      txt = `LEARNING SAMPLE · ${d.symbol || "—"}`;
      chip.classList.add("is-sample");
    }
    label.textContent = txt;
  }

  // --- SSE wiring ---
  function connectSSE() {
    const es = new EventSource(API_BASE + "/api/stream");
    const types = ["ready", "task", "portfolio", "tickers", "snapshots", "opportunities", "ai_verdict",
                   "position_opened", "position_closed", "entry_blocked", "bot_started",
                   "bot_stopped", "paper_reset", "position_managed", "settings_updated", "market_status",
                   "data_quality", "hermes_updated", "error",
                   // V5.1
                   "universe", "auto_strategy", "entry_kind", "strategy_mode",
                   // V5.4
                   "exit_blocked", "symbol_profile",
                   // V6 — live tester
                   "live_tester", "live_tester_blocked", "live_tester_attempt",
                   "live_kill_switch", "live_order_opened", "live_order_failed",
                   "live_exit_failed", "live_order_closed",
                   // V7 — scanner observability
                   "scanner_activity"];
    types.forEach((t) => {
      es.addEventListener(t, (e) => {
        try {
          const ev = JSON.parse(e.data);
          logEvent(ev);
          handleEvent(ev);
        } catch (err) { /* noop */ }
      });
    });
    es.onerror = () => {
      // Auto-reconnect after 3s
      es.close();
      setTimeout(connectSSE, 3000);
    };
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case "portfolio": renderPortfolio(ev.data); break;
      case "tickers":
        state.tickers = ev.data || {};
        renderMarket();
        if (state.portfolio) renderPortfolio(state.portfolio);
        break;
      case "snapshots":
        state.snapshots = ev.data || {};
        renderMarket();
        break;
      case "opportunities":
        renderOpportunities(ev.data || []);
        break;
      case "data_quality":
        state.hermes = state.hermes || {};
        state.hermes.data_quality = ev.data || {};
        renderHermes(state.hermes);
        break;
      case "hermes_updated":
        renderHermes(ev.data || {});
        // Hermes changes invalidate the gate; cheap refresh.
        refreshReadiness();
        break;
      case "exit_blocked":
        // V5.4 — surface impossible-fill events; gate already excludes them.
        try {
          const d = ev.data || {};
          const msg = `Paper exit blocked on ${d.symbol || ""}: ${d.why || ""}`;
          $("currentTask") && ($("currentTask").textContent = msg);
        } catch (_) { /* noop */ }
        break;
      case "symbol_profile":
        // Store live profiles for inspection; the opportunity card already
        // shows the short summary inside reasons.
        state.symbol_profiles = (ev.data && ev.data.profiles) || {};
        break;
      case "ai_verdict": renderAgents(ev.data); break;
      case "task":
        $("currentTask").textContent = ev.data.text || "—";
        $("taskDot").classList.remove("idle", "error");
        if ((ev.data.text || "").toLowerCase().startsWith("error")) $("taskDot").classList.add("error");
        if ((ev.data.text || "") === "idle" || (ev.data.text || "") === "stopped") $("taskDot").classList.add("idle");
        break;
      case "market_status":
        state.health = state.health || {};
        state.health.market_status = ev.data;
        renderPills();
        break;
      case "strategy_mode":
        state.health = state.health || {};
        state.health.settings = state.health.settings || {};
        if (ev.data?.active_strategy) state.health.settings.active_strategy = ev.data.active_strategy;
        renderStrategies();
        break;
      case "settings_updated":
        state.health = state.health || {};
        state.health.settings = ev.data || {};
        renderStrategies();
        refreshReadiness();
        renderPills();
        break;
      case "position_opened":
      case "position_closed":
      case "position_managed":
      case "paper_reset":
        refreshPortfolio();
        refreshTrades();
        break;
      case "universe":
        renderUniverse(ev.data || {});
        break;
      case "auto_strategy":
        renderAutoStrategy(ev.data || {});
        break;
      case "entry_kind":
        renderEntryKind(ev.data || {});
        break;
      // V6 — live tester events
      case "live_tester":
        renderLiveTester(ev.data || {});
        break;
      // V7 — scanner activity stream
      case "scanner_activity":
        renderScannerActivity(ev.data || {});
        break;
      case "live_tester_blocked":
      case "live_tester_attempt":
        // Either is informational; refresh tester + readiness panels.
        refreshLiveTester();
        refreshReadiness();
        try {
          const d = ev.data || {};
          const sym = d.symbol || "";
          const why = d.reason || d.status || ev.type;
          $("currentTask") && ($("currentTask").textContent = `live tester ${sym} ${why}`);
        } catch (_) { /* noop */ }
        break;
      case "live_kill_switch":
      case "live_order_failed":
      case "live_exit_failed":
        refreshLiveTester();
        refreshOkxAccount();
        refreshReadiness();
        try {
          const d = ev.data || {};
          $("currentTask") && ($("currentTask").textContent =
            `LIVE ${ev.type}: ${d.symbol || ""} ${d.reason || d.error || ""}`);
        } catch (_) { /* noop */ }
        break;
      case "live_order_opened":
      case "live_order_closed":
        refreshLiveTester();
        refreshOkxAccount();
        break;
    }
  }

  // --- actions ---
  async function refreshHealth() {
    state.health = await api("/api/health");
    state.strategies = state.health.strategies || state.strategies;
    if (state.health.readiness) renderGate(state.health.readiness);
    renderPills();
    renderStrategies();
    // Apply bot running state to buttons
    const running = state.health.bot?.running;
    $("startBtn").disabled = !!running;
    $("stopBtn").disabled = !running;
  }
  async function refreshReadiness() {
    try {
      const r = await api("/api/live/readiness");
      renderGate(r);
      renderPills();
      // Keep V6 panels in sync with the readiness snapshot.
      if (r && r.okx_account) renderOkxAccount(r.okx_account);
      if (r && r.live_tester && r.live_tester.summary) {
        renderLiveTester(r.live_tester.summary);
      }
    } catch (e) {
      console.warn("readiness fetch failed", e);
    }
  }
  async function refreshPortfolio() {
    const p = await api("/api/portfolio");
    renderPortfolio(p);
  }
  async function refreshTrades() {
    const { trades } = await api("/api/trades");
    renderTrades(trades);
  }
  async function refreshHermes() {
    const h = await api("/api/hermes");
    renderHermes(h);
  }

  $("startBtn").addEventListener("click", async () => {
    await api("/api/bot/start", { method: "POST" });
    refreshHealth();
  });
  $("stopBtn").addEventListener("click", async () => {
    await api("/api/bot/stop", { method: "POST" });
    refreshHealth();
  });
  $("findBtn").addEventListener("click", async () => {
    $("currentTask").textContent = "Find best trade now…";
    await api("/api/bot/find-best", { method: "POST" });
  });
  $("resetBtn").addEventListener("click", async () => {
    if (!confirm("Reset the paper account? Cash, P/L, and trade log will be cleared.")) return;
    await api("/api/paper/reset", { method: "POST" });
    refreshPortfolio();
    refreshTrades();
  });
  const refreshBtn = $("refreshReadiness");
  if (refreshBtn) refreshBtn.addEventListener("click", () => refreshReadiness());

  // V6 — live tester + OKX account controls
  const refreshAccountBtn = $("refreshAccountBtn");
  if (refreshAccountBtn) refreshAccountBtn.addEventListener("click", () => refreshOkxAccount());
  const testOkxAuthBtn = $("testOkxAuthBtn");
  if (testOkxAuthBtn) testOkxAuthBtn.addEventListener("click", () => runOkxDiagnostic());
  const ltRefreshBtn = $("ltRefreshBtn");
  if (ltRefreshBtn) ltRefreshBtn.addEventListener("click", () => {
    refreshLiveTester();
    refreshOkxAccount();
    refreshReadiness();
  });
  const ltKillBtn = $("ltKillBtn");
  if (ltKillBtn) ltKillBtn.addEventListener("click", async () => {
    if (!confirm("Engage the live tester kill switch? All future live entries will be blocked until you release it.")) return;
    try {
      const reason = prompt("Reason for engaging kill switch?", "manual") || "manual";
      await api("/api/live/tester/kill", { method: "POST", body: JSON.stringify({ reason }) });
      refreshLiveTester();
      refreshReadiness();
      $("currentTask").textContent = "live tester kill switch ENGAGED";
    } catch (e) {
      $("currentTask").textContent = "kill switch failed: " + (e && e.message || e);
    }
  });
  const ltReleaseBtn = $("ltReleaseBtn");
  if (ltReleaseBtn) ltReleaseBtn.addEventListener("click", async () => {
    if (!confirm("Release the kill switch? This re-enables tiny live entries within the configured caps.")) return;
    try {
      await api("/api/live/tester/release", { method: "POST" });
      refreshLiveTester();
      refreshReadiness();
      $("currentTask").textContent = "live tester kill switch RELEASED";
    } catch (e) {
      $("currentTask").textContent = "release failed: " + (e && e.message || e);
    }
  });
  $("resetHermesBtn").addEventListener("click", async () => {
    if (!confirm("Reset Hermes learning journal? This clears model/strategy learning history, but not your paper account.")) return;
    const { hermes } = await api("/api/hermes/reset", { method: "POST" });
    renderHermes(hermes);
  });
  $("strategySelect").addEventListener("change", async (e) => {
    await setStrategy(e.target.value);
  });

  async function setStrategy(id) {
    if (!id) return;
    await api("/api/settings", { method: "POST", body: JSON.stringify({ active_strategy: id }) });
    await refreshHealth();
    $("currentTask").textContent = `strategy armed: ${id}`;
  }

  // V5.3 — clickable Auto Strategy chip toggles auto_strategy_selection live
  const autoChip = document.getElementById("autoStrategyChip");
  if (autoChip) {
    autoChip.addEventListener("click", async () => {
      const s = state.health && state.health.settings;
      const current = !!(s && s.auto_strategy_selection);
      const next = !current;
      autoChip.setAttribute("disabled", "disabled");
      try {
        const resp = await api("/api/settings", {
          method: "POST",
          body: JSON.stringify({ auto_strategy_selection: next }),
        });
        state.health = state.health || {};
        state.health.settings = resp.settings || state.health.settings || {};
        if (state.health.settings) state.health.settings.auto_strategy_selection = next;
        renderAutoStrategyChip();
        renderPills();
        $("currentTask").textContent = `auto strategy: ${next ? "ON" : "OFF"}`;
      } catch (err) {
        $("currentTask").textContent = "auto-strategy toggle failed: " + (err && err.message || err);
      } finally {
        autoChip.removeAttribute("disabled");
      }
    });
  }

  // --- settings drawer ---
  const drawer = $("drawer");
  function openDrawer() {
    populateSettingsForm(state.health?.settings);
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
  }
  function closeDrawer() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  }
  $("settingsBtn").addEventListener("click", openDrawer);
  $("closeDrawer").addEventListener("click", closeDrawer);
  $("drawerScrim").addEventListener("click", closeDrawer);

  function populateSettingsForm(s) {
    if (!s) return;
    for (const k of Object.keys(s)) {
      const el = document.getElementById("set-" + k);
      if (!el) continue;
      if (typeof s[k] === "boolean") el.value = s[k] ? "true" : "false";
      else if (Array.isArray(s[k])) el.value = s[k].join(",");
      else el.value = s[k];
    }
  }

  $("saveSettings").addEventListener("click", async () => {
    const fields = [
      "starting_balance_usdt", "risk_per_trade_pct", "max_position_pct",
      "stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
      "daily_loss_cap_pct", "max_trades_per_day", "max_open_positions",
      "fee_pct", "slippage_pct", "confidence_threshold", "timeframe",
      "scan_interval_seconds", "ai_timeout_seconds", "ai_max_tokens",
      "bias_timeframe", "price_refresh_seconds", "ride_winners_enabled",
      "active_strategy",
      "commander_model", "scout_model", "risk_model", "skeptic_model",
      "indicator_only_mode",
      // V5 — demo/live gate
      "execution_mode", "live_min_closed_trades", "live_min_win_rate",
      "live_min_total_pnl_usdt", "live_min_data_quality_score", "live_max_order_usdt",
      // V5 — leverage simulator
      "leverage_enabled", "leverage_multiplier", "leverage_max_multiplier",
      "leverage_liquidation_buffer_pct", "leverage_max_daily_loss_pct",
      "leverage_extra_min_closed_trades",
      // V5.1 — active paper training
      "active_paper_training", "paper_training_min_score",
      "paper_training_allow_exploration", "paper_training_size_pct",
      "paper_training_max_daily_trades",
      // V5.1 — auto strategy + dynamic discovery
      "auto_strategy_selection",
      "dynamic_symbol_discovery", "max_dynamic_symbols",
      "dynamic_min_quote_volume_usdt",
      // V5.1 — learning samples
      "learning_sample_enabled", "learning_sample_min_score",
      "learning_sample_size_pct", "learning_sample_max_per_day",
      // V5.4 — realistic paper mode
      "paper_profile", "min_cash_reserve_pct", "max_capital_in_positions_pct",
      "global_trade_cooldown_seconds", "per_symbol_cooldown_seconds",
      "realistic_fills_enabled", "exit_verification_enabled",
      "symbol_adaptive_enabled",
      "symbol_min_atr_pct", "symbol_max_atr_pct",
      "symbol_max_spread_pct", "symbol_min_quote_vol_usdt",
      "paper_excluded_bases",
      // V6 — live tester
      "live_tester_enabled", "live_max_order_usdt_tester",
      "live_daily_loss_cap_usdt", "live_max_trades_per_day",
      "live_one_position_per_symbol", "live_max_open_positions",
      "live_require_protective_exit", "live_spot_only",
      "live_free_reserve_multiplier",
      // V7 — scanner universe ceiling + optional market-intel provider
      "max_scan_symbols", "market_intel_provider",
    ];
    const patch = {};
    for (const f of fields) {
      const el = document.getElementById("set-" + f);
      if (!el) continue;
      patch[f] = el.type === "number" ? Number(el.value) : (el.value === "true" ? true : el.value === "false" ? false : el.value);
    }
    await api("/api/settings", { method: "POST", body: JSON.stringify(patch) });
    closeDrawer();
    refreshHealth();
    refreshReadiness();
  });

  // --- clock ---
  function tickClock() {
    const d = new Date();
    $("clockPill").textContent = d.toISOString().slice(11, 19) + " UTC";
  }
  setInterval(tickClock, 1000); tickClock();

  // --- boot ---
  (async function init() {
    try {
      await refreshHealth();
      await refreshPortfolio();
      await refreshTrades();
      await refreshHermes();
      await refreshReadiness();
      // V6 — hydrate OKX account + live tester panels
      await refreshOkxAccount();
      await refreshLiveTester();
      await refreshScanner();
      const m = await api("/api/market");
      state.tickers = m.tickers || {};
      if (m.quality && state.hermes) {
        state.hermes.data_quality = m.quality;
        renderHermes(state.hermes);
      }
      renderMarket();
      renderPills();
    } catch (e) {
      $("currentTask").textContent = "boot error: " + e.message;
    }
    connectSSE();
    // Polling fallbacks for when SSE is unavailable (e.g. Vercel)
    setInterval(refreshHealth, 30000);
    setInterval(refreshPortfolio, 10000);
    setInterval(refreshTrades, 30000);
    setInterval(refreshHermes, 60000);
    setInterval(refreshReadiness, 30000);
  })();
})();

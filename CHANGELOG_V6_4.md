# V6.4 — UI Direction Labels (Spot LONG-only clarity hotfix)

UI-only hotfix on top of V6.3. **No trading-logic changes. No new live risk.**
This release does not add real short trading, margin, perps, leverage,
borrowing, or withdrawals. It only makes the existing spot LONG behavior
unambiguous in the dashboard.

## Symptom this release fixes

After V6.3 the operator looked at the dashboard, saw an open DOGE position
and an AI verdict of `WAIT`, and could not tell whether the bot was trading
LONG, trading SHORT, or paralyzed. Nothing on the page said the word "long"
or "short" anywhere a reasonable person would look — the AI committee card
in particular *looked like* a verdict on the DOGE position (it is not; it
is a vote on a *candidate* setup for the next entry), and the open-position
strip and opportunity radar cards had no Direction field at all.

This eroded trust: the operator's reasonable read was "the bot only ever
trades long" — which is **correct for this build** but had to be inferred.

## What changed (UI only)

1. **Paper portfolio → open positions** now render an explicit `LONG`
   badge next to each symbol. Tooltip says `Direction: LONG (spot buy)
   · open paper position`. Backed by the existing `Position.side` field;
   no new field on the position payload.
2. **Opportunity radar** cards now render a `Candidate direction:` line.
   Long candidates show `LONG (spot buy)`. If the bias is bearish and
   the card was `AVOID`, the card additionally shows
   `SHORT unavailable in this spot-only build` so the asymmetry is
   visible rather than implied. Backend `_build_opportunities` now emits
   `side: "long"`, `candidate_side: "long"`, `shorts_available: false`
   on every row.
3. **AI brain committee** card now labels itself as a *candidate setup*
   with a LONG badge next to the symbol and a standing explanation line:
   *"Candidate direction: LONG (spot buy). The committee is voting on
   whether to OPEN this setup — it is not a verdict on any already-open
   position."* The verdict payload now carries `side: "long"`,
   `candidate_side: "long"`, `is_candidate: true`.
4. **Live Tester card** carries a new banner: *"Allowed direction: LONG
   only (OKX spot buy). Shorts require a separate margin/perps adapter
   and are not enabled in this build. The tester will never sell what
   you do not already hold."* Each open live-tester position also shows
   a LONG badge.
5. **Control deck** carries a small standing note under the Current
   task line: *"Allowed direction: LONG only (OKX spot buy). This
   build does not short, borrow, use margin, perps, leverage, or
   withdrawals. Bearish signals are avoided/watched, never sold short."*
6. **Opportunity radar header** gains a one-line scan-scope banner:
   *"Long-spot scan only. This build scans for LONG (spot buy) setups;
   bearish signals are avoided/watched, not shorted."*
7. **Trade log** now renders the `Side` column as an uppercase
   color-coded badge (`LONG` / `SHORT`) with extra horizontal padding so
   the column is impossible to miss. The trade dataclass already had
   `side`; the previous build rendered it lowercase and cramped.

## What did NOT change

- No new strategies. No new endpoints (the existing `side` fields are
  reused).
- No change to OKX adapters, paper engine fill logic, live tester
  gating, risk caps, kill switch, or readiness math.
- No real short-selling, margin, perps, leverage, borrowing, or
  withdrawals are introduced. The `live_spot_only` flag remains the
  source of truth and is still enforced upstream of any order.
- No new env variables. Existing `.env.example` is annotated only.
- No data model migration. `data/settings.json` is forward/backward
  compatible.

## Files changed

- `app/services/ai_brain.py` — verdict payload gains `side`,
  `candidate_side`, `is_candidate`.
- `app/services/strategy.py` — opportunity rows gain `side`,
  `candidate_side`, `shorts_available`.
- `web/app.js` — open position rows, live-tester position rows, trade
  log rows, opportunity cards, and AI committee headline all surface the
  side via a `side-badge` element. Trade log uppercases `side`.
- `web/index.html` — adds the Control-deck direction note, the
  Opportunity-radar long-only banner, the AI-committee candidate-direction
  line (`#consensusCandidateLine`), and the Live-Tester allowed-direction
  banner.
- `web/styles.css` — adds `.side-badge`, `.side-long`, `.side-short`,
  `.side-na`, `.opp-side`, `.direction-note`, `.radar-note-direction`,
  `.consensus-note`, `.banner-direction`, plus Side-column padding for
  the trade log.
- `.env.example` — comment-only annotations on the live-tester block to
  re-state that this build is spot-LONG-only.
- `CHANGELOG_V6_4.md` — this file.

## How to verify in the running app

1. Start the app and open the dashboard.
2. **Control deck** shows a green "Allowed direction: LONG only" note
   under "Current task".
3. **Paper portfolio** rows: every open position has a green `LONG`
   pill next to the symbol.
4. **AI brain committee**: symbol heading shows a `LONG` pill and
   `candidate setup`; a paragraph below the consensus row explains
   the committee is voting on a *candidate*, not on an existing
   position.
5. **Opportunity radar**: each row has a `Candidate direction:` line.
   Bearish-avoid rows show both `LONG (spot buy)` and the
   `SHORT unavailable in this spot-only build` chip.
6. **Live Tester**: a green banner says
   *"Allowed direction: LONG only (OKX spot buy)."* Any open live
   position also has a `LONG` pill.
7. **Trade log**: the `Side` column shows large uppercase
   `LONG` / `SHORT` badges with breathing room.

## Tests run before packaging

```
python -m compileall -q app
node --check web/app.js
```

Both must succeed before producing the V6.4 zip.

## Windows install steps (from a fresh extract of the zip)

1. Unzip
   `AI_HUMMINGBOT_BRAIN_V6_4_DIRECTION_LABELS_SPOT_LONG_ONLY.zip` into a
   folder, e.g. `C:\ai-hummingbot-brain`.
2. Open PowerShell in that folder.
3. `python -m venv .venv` then `.\.venv\Scripts\Activate.ps1`.
4. `pip install -r requirements.txt`.
5. Copy `.env.example` to `.env` and fill in your OKX keys + AI keys as
   in V6.3. **No new env vars in V6.4.**
6. Start the app the same way you started V6.3:
   `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.
7. Open `http://127.0.0.1:8000/` in a browser. You should now see all
   seven direction labels listed under "How to verify" above.

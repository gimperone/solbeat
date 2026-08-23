# ⚡ SolBeat — Autonomous Solana Ecosystem Reporter

A comprehensive, automatically-updating report on the state of the Solana ecosystem.
**Zero API keys. Zero pip dependencies.** Pure Python 3 stdlib + public Solana RPC +
keyless public APIs (DeFiLlama, CoinGecko, RSS).

Built for the Superteam Canada bounty *"Develop Solana Ecosystem Auto-Updating Report &
Interactive Dashboard"*.

---

## What you get

| Output | Path | Description |
|---|---|---|
| 🖥️ Interactive dashboard | `dashboard/index.html` served by `solbeat.py run` | Dark-theme live UI: KPIs, SVG trend charts, validator table, news, upgrades roadmap, source health |
| 📄 Markdown report | `reports/report.md` | Human-readable, regenerated after every collection round |
| 🔢 Machine-readable JSON | `reports/report.json` | Structured payload (schema_version 1.0) for downstream automation |
| 🗂️ Sample outputs | `samples/` | Committed examples of generated MD + JSON |

## Quick start

```bash
# requirements: Python 3.9+ (nothing else)
python solbeat.py run          # collectors scheduler + dashboard on http://localhost:7801
python solbeat.py collect      # one collection round, print metrics, exit
python solbeat.py report       # regenerate reports/report.md + .json, exit
python solbeat.py serve --port 8000   # serve UI/API without background collection
```

Configuration lives in `config.json` (intervals, thresholds, feeds, RPC endpoints).
No virtualenv, no pip install, no `.env`, no keys.

## Data sources & integration strategy

| Source | What we extract | Why this source |
|---|---|---|
| **Solana JSON-RPC** (`api.mainnet-beta.solana.com`, failover list in config) | `getHealth`, `getVersion`, `getEpochInfo` (epoch, progress), `getSlot` (cross-checked against epoch info — divergence is itself a metric), `getRecentPerformanceSamples` (TPS total/non-vote, avg slot time), `getVoteAccounts` (active/delinquent validators, stake, commission, epoch credits -> Nakamoto coefficient, top-10 share, commission distribution), `getSupply` (circulating SOL), `getBlockHeight`+`getBlocks`+`getBlock` (median/mean fees + DAA estimator), `getBlockTime` (wall-clock block-time cross-check vs perf-sample estimate), `getSignaturesForAddress` (top-DEX program activity freshness: Jupiter signatures/min + lag) | Direct on-chain truth; covers every method listed in the brief except `getBalance` (intentional: no required metric needs a single-account balance; documented decision) |
| **DeFiLlama** (`api.llama.fi`, `stablecoins.llama.fi`) | Chain TVL, DEX volume 24h, chain fees 24h (**REV proxy**), stablecoin supply on Solana (path-style endpoint `/stablecoincharts/solana`; the `?chain=` query form silently returns global totals — verified and documented), RWA/tokenized-asset TVL | Keyless, reliable, no auth |
| **CoinGecko** simple price | SOL price, 24h change, market cap | Keyless free tier |
| **RSS/Atom feeds** (Cointelegraph, CoinDesk, Agave releases) | Ecosystem/community news filtered by keyword list, deduplicated in SQLite | Replaces Twitter/Dune (both require keys → excluded per the brief's no-keys preference) |

### Methodology notes (honesty first)

- **REV proxy**: exact REV (priority fees + MEV tips) is not available keyless. We use
  DeFiLlama's 24h chain fees as a documented lower-bound proxy.
- **Daily Active Addresses**: estimated by sampling N recent blocks (default 6,
  configurable), collecting unique fee payers, and scaling by slots-per-day ÷ sampled
  slot span. Labeled `est_*` everywhere; bounded RPC load and hard 45s time budget.
- **Tokenized assets**: approximated by summing TVL of DeFiLlama protocols tagged RWA
  active on Solana; per-asset volume is not available keyless.
- Every derived number that is not a raw measurement carries an `est_` prefix or is
  flagged in the report footer.

## Automation strategy

1. `Orchestrator` schedules each source independently (intervals in `config.json`:
   RPC 60s, markets 300s, news 900s, DAA estimator 1800s).
2. Sources run in parallel threads; each failure is isolated — the source's health
   row flips to `error` in SQLite and the dashboard shows a red chip, while all other
   data keeps flowing. No single upstream outage can kill the report.
3. Every successful tick appends metrics to SQLite and writes a full snapshot;
   reports are regenerated automatically after rounds.
4. Retention: snapshots 7 days, alerts 3 days, news 14 days (self-cleaning DB).

### Fully automated deployments (zero-cost)

`.github/workflows/report.yml` runs on a schedule: it collects fresh data headlessly,
regenerates `report.md`/`report.json`, commits them back and publishes the dashboard +
reports to **GitHub Pages** via Actions. That gives a continuously updating hosted
version without owning any server — satisfying the "live demo" bonus with $0 infra.

## Anomaly detection

Two complementary engines (`core/anomaly.py`):

1. **Absolute thresholds** (config): slot time warn ≥600ms / crit ≥1000ms;
   delinquent stake warn >1% / crit >5% of stake.
2. **Statistical**: rolling z-score over stored history (window configurable,
   default 120 points, 3σ trigger) for TPS spikes/drops and median-fee spikes, plus
   percentage-move checks between consecutive samples for SOL price (±15%), DeFi TVL
   (±10%) and stablecoin supply (±5%). Move detection is regression-tested against
   the production evaluation order (record -> evaluate).
3. **Multi-source correlation** (innovation): CoinGecko vs Binance price divergence
   (warn >1.5%), `getSlot` vs `getEpochInfo` slot divergence (two RPC sources of
   truth), perf-sample vs `getBlockTime` wall-clock block time — disagreement between
   independent sources is itself an anomaly signal.

Alerts are persisted, surfaced at the top of the dashboard and in the Markdown
report header. Example caught during development: the stablecoin-supply fix from a
mis-scoped API response was instantly flagged as `-94.7% move`.

## Interpreting the report

- **Network performance** — TPS total vs non-vote separates real economic activity
  from consensus traffic; slot time ≈367ms indicates healthy pacing (nominal ~400ms).
- **Validators** — stake share of top validators shows Nakamoto-coefficient pressure;
  median commission tracks the fee market for delegation; any `⚠ delinquent` row plus
  the delinquency % alerts indicate network-health issues.
- **Economic indicators** — TVL + DEX volume + fees together approximate real demand;
  stablecoin supply growth signals liquidity inflows/outflows independent of price.
- **Alerts** — anything red needs human attention before relying on other numbers.

## Project layout

```
solbeat.py               CLI entrypoint + orchestrator + HTTP server
config.json              intervals, thresholds, feeds, endpoints (no secrets!)
core/util.py             keyless HTTP/RPC client w/ retry + failover
core/store.py            SQLite: metrics history, snapshots, alerts, source health, news dedup
core/anomaly.py          thresholds + z-score engine + multi-source correlation rules
core/report.py           Markdown + JSON generators (+ embedded chart history)
collectors/rpc_collector.py   network, validators, supply, fees/DAA sampler, program activity
collectors/market.py     DeFiLlama + CoinGecko + Binance cross-check
collectors/news.py       namespace-agnostic RSS/Atom parser + keyword filter
dashboard/index.html     overview page (self-contained dark UI, hand-rolled SVG charts)
dashboard/validators.html dedicated validators page (sortable table, concentration charts, CSV export)
.github/workflows/report.yml  scheduled regeneration + Pages publish
tests/                   stdlib unittest suite (anomaly order regression, collectors, utils)
```

## Design principles

- **Stdlib-only**: anyone can run it anywhere in seconds; nothing to compromise.
- **Graceful degradation**: partial data beats no data; every metric is optional.
- **Honest estimates**: methodology inline; no invented precision.
- **Self-documenting ops**: the dashboard shows its own ingestion health.

## Limitations & future work

- Public RPC rate limits → keep intervals ≥60s or point `rpc_endpoints` at your own node.
- Exact REV and per-asset tokenized-equity volume would need keyed APIs (Dune/Twitter
  adapters are intentionally out of scope per the no-keys constraint; the collector
  registry makes adding them trivial if policy changes).
- History currently local to SQLite; remote sinks (Postgres/GCS) could be added behind
  the same `Store` interface.

---

*All code original. Data belongs to the public sources credited above.*

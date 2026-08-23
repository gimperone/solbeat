# SolBeat — Implementation Plan

Bounty: Superteam Canada — "Develop Solana Ecosystem Auto-Updating Report & Interactive Dashboard"
Deadline: 2026-09-01 · Prizes: 500/300/200 USDG · Submissions at plan time: 11

## Hard requirements extracted from the brief
1. Pull from multiple sources: Solana RPC (getEpochInfo, getRecentPerformanceSamples,
   getVoteAccounts, getSupply, ...), DeFiLlama, CoinGecko, ecosystem reports/news feeds.
   Dune + Twitter require API keys -> excluded by the no-keys preference; replaced with
   keyless equivalents (documented decision).
2. Metrics: TPS, slot time, block height, epoch progress, validator stats (active vs
   delinquent, stake distribution, top validators, commissions, delinquency alerts),
   SOL price, stablecoin supply, DEX volume, REV proxy, median fees, tokenized assets,
   daily active addresses estimate, upcoming upgrades (Alpenglow, SIMD-525), news.
3. Automation: configurable intervals, low maintenance ("SolPulse"-style agent).
4. Anomaly detection (highly valued): TPS drops/spikes, slow slots, validator
   delinquency, large TVL/price moves.
5. Outputs: interactive HTML dashboard (dark), Markdown report, machine-readable JSON.
6. PREFERRED: no API keys / no external dependencies beyond Python stdlib + Solana RPC.
7. Deliverables: public GitHub repo + README (run + interpret), live demo counts extra,
   sample MD + JSON reports, write-up of sources/automation/anomalies/setup.

## Architecture decisions (senior rationale)
- Pure Python 3 stdlib (urllib, sqlite3, threading, xml.etree, http.server).
  Zero pip installs -> directly maximizes the explicit "preferred" criterion.
- Hand-rolled SVG charts, no CDN -> dashboard works fully offline; single-file UI.
- SQLite for history -> z-score anomaly detection over rolling windows without any TSDB.
- Every collector isolated + failure-tolerant; source health tracked and displayed
  (partial outages degrade gracefully instead of killing the report).
- Honest methodology: REV proxied via DeFiLlama chain fees (documented); Daily Active
  Addresses estimated via statistical sampling of recent blocks' fee payers (documented,
  configurable sample size); tokenized equities approximated by RWA-category TVL on
  Solana (DeFiLlama) because keyless volume data does not exist.
- Free "live" story: GitHub Actions cron regenerates MD/JSON reports on schedule and
  publishes to GitHub Pages; interactive dashboard runs anywhere with one command.

## Milestones
M1 Skeleton + config + util/store                     [x]
M2 RPC collector (network, validators, supply)        [ ]
M3 Market collectors (DeFiLlama, CoinGecko)           [ ]
M4 News collector + curated upgrade roadmap           [ ]
M5 Anomaly engine                                     [ ]
M6 Report generators (MD + JSON)                      [ ]
M7 Dashboard (self-contained HTML/SVG)                [ ]
M8 Server + CLI + scheduler                           [ ]
M9 Tests, samples, README, Actions workflow           [ ]

## Risks
- Public RPC rate limits -> intervals >=60s, batching, retry/backoff, fallback endpoints.
- CoinGecko throttling -> 5 min interval + cached last good value.
- RSS format variance -> namespace-agnostic parsing, per-feed error isolation.

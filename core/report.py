"""Generate the three output formats: Markdown, JSON, and API payload assembly."""
import json
import time

SCHEMA_VERSION = "1.1"

# metric keys embedded (downsampled) into every payload so the static/Pages
# hosted version can still render trend charts without a backend
HISTORY_KEYS = [
    "tps_total", "sol_price_usd", "defi_tvl_usd",
    "dex_volume_24h_usd", "stablecoin_supply_usd", "median_tx_fee_sol",
]
HISTORY_POINTS = 120


def _downsample(rows: list[tuple[float, float]], points: int) -> list:
    if len(rows) <= points:
        return [[t * 1000, v] for t, v in rows]
    step = len(rows) / points
    return [[rows[int(i * step)][0] * 1000, rows[int(i * step)][1]] for i in range(points)]


def build_payload(snapshot: dict | None, store, cfg: dict) -> dict:
    """Merge latest snapshot with alerts/news/sources + embedded chart history."""
    snap = snapshot or {}
    m = snap.get("metrics", {})
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": snap.get("ts"),
        "metrics": m,
        "validators": snap.get("validators", []),
        # structured convenience object assembled from flat metrics
        "epoch": {
            "number": m.get("epoch"),
            "progress_pct": m.get("epoch_progress_pct"),
            "eta_hours": m.get("epoch_eta_hours"),
            "slots_remaining": m.get("epoch_slots_remaining"),
        },
        "derived": snap.get("derived", {}),
        "alerts": store.recent_alerts(50),
        "news": store.recent_news(cfg.get("news", {}).get("max_items", 30)),
        "upgrades": cfg.get("upgrades", []),
        "sources": store.all_sources(),
        "history_embed": {},
    }
    for key in HISTORY_KEYS:
        hist = store.history(key, limit=2000)
        if len(hist) >= 2:
            payload["history_embed"][key] = _downsample(hist, HISTORY_POINTS)
    return payload


def _fmt_usd(v) -> str:
    if v is None:
        return "n/a"
    if abs(v) >= 1e9:
        return f"${v / 1e9:,.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.1f}M"
    return f"${v:,.0f}"


def generate_markdown(payload: dict) -> str:
    m = payload["metrics"]
    g = lambda k: m.get(k)
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(payload["generated_at"])) if payload["generated_at"] else "n/a"

    lines = []
    lines.append("# SolBeat — Solana Ecosystem Report")
    lines.append("")
    lines.append(f"_Auto-generated on {ts}. All data collected keyless from public APIs._")
    lines.append("")

    # Alerts first — actionable info on top
    crit = [a for a in payload["alerts"] if a["level"] == "crit"]
    warn = [a for a in payload["alerts"] if a["level"] == "warn"]
    lines.append(f"## 🚨 Active anomalies ({len(crit)} critical, {len(warn)} warnings)")
    if not payload["alerts"]:
        lines.append("None detected in the current window. ✅")
    for a in payload["alerts"][:10]:
        icon = "🔴" if a["level"] == "crit" else "🟡"
        t = time.strftime("%m-%d %H:%M", time.gmtime(a["ts"]))
        lines.append(f"- {icon} `{a['metric']}` {a['message']} _({t} UTC)_")
    lines.append("")

    lines.append("## ⚡ Network performance")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| TPS (total, ~last min avg) | {g('tps_total') or 'n/a':.0f} |" if g("tps_total") else "| TPS | n/a |")
    lines.append(f"| TPS (non-vote) | {g('tps_non_vote'):.0f} |" if g("tps_non_vote") else "| TPS (non-vote) | n/a |")
    lines.append(f"| Avg slot time | {g('avg_slot_ms'):.0f} ms |" if g("avg_slot_ms") else "| Avg slot time | n/a |")
    lines.append(f"| Median tx fee | {g('median_tx_fee_sol') * 1e9:,.0f} lamports ({g('median_tx_fee_sol'):.9f} SOL) |"
                 if g("median_tx_fee_sol") else "| Median tx fee | n/a |")
    lines.append(f"| Slot height | {g('slot'):,} |" if g("slot") else "| Slot | n/a |")
    lines.append(f"| Epoch progress | {g('epoch_progress_pct'):.1f}% (#{g('epoch')}) · ends in ~{g('epoch_eta_hours')}h |"
                 if g("epoch_progress_pct") and g("epoch_eta_hours") else "| Epoch progress | n/a |")
    lines.append("")

    lines.append("## 🗳️ Validators")
    va = payload["derived"].get("validators_summary", {})
    lines.append(f"- Active validators: **{va.get('active', 'n/a')}** · Delinquent: **{va.get('delinquent', 'n/a')}** "
                 f"({va.get('delinquent_stake_pct', 0):.2f}% of stake)")
    lines.append(f"- Total active stake: **{(va.get('activated_stake_sol', 0) or 0):,.0f} SOL** · "
                 f"Median commission: **{va.get('median_commission', 'n/a')}%**")
    if va.get("nakamoto_coefficient"):
        lines.append(f"- **Nakamoto coefficient: {va['nakamoto_coefficient']}** validators to reach 34% stake · "
                     f"Top-10 hold **{va.get('top10_stake_pct', 'n/a')}%** of stake")
    if va.get("commission_0_pct") is not None:
        lines.append(f"- Commission distribution: {va['commission_0_pct']}% at 0% · {va.get('commission_low_pct')}% "
                     f"at 1-9% · {va.get('commission_high_pct')}% at ≥10%")
    top = payload["validators"][:10]
    if top:
        lines.append("")
        lines.append("| # | Validator | Stake (SOL) | Commission | Credits (epoch) | Status |")
        lines.append("|---|---|---|---|---|---|")
        for i, v in enumerate(top, 1):
            status = "⚠️ delinquent" if v.get("delinquent") else "active"
            name = v.get("name") or (v.get("vote_pubkey", "")[:12] + "…")
            lines.append(f"| {i} | {name} | {v['activated_stake_sol']:,.0f} | {v.get('commission', '?')}% | "
                         f"{v.get('epoch_credit_current', '—')} | {status} |")
    lines.append("")

    lines.append("## 💹 Economic indicators")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    price = g("sol_price_usd")
    chg = g("sol_price_change_24h_pct")
    price_s = f"${price:,.2f}" + (f" ({chg:+.1f}% 24h)" if chg is not None else "") if price else "n/a"
    lines.append(f"| SOL price | {price_s} |")
    binp = g("binance_sol_price_usd")
    if binp:
        lines.append(f"| Binance reference | ${binp:,.2f} (divergence {g('cg_bin_divergence_pct')}%) |")
    lines.append(f"| Market cap | {_fmt_usd(g('sol_market_cap_usd'))} |")
    lines.append(f"| DeFi TVL (Solana) | {_fmt_usd(g('defi_tvl_usd'))} |")
    lines.append(f"| DEX volume 24h | {_fmt_usd(g('dex_volume_24h_usd'))} |")
    lines.append(f"| Chain fees 24h (REV proxy) | {_fmt_usd(g('fees_24h_usd'))} |")
    if g("rev_24h_usd"):
        lines.append(f"| Chain revenue 24h | {_fmt_usd(g('rev_24h_usd'))} |")
    lines.append(f"| Stablecoin supply | {_fmt_usd(g('stablecoin_supply_usd'))} |")
    lines.append(f"| SOL circulating supply | {(g('sol_circulating_supply') or 0):,.0f} |")
    lines.append(f"| Est. daily active addresses (sampled)* | {g('est_daily_active_addresses'):,.0f} |"
                 if g("est_daily_active_addresses") else "| Est. daily active addresses | n/a |")
    lines.append(f"| RWA / tokenized assets TVL* | {_fmt_usd(g('rwa_tvl_usd'))} |" if g("rwa_tvl_usd") else "| RWA tokenized TVL | n/a |")
    lines.append("")
    lines.append("\\* Methodology notes in README.md (sampling-based estimates, keyless data only).")
    lines.append("")

    news = payload["news"][:8]
    lines.append("## 📰 Ecosystem & community news (auto-filtered)")
    if news:
        for n in news:
            t = time.strftime("%m-%d", time.gmtime(n["ts"]))
            lines.append(f"- [{n['source']}] [{n['title']}]({n['link']}) _{t}_")
    else:
        lines.append("_No fresh items matched filters._")
    lines.append("")

    lines.append("## 🛣️ Upcoming upgrades & developments (curated)")
    for u in payload["upgrades"]:
        lines.append(f"- **{u['name']}** — {u['status']}: {u['note']} [link]({u['link']})")
    lines.append("")

    lines.append("## 📡 Source health")
    for s in payload["sources"]:
        ok = s["status"] == "ok"
        mark = "✅" if ok else "❌"
        age = f"{(time.time() - s['last_ok']) / 60:.0f} min ago" if s.get("last_ok") else "never"
        lines.append(f"- {mark} **{s['name']}**: {s['status']} (last success {age}, {s.get('items', 0)} items)")

    lines.append("")
    lines.append("---")
    lines.append("_Generated by [SolBeat](https://github.com/) — pure Python stdlib, zero API keys._")
    return "\n".join(lines)


def generate_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str)

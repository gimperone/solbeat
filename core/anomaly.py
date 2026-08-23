"""Anomaly detection: absolute thresholds + z-score over rolling metric history."""
import statistics

from .util import utcnow


def _zscore(values: list[float], current: float) -> float | None:
    if len(values) < 10:
        return None
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return None
    return (current - mean) / stdev


def evaluate(snapshot: dict, store, cfg: dict) -> list[dict]:
    """Return list of alert dicts based on the fresh snapshot + stored history."""
    a = cfg.get("anomaly", {})
    window = int(a.get("window_size", 120))
    zt = float(a.get("z_threshold", 3.0))
    alerts: list[dict] = []
    now = utcnow()

    def check(metric: str, value: float | None, fn):
        if value is not None:
            res = fn(value)
            for level, message in res:
                alerts.append({"ts": now, "level": level, "metric": metric, "message": message})

    m = snapshot.get("metrics", {})

    # -- slot time (absolute thresholds) --------------------------------
    def slot_rule(v: float):
        out = []
        if v >= a.get("avg_slot_ms_crit", 1000):
            out.append(("crit", f"Average slot time {v:.0f}ms >= {a.get('avg_slot_ms_crit', 1000)}ms"))
        elif v >= a.get("avg_slot_ms_warn", 600):
            out.append(("warn", f"Average slot time {v:.0f}ms above nominal ~400-500ms"))
        return out
    check("avg_slot_ms", m.get("avg_slot_ms"), slot_rule)

    # -- validator delinquency ------------------------------------------
    def delinq_rule(v: float):
        out = []
        if v > a.get("delinquent_pct_crit", 5.0):
            out.append(("crit", f"Delinquent validators {v:.2f}% of stake"))
        elif v > a.get("delinquent_pct_warn", 1.0):
            out.append(("warn", f"Delinquent stake {v:.2f}% elevated"))
        return out
    check("delinquent_stake_pct", m.get("delinquent_stake_pct"), delinq_rule)

    # -- TPS: pct drop vs rolling median + z-score spike -----------------
    tps = m.get("tps_total")

    def tps_rule(v: float):
        out = []
        hist = store.window_values("tps_total", utcnow() - 86400)
        if len(hist) >= 20:
            med = statistics.median(hist[:-1])
            drop = a.get("tps_drop_pct", 50)
            if med and v < med * (1 - drop / 100.0):
                out.append(("crit", f"TPS {v:.0f} dropped >{drop:.0f}% vs 24h median ({med:.0f})"))
        z = _zscore(store.window_values("tps_total", utcnow() - 7 * 86400), v)
        if z is not None and abs(z) >= zt:
            out.append(("warn", f"TPS z-score {z:+.1f} vs history (value {v:.0f})"))
        return out
    check("tps_total", tps, tps_rule)

    # -- price / TVL / stablecoins: pct moves vs previous stored value ------
    # NOTE: in production the current value is recorded BEFORE evaluate() runs,
    # so history's last point IS the current one -> baseline must be hist[0]
    # (the second-most-recent sample). Regression-tested against this order.
    def move_rule(key: str, label: str, threshold: float):
        def rule(v: float):
            out = []
            hist = store.history(key, limit=2)
            if hist:
                # production order (record->evaluate): hist[0] = previous sample;
                # direct-evaluate order (tests/one-shot): hist[0] = only sample,
                # which acts as baseline against the fresh snapshot value.
                baseline = hist[0][1]
                change = (v - baseline) / abs(baseline) * 100.0 if baseline else 0.0
                if abs(change) >= threshold:
                    out.append((
                        "warn",
                        f"{label} moved {change:+.1f}% since previous sample "
                        f"({baseline:,.0f} -> {v:,.0f})",
                    ))
            return out
        return rule
    check("sol_price_usd", m.get("sol_price_usd"), move_rule("sol_price_usd", "SOL price", a.get("price_move_pct", 15)))
    check("defi_tvl_usd", m.get("defi_tvl_usd"), move_rule("defi_tvl_usd", "DeFi TVL", a.get("tvl_move_pct", 10)))
    check("stablecoin_supply_usd", m.get("stablecoin_supply_usd"),
          move_rule("stablecoin_supply_usd", "Stablecoin supply", a.get("stables_move_pct", 5)))

    # -- multi-source price correlation (CoinGecko vs Binance) -------------
    def divergence_rule(v: float):
        out = []
        if v >= a.get("price_divergence_warn", 1.5):
            out.append(("warn",
                        f"Price divergence {v:.2f}% between CoinGecko and Binance "
                        f"(threshold {a.get('price_divergence_warn', 1.5)}%)"))
        return out
    check("cg_bin_divergence_pct", m.get("cg_bin_divergence_pct"), divergence_rule)

    # -- validator set shrinkage -------------------------------------------
    def valcount_rule(v: float):
        out = []
        hist = store.window_values("validators_active", utcnow() - 86400)
        if len(hist) >= 10:
            med = statistics.median(hist)
            drop = a.get("validators_drop_pct", 2.0)
            if med and v < med * (1 - drop / 100.0):
                out.append(("warn",
                            f"Active validators {v:.0f} dropped >{drop:.0f}% vs 24h median ({med:.0f})"))
        return out
    check("validators_active", m.get("validators_active"), valcount_rule)

    # -- fee spike (z-score on median fee) ----------------------------------
    def fee_rule(v: float):
        out = []
        z = _zscore(store.window_values("median_tx_fee_sol", utcnow() - 7 * 86400), v)
        if z is not None and abs(z) >= a.get("fee_spike_z", 3.0):
            out.append(("warn", f"Median tx fee z-score {z:+.1f} (now {v * 1e6:.1f} microSOL)"))
        return out
    check("median_tx_fee_sol", m.get("median_tx_fee_sol"), fee_rule)

    return alerts

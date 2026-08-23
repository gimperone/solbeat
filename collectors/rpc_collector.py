"""Solana JSON-RPC collector: network performance, validators, supply,
block sampling for median fees + daily-active-address estimate,
stake concentration and commission distribution."""
import random
import statistics
import time

from core.util import RpcClient, utcnow


def _perf_samples_metrics(client: RpcClient) -> dict:
    res = client.call("getRecentPerformanceSamples", [60]) or []
    if not res:
        return {}
    total_tx = sum(s.get("numTransactions", 0) for s in res)
    total_nonvote = sum(s.get("numNonVoteTransaction", s.get("numNonVoteTransactions", 0)) for s in res)
    total_secs = sum(s.get("samplePeriodSecs", 60) for s in res)
    total_slots = sum(s.get("numSlots", 0) for s in res) or 1
    return {
        "tps_total": round(total_tx / total_secs, 1),
        "tps_non_vote": round(total_nonvote / total_secs, 1),
        "avg_slot_ms": round(total_secs * 1000 / total_slots, 1),
    }


def _epoch_metrics(client: RpcClient) -> dict:
    ei = client.call("getEpochInfo") or {}
    slots = ei.get("slotsInEpoch", 432000)
    slot = ei.get("absoluteSlot") or ei.get("slotIndex", 0)
    return {
        "slot": slot,
        "epoch": ei.get("epoch"),
        "epoch_progress_pct": round((ei.get("slotIndex", 0) / slots) * 100, 2) if slots else None,
        "block_height": ei.get("blockHeight"),
        "slots_in_epoch": slots,
    }


def _validator_rows(client: RpcClient, top_n: int) -> tuple[list[dict], dict]:
    va = client.call("getVoteAccounts") or {}
    current = va.get("current", [])
    delinquent = va.get("delinquent", [])
    max_stake = max([v.get("activatedStake", 0) for v in current], default=1) or 1
    rows = []
    for v in current + delinquent:
        ec = v.get("epochCredits") or []
        credit_now = round(ec[-1][1] - ec[-1][2]) if len(ec) >= 2 else None
        name = (v.get("nodePubkey") or "")[:16]
        rows.append({
            "name": name,
            "vote_pubkey": v.get("votePubkey"),
            "activated_stake_sol": round(v.get("activatedStake", 0) / 1e9),
            "commission": v.get("commission"),
            "epoch_credit_current": credit_now,
            "delinquent": bool(v.get("delinquent")),
            "stake_share_pct": round(v.get("activatedStake", 0) / max_stake * 100, 2),
        })
    rows.sort(key=lambda r: r["activated_stake_sol"], reverse=True)

    commissions = sorted(v.get("commission", 0) for v in current)
    med = commissions[len(commissions) // 2] if commissions else None
    delinq_stake = sum(v.get("activatedStake", 0) for v in delinquent)
    total_stake = sum(v.get("activatedStake", 0) for v in current) + delinq_stake
    active_stake = total_stake - delinq_stake

    # -- stake concentration (SOL-denominated, active validators only):
    #    Nakamoto coefficient = minimum validators holding >=34% of active stake
    active_rows = [v for v in rows if not v["delinquent"]]
    active_stake_sol = sum(v["activated_stake_sol"] for v in active_rows)
    nak = 0
    cum = 0
    for v in active_rows:
        cum += v["activated_stake_sol"]
        nak += 1
        if active_stake_sol and cum >= active_stake_sol * 0.34:
            break
    top10_share = round(sum(v["activated_stake_sol"] for v in active_rows[:10]) / active_stake_sol * 100, 2) if active_stake_sol else None

    # -- commission distribution buckets (tracking over time via metrics)
    n_cur = max(1, len(commissions))
    comm_0 = sum(1 for c in commissions if c == 0)
    comm_low = sum(1 for c in commissions if 0 < c < 10)
    comm_high = sum(1 for c in commissions if c >= 10)

    summary = {
        "active": len(current),
        "delinquent": len(delinquent),
        "activated_stake_sol": round(total_stake / 1e9),
        "delinquent_stake_pct": round(delinq_stake / total_stake * 100, 3) if total_stake else 0.0,
        "median_commission": med,
        "nakamoto_coefficient": nak,
        "top10_stake_pct": top10_share,
        "commission_0_pct": round(comm_0 / n_cur * 100, 1),
        "commission_low_pct": round(comm_low / n_cur * 100, 1),
        "commission_high_pct": round(comm_high / n_cur * 100, 1),
    }
    return rows[:top_n], summary


def _supply_metrics(client: RpcClient) -> dict:
    sup = client.call("getSupply", [{"excludeNonCirculatingAccountsList": True}]) or {}
    val = sup.get("value", {}) or {}
    return {
        "sol_total_supply": round(val.get("total", 0) / 1e9, 2),
        "sol_circulating_supply": round(val.get("circulating", 0) / 1e9, 2),
    }


def _sample_blocks_stats(client: RpcClient, sample_blocks: int) -> dict:
    """Block sampler: unique fee payers -> DAA estimate; transaction meta.fee ->
    median/mean fee. One bounded pass, shared RPC cost for two metrics."""
    deadline = time.time() + 45  # hard time budget
    try:
        bh_res = client.call("getBlockHeight") or 0
        if not bh_res:
            return {}
        start = bh_res - 150  # ~1 minute of chain
        end = bh_res - 5
        blocks = client.call("getBlocks", [start, end]) or []
        if not blocks:
            return {}
        picks = random.sample(blocks, min(max(3, sample_blocks), len(blocks)))
        payers = set()
        fees = []
        ok_blocks = 0
        for b in picks:
            if time.time() > deadline or len(payers) >= 4000 or len(fees) >= 20000:
                break
            try:
                blk = client.call("getBlock", [b, {"maxSupportedTransactionVersion": 0}])
                if not blk:
                    continue
                ok_blocks += 1
                block_payers = set()
                for tx in blk.get("transactions", []):
                    msg = tx.get("transaction", {}).get("message", {})
                    keys = msg.get("accountKeys", [])
                    if isinstance(keys, list) and keys:
                        first = keys[0]
                        fee_payer = first if isinstance(first, str) else (first or {}).get("pubkey", "")
                    else:
                        continue
                    if fee_payer:
                        block_payers.add(fee_payer)
                    meta = tx.get("meta") or {}
                    fee = meta.get("fee")
                    if isinstance(fee, int):
                        fees.append(fee)
                payers |= block_payers
            except Exception:
                continue  # individual block unavailable -> skip it
        out = {"fee_sample_txs": len(fees), "fee_sample_blocks": ok_blocks}
        if ok_blocks < 3:
            return out
        day_slots = 216_000  # ~400ms nominal
        span = max(1, blocks[-1] - blocks[0] + 1)
        if payers:
            out["est_daily_active_addresses"] = round(len(payers) * (day_slots / span))
        if fees:
            out["median_tx_fee_sol"] = round(statistics.median(fees) / 1e9, 9)
            out["mean_tx_fee_sol"] = round(sum(fees) / len(fees) / 1e9, 9)
        # -- getBlockTime: real timestamps -> independent slot-time cross-check
        #    (perf-sample slot time is derived; this one comes from wall clock)
        times: list[tuple[int, int]] = []
        for b in picks:
            if time.time() > deadline:
                break
            try:
                ts = client.call("getBlockTime", [b])
                if isinstance(ts, int):
                    times.append((b, ts))
            except Exception:
                continue
        if len(times) >= 2:
            rates = []
            for (s1, t1), (s2, t2) in zip(times, times[1:]):
                ds, dt = s2 - s1, t2 - t1
                if ds > 0 and dt >= 0:
                    rates.append(dt / ds * 1000)
            if rates:
                out["block_time_getblocktime_ms"] = round(statistics.median(rates), 1)
        return out
    except Exception:
        return {}


def collect(cfg: dict) -> dict:
    """Returns partial snapshot: {metrics:{}, validators:[], derived:{}}"""
    client = RpcClient(cfg.get("rpc_endpoints"))
    metrics: dict = {}
    out: dict = {"metrics": metrics}

    health = client.call("getHealth")
    version = client.call("getVersion") or {}

    metrics.update(_epoch_metrics(client))
    metrics.update(_perf_samples_metrics(client))
    metrics.update(_supply_metrics(client))
    metrics.update({"rpc_health": 1.0 if health == "ok" else 0.0})
    out["derived"] = {
        "node_version": f"{version.get('major','?')}.{version.get('minor','?')}.{version.get('patch','?')}",
    }

    # -- getSlot: brief-listed method + consistency cross-check vs getEpochInfo
    try:
        slot_direct = client.call("getSlot")
        if isinstance(slot_direct, int):
            prev = metrics.get("slot")
            metrics["slot"] = slot_direct
            if isinstance(prev, int):
                # two sources of truth must agree within a few slots
                metrics["slot_source_divergence"] = abs(slot_direct - prev)
    except Exception:
        pass

    # -- getSignaturesForAddress: ecosystem activity via top DEX aggregator.
    #    Measures how fresh the busiest program's traffic is (lag in slots).
    jup = cfg.get("jupiter_program", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
    try:
        sigs = client.call("getSignaturesForAddress", [jup, {"limit": 100}]) or []
        if sigs:
            cur_slot = metrics.get("slot")
            if isinstance(cur_slot, int):
                metrics["jupiter_tx_lag_slots"] = max(0, cur_slot - sigs[0].get("slot", cur_slot))
            times = [s.get("blockTime") for s in sigs if s.get("blockTime")]
            if len(times) >= 2 and cur_slot:
                span_sec = max(1, max(times) - min(times))
                metrics["jupiter_signatures_per_min"] = round(len(times) / span_sec * 60, 1)
    except Exception:
        pass

    rows, summary = _validator_rows(client, int(cfg.get("top_validators_count", 25)))
    out["validators"] = rows
    out["derived"]["validators_summary"] = summary
    metrics.update({
        "validators_active": summary["active"],
        "validators_delinquent": summary["delinquent"],
        "delinquent_stake_pct": summary["delinquent_stake_pct"],
        "nakamoto_coefficient": summary["nakamoto_coefficient"],
        "top10_stake_pct": summary["top10_stake_pct"],
        "commission_0_pct": summary["commission_0_pct"],
        "commission_high_pct": summary["commission_high_pct"],
    })

    # epoch ETA from observed slot pace (more honest than nominal 400ms)
    if metrics.get("avg_slot_ms") and metrics.get("slots_in_epoch"):
        slots_left = max(0, metrics["slots_in_epoch"] - (metrics.get("slot", 0) % metrics["slots_in_epoch"]))
        metrics["epoch_slots_remaining"] = slots_left
        metrics["epoch_eta_hours"] = round(slots_left * metrics["avg_slot_ms"] / 3_600_000, 2)

    metrics["collected_at"] = utcnow()
    return out


def collect_daa(cfg: dict) -> dict:
    """Independent slow source so block sampling never delays network metrics."""
    client = RpcClient(cfg.get("rpc_endpoints"))
    res = _sample_blocks_stats(client, int(cfg.get("daa_sample_blocks", 6)))
    if not res or "est_daily_active_addresses" not in res:
        raise RuntimeError(f"block sampling inconclusive this round: {res}")
    keep = {k: v for k, v in res.items() if k.startswith(("est_", "median_", "mean_", "fee_sample"))}
    return {"metrics": keep}

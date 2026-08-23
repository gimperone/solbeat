"""Keyless market collectors: DeFiLlama (TVL, stablecoins, DEX volume, fees/REV proxy,
RWA/tokenized TVL), CoinGecko (SOL price) and Binance (multi-source price check)."""
from core.util import http_json, utcnow


def _llama_chain_tvl() -> float | None:
    chains = http_json("https://api.llama.fi/v2/chains")
    for c in chains:
        if c.get("name", "").lower() == "solana":
            return float(c.get("tvl", 0))
    return None


def _stablecoin_supply() -> float | None:
    # NOTE: the ?chain=Solana query form is ignored by the API (returns global);
    # the path form is chain-filtered and verified against known totals.
    data = http_json("https://stablecoins.llama.fi/stablecoincharts/solana")
    if not data:
        return None
    last = data[-1]
    total = float(last.get("totalCirculating", {}).get("peggedUSD", 0))
    return total


def _dex_volume_24h() -> float | None:
    data = http_json(
        "https://api.llama.fi/overview/dexs/solana",
        params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
    )
    return data.get("total24h")


def _fees_24h() -> tuple[float | None, float | None]:
    data = http_json(
        "https://api.llama.fi/overview/fees/solana",
        params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
    )
    return data.get("total24h"), data.get("total24hRev")


def _rwa_tvl_solana() -> float | None:
    """Sum TVL of protocols tagged RWA active on Solana (tokenized assets incl. equities)."""
    protocols = http_json("https://api.llama.fi/protocols")
    total = 0.0
    for p in protocols:
        if "RWA" in (p.get("category") or "") and "Solana" in (p.get("chains") or []):
            total += p.get("tvl", 0) or 0
    return total if total > 0 else None


def collect_defillama(cfg: dict) -> dict:
    metrics: dict = {}
    tvl = _llama_chain_tvl()
    if tvl is not None:
        metrics["defi_tvl_usd"] = tvl
    stables = _stablecoin_supply()
    if stables is not None:
        metrics["stablecoin_supply_usd"] = stables
    dex = _dex_volume_24h()
    if dex is not None:
        metrics["dex_volume_24h_usd"] = float(dex)
    fees, rev = _fees_24h()
    if fees is not None:
        metrics["fees_24h_usd"] = float(fees)
    if rev is not None:
        metrics["rev_24h_usd"] = float(rev)
    rwa = _rwa_tvl_solana()
    if rwa is not None:
        metrics["rwa_tvl_usd"] = rwa
    return {"metrics": metrics}


def _binance_price() -> float | None:
    """Keyless CEX reference for multi-source price correlation."""
    try:
        data = http_json("https://api.binance.com/api/v3/ticker/price", params={"symbol": "SOLUSDT"})
        return float(data["price"])
    except Exception:
        return None


def collect_coingecko(cfg: dict) -> dict:
    data = http_json(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": "solana",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_market_cap": "true",
        },
    )
    sol = data.get("solana", {})
    metrics = {}
    if "usd" in sol:
        metrics["sol_price_usd"] = float(sol["usd"])
    if "usd_24h_change" in sol:
        metrics["sol_price_change_24h_pct"] = float(sol["usd_24h_change"])
    if "usd_market_cap" in sol:
        metrics["sol_market_cap_usd"] = float(sol["usd_market_cap"])

    # multi-source correlation: CoinGecko vs Binance divergence
    binp = _binance_price()
    if binp and metrics.get("sol_price_usd"):
        metrics["binance_sol_price_usd"] = binp
        metrics["cg_bin_divergence_pct"] = round(
            abs(metrics["sol_price_usd"] - binp) / binp * 100, 3)
    return {"metrics": metrics}

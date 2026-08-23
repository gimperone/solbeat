import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from collectors.rpc_collector import _validator_rows  # noqa: E402


class FakeRpcClient:
    """Minimal getVoteAccounts stub with lamports-denominated stakes:
    40 + 30 + 30 SOL active, 5 SOL delinquent."""
    def call(self, method, params=None):
        if method == "getVoteAccounts":
            return {
                "current": [
                    {"votePubkey": "V1", "nodePubkey": "AAA", "activatedStake": 40e9,
                     "commission": 0, "epochCredits": [[100, 500, 400]]},
                    {"votePubkey": "V2", "nodePubkey": "BBB", "activatedStake": 30e9,
                     "commission": 5, "epochCredits": [[100, 700, 600]]},
                    {"votePubkey": "V3", "nodePubkey": "CCC", "activatedStake": 30e9,
                     "commission": 10},
                ],
                "delinquent": [
                    {"votePubkey": "V4", "nodePubkey": "DDD", "activatedStake": 5e9,
                     "commission": 8},
                ],
            }
        return None


class TestValidatorRows(unittest.TestCase):
    def setUp(self):
        self.rows, self.summary = _validator_rows(FakeRpcClient(), 25)

    def test_counts(self):
        self.assertEqual(self.summary["active"], 3)
        self.assertEqual(self.summary["delinquent"], 1)

    def test_total_stake_in_sol(self):
        # (40+30+30+5) SOL — lamports converted
        self.assertEqual(self.summary["activated_stake_sol"], 105)

    def test_nakamoto_excludes_delinquent_and_uses_consistent_units(self):
        # 34% of 100 active SOL = 34 -> first validator alone (40) crosses it
        self.assertEqual(self.summary["nakamoto_coefficient"], 1)

    def test_top10_share_over_active_stake_only(self):
        self.assertEqual(self.summary["top10_stake_pct"], 100.0)

    def test_commission_buckets(self):
        # active set: 0%, 5%, 10% -> one third each
        self.assertEqual(self.summary["commission_0_pct"], 33.3)
        self.assertEqual(self.summary["commission_low_pct"], 33.3)
        self.assertEqual(self.summary["commission_high_pct"], 33.3)
        self.assertEqual(self.summary["median_commission"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

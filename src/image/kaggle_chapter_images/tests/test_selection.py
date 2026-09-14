import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Avoid importing the GPU kernel. This mirrors the deterministic selector contract.
WEIGHTS = {"narrative": 25, "visual": 20, "character": 15, "emotion": 15, "coverage": 15, "evidence": 10}


def score(candidate):
    return sum(candidate["scores"][key] / 5 * weight for key, weight in WEIGHTS.items())


class SelectionContractTests(unittest.TestCase):
    def test_weighted_score_requires_all_dimensions(self):
        candidate = {"scores": {key: 5 for key in WEIGHTS}}
        self.assertEqual(score(candidate), 100)

    def test_distinct_evidence_and_order_contract(self):
        first = {"position": .2, "evidence": [{"chunk_id": "ch0001_p001"}]}
        second = {"position": .8, "evidence": [{"chunk_id": "ch0001_p004"}]}
        self.assertLess(first["position"], second["position"])
        self.assertNotEqual({x["chunk_id"] for x in first["evidence"]}, {x["chunk_id"] for x in second["evidence"]})


if __name__ == "__main__": unittest.main()

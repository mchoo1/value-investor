"""Tests for the valuation engine and the inbox safety checks. Run: python -m pytest tools -q (or python tools/test_valuation.py)"""
import sys, copy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import valuation as V

A = {"revenue0": 100e9, "shares0": 1e9, "net_debt": 0,
     "profile": {"moat": "stable", "nd_ebitda": 0.5},
     "scenarios": {"bear": {"growth": [0.03], "margin": [0.20, 0.18], "reinvest_pct": 0.05, "tax": 0.2, "terminal_growth": 0.02},
                   "base": {"growth": [0.08, 0.08, 0.07, 0.07, 0.06], "margin": [0.22, 0.25], "reinvest_pct": 0.05, "tax": 0.2, "terminal_growth": 0.025},
                   "bull": {"growth": [0.12], "margin": [0.24, 0.30], "reinvest_pct": 0.05, "tax": 0.2, "terminal_growth": 0.03}}}


def test_deterministic():
    r1, r2 = V.run(copy.deepcopy(A), price=200), V.run(copy.deepcopy(A), price=200)
    assert r1 == r2 and r1["inputs_hash"] == r2["inputs_hash"]


def test_order_and_targets():
    r = V.run(A, price=200)
    assert r["iv_low"] < r["iv_base"] < r["iv_high"]
    assert r["entry_target"] == round(r["iv_base"] * (1 - r["margin_of_safety"]), 2)
    assert 0.15 <= r["margin_of_safety"] <= 0.40
    assert r["growth_zone"] and r["growth_zone"] > 0
    assert r["reverse_dcf_growth"] is not None


def test_margin_of_safety_rules():
    assert V.margin_of_safety({}) == 0.25                      # unproven moat adds 5
    assert V.margin_of_safety({"moat": "stable"}) == 0.20
    assert V.margin_of_safety({"moat": "narrowing", "cyclical": True, "nd_ebitda": 4}) == 0.40
    assert V.margin_of_safety({"moat": "wide", "roic_10y_above_15": True, "stable_revenue": True}) == 0.15


def test_non_dcf_models():
    r = V.run(dict(A, profile={"model": "bank"}), price=50)
    assert r["dcf_valid"] is False and "DCF not valid" in r["note"]
    assert V.model_type("Finance", "Major Banks") == "bank"
    assert V.model_type("Technology", "Prepackaged Software") == "standard"


def test_gap_attribution():
    b = copy.deepcopy(A)
    b["scenarios"]["base"]["margin"] = [0.18, 0.20]
    g = V.attribute_gap(A, b, price=200)
    assert g["b"] < g["a"] and g["parts"][0]["input"] == "margin"


def test_evidence():
    s = V.evidence_summary([{"claim": "x", "grade": "A", "source": "10-K", "pillar": 1},
                            {"claim": "y", "grade": "E", "pillar": 1}, {"claim": "z", "grade": "E", "pillar": 2}])
    assert s["weak_flag"] and s["counts"]["E"] == 2


def test_missing_inputs():
    try:
        V.run({"scenarios": {"base": {}}})
        assert False
    except ValueError:
        pass



def test_inbox_validate():
    import inbox
    inbox.validate({"type": "thesis", "ticker": "MSFT"})
    for bad in ({"type": "nope"}, {"type": "refresh"}, {"type": "thesis", "ticker": "X", "assumptions": {"scenarios": {}}}):
        try:
            inbox.validate(bad)
            assert False, bad
        except ValueError:
            pass
    assert inbox.capped(80, 60) == 60 and inbox.capped(50, 60) == 50 and inbox.capped(80, None) == 80



def test_simulator():
    r0 = V.simulate(A, price=200)
    assert abs(r0["change_vs_base"]) < 1e-9
    up = V.simulate(A, price=200, changes={"growth_delta": 0.05})
    dn = V.simulate(A, price=200, changes={"revenue_shocks": [{"year": 2, "pct": -0.2, "phase_years": 2}]})
    assert up["value"] > r0["value"] > dn["value"]
    rv = V.reverse(A, price=200)
    chk = V.simulate(A, price=200, changes={"growth": [rv["growth_needed"]] * 5})
    assert abs(chk["value"] - 200) < 0.5
    d = V.default_assumptions({"history": [{"revenue": 100, "op_margin": .2, "fcf_margin": .15, "shares": 10},
                                           {"revenue": 110}, {"revenue": 121}, {"revenue": 133, "op_margin": .22, "fcf_margin": .16, "shares": 10}]})
    assert d and 0.09 < d["scenarios"]["base"]["growth"][0] < 0.11


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
    print("all valuation tests passed")

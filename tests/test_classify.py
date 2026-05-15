import pandas as pd
from src.classify import classify_open_orders


def test_classify_open_orders():
    openords = pd.DataFrame(
        {
            "symbol":        ["AAPL", "TSLA"],
            "secType":       ["STK",  "OPT"],
            "action":        ["BUY",  "SELL"],
            "totalQuantity": [100,    5],
            "lmtPrice":      [150.0,  5.0],
            "state":         ["",     ""],
            "right":         ["",     "C"],
            "strike":        [0.0,    450.0],
        }
    )

    pf = pd.DataFrame(
        {
            "symbol":   ["AAPL"],
            "secType":  ["STK"],
            "position": [100],
            "avgCost":  [140.0],
            "right":    [""],
            "strike":   [0.0],
            "state":    ["exposed"],
        }
    )

    result = classify_open_orders(openords, pf)

    assert "state" in result.columns
    assert len(result) == 2

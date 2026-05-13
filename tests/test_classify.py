import pandas as pd
from classify import classify_open_orders


def test_classify_open_orders():
    openords = pd.DataFrame(
        {
            "symbol": ["AAPL", "TSLA"],
            "secType": ["STK", "OPT"],
            "action": ["BUY", "SELL"],
            "totalQuantity": [100, 5],
            "lmtPrice": [150.0, 5.0],
            "state": ["", ""],
            "right": ["", "C"],
        }
    )

    pf = pd.DataFrame(
        {"symbol": ["AAPL"], "position": [100], "avgCost": [140.0], "state": ["exposed"]}
    )

    result = classify_open_orders(openords, pf)

    assert "state" in result.columns
    assert len(result) == 2

"""Position parsing + state machine, including settlement awareness."""

from src.nsewheel.classify import classify_pf, parse_positions


def test_parse_and_classify_states(client, df_instr):
    df_pf = classify_pf(parse_positions(client.positions(), df_instr))
    by_sym = df_pf.set_index(["symbol", "secType"])["state"].to_dict()

    # SBIN: short put, no stock -> sowed (physical/wheelable)
    assert by_sym[("SBIN", "OPT")] == "sowed"
    # INFY: long stock + short call -> stock unprotected, the call covering
    assert by_sym[("INFY", "STK")] == "unprotected"
    assert by_sym[("INFY", "OPT")] == "covering"
    # NIFTY: short call, no stock, cash-settled -> income_short (never sowed)
    assert by_sym[("NIFTY", "OPT")] == "income_short"


def test_settlement_override_blocks_index_wheel(client, df_instr):
    df_pf = classify_pf(parse_positions(client.positions(), df_instr))
    cash_shorts = df_pf[(df_pf.settlement == "cash") & (df_pf.position < 0)]
    assert (cash_shorts["state"] == "income_short").all()
    assert "sowed" not in set(cash_shorts["state"])

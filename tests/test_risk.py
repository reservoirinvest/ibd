from build import atm_margin

def test_atm_margin():
    margin = atm_margin(100, 100, 30, 0.2)
    assert margin > 0
    assert isinstance(margin, float)

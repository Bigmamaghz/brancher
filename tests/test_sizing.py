from src.sizing import qty_for_hit


def test_sizing_75_percent_is_base():
    assert qty_for_hit(0.75, base=1, max_mult=4) == 1


def test_sizing_100_percent_is_max():
    assert qty_for_hit(1.0, base=1, max_mult=4) == 4


def test_sizing_below_min_hit_is_zero():
    assert qty_for_hit(0.70, base=1, max_mult=4, min_hit=0.75) == 0


def test_sizing_midpoint():
    # 87.5% → halfway between 1× and 4× → ~2 or 3
    qty = qty_for_hit(0.875, base=1, max_mult=4)
    assert 2 <= qty <= 3

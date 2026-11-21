import pytest

from datalens.formula import FormulaError, compile_formula

COLS = {"strike_rate", "average", "economy", "runs"}


def test_simple_metric():
    assert compile_formula("strike_rate", COLS) == "strike_rate"


def test_weighted_formula_and_division_is_safe():
    sql = compile_formula("0.4*strike_rate + 0.6*average / runs", COLS)
    assert "NULLIF(runs, 0)" in sql


def test_window_helpers():
    assert "OVER ()" in compile_formula("z(strike_rate)", COLS)
    assert "percent_rank" in compile_formula("pctile(economy)", COLS)


@pytest.mark.parametrize("bad", [
    "__import__('os').system('ls')", "strike_rate; DROP TABLE balls", "open('/etc/passwd')",
    "strike_rate if 1 else 2", "unknown_col + 1", "'abc'", "lambda: 1", "z(strike_rate + 1)", "", "x" * 500,
    "strike_rate.real", "[1,2]", "strike_rate and runs",
])
def test_rejects_unsafe_or_unknown(bad):
    with pytest.raises(FormulaError):
        compile_formula(bad, COLS)

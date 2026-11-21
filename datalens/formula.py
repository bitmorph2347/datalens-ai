"""Safe compiler for user-written metric formulas.

Users write things like ``0.4*z(strike_rate) + 0.3*z(average) - 0.1*dot_pct``.
The expression is parsed with ``ast`` and translated to a DuckDB SQL expression
using only whitelisted column names, operators and functions - no raw SQL ever
reaches the database.
"""
from __future__ import annotations

import ast

MAX_LEN = 400
FUNCS = {"abs": "abs", "sqrt": "sqrt", "log": "ln", "min": "least", "max": "greatest", "round": "round"}
WINDOW_FUNCS = {"z", "pctile"}


class FormulaError(ValueError):
    pass


def compile_formula(expr: str, columns: set[str]) -> str:
    expr = (expr or "").strip()
    if not expr:
        raise FormulaError("Formula is empty")
    if len(expr) > MAX_LEN:
        raise FormulaError("Formula too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"Invalid formula syntax: {e.msg}") from None
    return _node(tree.body, columns)


def _node(n: ast.AST, cols: set[str]) -> str:
    if isinstance(n, ast.Constant):
        if isinstance(n.value, bool) or not isinstance(n.value, (int, float)):
            raise FormulaError("Only numbers are allowed as constants")
        return repr(float(n.value)) if isinstance(n.value, float) else str(n.value)
    if isinstance(n, ast.Name):
        if n.id not in cols:
            raise FormulaError(f"Unknown metric '{n.id}'. Available: {', '.join(sorted(cols))}")
        return n.id
    if isinstance(n, ast.UnaryOp):
        inner = _node(n.operand, cols)
        if isinstance(n.op, ast.USub):
            return f"(-{inner})"
        if isinstance(n.op, ast.UAdd):
            return f"(+{inner})"
        raise FormulaError("Unsupported unary operator")
    if isinstance(n, ast.BinOp):
        a, b = _node(n.left, cols), _node(n.right, cols)
        if isinstance(n.op, ast.Add):
            return f"({a} + {b})"
        if isinstance(n.op, ast.Sub):
            return f"({a} - {b})"
        if isinstance(n.op, ast.Mult):
            return f"({a} * {b})"
        if isinstance(n.op, ast.Div):
            return f"({a} / NULLIF({b}, 0))"
        if isinstance(n.op, ast.Pow):
            return f"pow({a}, {b})"
        raise FormulaError("Unsupported operator (use + - * / **)")
    if isinstance(n, ast.Call):
        if not isinstance(n.func, ast.Name) or n.keywords:
            raise FormulaError("Unsupported function call")
        name = n.func.id
        if name in WINDOW_FUNCS:
            if len(n.args) != 1 or not isinstance(n.args[0], ast.Name):
                raise FormulaError(f"{name}() takes a single metric name, e.g. {name}(strike_rate)")
            col = _node(n.args[0], cols)
            if name == "z":
                return f"(({col} - avg({col}) OVER ()) / NULLIF(stddev_pop({col}) OVER (), 0))"
            return f"(percent_rank() OVER (ORDER BY {col}) * 100)"
        if name in FUNCS:
            args = ", ".join(_node(a, cols) for a in n.args)
            if not n.args:
                raise FormulaError(f"{name}() needs arguments")
            return f"{FUNCS[name]}({args})"
        raise FormulaError(f"Unknown function '{name}'. Allowed: {', '.join(sorted(list(FUNCS) + list(WINDOW_FUNCS)))}")
    raise FormulaError("Unsupported expression")

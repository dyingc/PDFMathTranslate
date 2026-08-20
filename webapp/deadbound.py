"""Find provably dead clauses in min()/max() bound expressions.

A clause is dead when removing it never changes the call's value. `sizing()`
once read `min(6000, max(150, min(total, total // 5)))`, where the inner `min`
is exactly that: the guard belonged outside the `max`, and misplacing it let
the floor ask for more text than the document contained. Nothing broke,
because the loop consuming the value stopped at the end of the paragraphs
anyway — which is precisely why it survived review. This looks for the rest of
that family.

The test is per-argument — does dropping *this one* change the result — rather
than "which argument wins", since the latter calls both copies of a duplicated
clause dead when only one of them is.

Deadness is decided by evaluating the expression at sample points rather than
by symbolic reasoning, so coverage of the sample grid is the whole ballgame: a
grid that never reaches the crossing reports a live clause as dead. Hence
`breakpoints()`, which derives the interesting values from the literals and
divisors in the expression itself instead of guessing a range. Findings are
still worth reading rather than trusting.

Only expressions built from integer literals, plain names/attributes and
+ - * // % and nested min/max are analysed; anything else (calls, subscripts,
comparisons, floats) is skipped rather than guessed at.
"""

import ast
import itertools
import sys
from pathlib import Path

# Values a free variable is tried at. Small, plus a range wide enough to
# separate `x` from `x // 5` and to straddle the usual literal constants.
NONNEG = [0, 1, 2, 3, 7, 20, 40, 150, 999, 1000, 6000, 100000]
SAMPLES = [-100000, -150, -1] + NONNEG
MAX_VARS = 3          # 12**3 = 1728 assignments per call; beyond that, skip


class Unanalysable(Exception):
    pass


def variables(node):
    """Free variables in an expression, as source text (so a.b is one var)."""
    found = set()

    def walk(n):
        if isinstance(n, ast.Constant):
            if not isinstance(n.value, int) or isinstance(n.value, bool):
                raise Unanalysable()
        elif isinstance(n, (ast.Name, ast.Attribute)):
            found.add(ast.unparse(n))
        elif isinstance(n, ast.BinOp):
            if not isinstance(n.op, (ast.Add, ast.Sub, ast.Mult,
                                     ast.FloorDiv, ast.Mod)):
                raise Unanalysable()
            walk(n.left)
            walk(n.right)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            walk(n.operand)
        elif is_bound_call(n):
            for arg in n.args:
                walk(arg)
        else:
            raise Unanalysable()

    walk(node)
    return found


def is_bound_call(n):
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in ("min", "max") and len(n.args) >= 2
            and not n.keywords and not any(isinstance(a, ast.Starred)
                                           for a in n.args))


def evaluate(node, env):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        return env[ast.unparse(node)]
    if isinstance(node, ast.UnaryOp):
        return -evaluate(node.operand, env)
    if isinstance(node, ast.BinOp):
        left, right = evaluate(node.left, env), evaluate(node.right, env)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise Unanalysable()
        return left // right if isinstance(node.op, ast.FloorDiv) else left % right
    if is_bound_call(node):
        pick = min if node.func.id == "min" else max
        return pick(evaluate(a, env) for a in node.args)
    raise Unanalysable()


def breakpoints(call, base):
    """Sample points where the clauses of this call can actually cross.

    A fixed grid is not enough: `min(max(3000, total // 20), 20000)` only
    reaches its cap at total = 400000, and a grid that stops short of that
    would report the cap as dead. The crossings live at the literals scaled by
    the divisors and multipliers in the expression, so those are derived from
    the source rather than guessed.
    """
    literals, factors = set(), {1}
    for n in ast.walk(call):
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            literals.add(abs(n.value))
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.FloorDiv,
                                                          ast.Mult)):
            for side in (n.left, n.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, int) \
                        and side.value:
                    factors.add(abs(side.value))
    points = set(base)
    for lit in literals:
        for factor in factors:
            for value in (lit * factor, lit // factor):
                # Step by the factor too: floor division flattens ±1, so
                # `total // 20` needs total to move by 20 to change at all.
                points.update({value - factor, value - 1, value,
                               value + 1, value + factor})
    # The ceiling has to clear the largest derived breakpoint, or a literal
    # bigger than the grid (a 32-bit cap, say) gets reported as dead purely
    # because nothing was ever sampled above it.
    top = max([max(base)] + list(literals)) * max(factors) + 1
    points = sorted(p for p in points if min(base) <= p <= top)
    if len(points) > 60:
        # Thin evenly rather than truncating: the large breakpoints are the
        # ones a fixed grid was missing in the first place.
        step = len(points) / 60
        points = [points[int(i * step)] for i in range(60)] + [points[-1]]
    return points


def dead_clauses(call, base):
    """Indices of arguments whose removal never changes the result."""
    samples = breakpoints(call, base)
    names = sorted(variables(call))
    if len(names) > MAX_VARS:
        raise Unanalysable()
    pick = min if call.func.id == "min" else max
    # Dead means: dropping this one argument never changes the result. Asking
    # instead which argument "wins" would call both copies of a duplicated
    # clause dead, when in fact only one of them is.
    alive = set()
    for combo in itertools.product(samples, repeat=len(names)):
        env = dict(zip(names, combo))
        values = [evaluate(a, env) for a in call.args]
        best = pick(values)
        for i in range(len(values)):
            if i not in alive and pick(values[:i] + values[i + 1:]) != best:
                alive.add(i)
        if len(alive) == len(call.args):
            break
    return [i for i in range(len(call.args)) if i not in alive]


def analyse(tree):
    """Findings anywhere in a tree, innermost calls included.

    Walking matters: the defect this exists for sat in the *inner* `min` of a
    three-deep expression whose outer call was faultless.
    """
    for node in ast.walk(tree):
        if not is_bound_call(node):
            continue
        try:
            # Dead over all integers is a proof outright. Dead only over the
            # non-negatives is a proof conditional on the variables being
            # counts or lengths — which most bound expressions are, but the
            # tool cannot know that, so the two are reported apart.
            dead = dead_clauses(node, SAMPLES)
            conditional = [i for i in dead_clauses(node, NONNEG)
                           if i not in dead]
        except (Unanalysable, KeyError, RecursionError):
            continue
        seen = set()
        for level, group in (("dead", dead), ("dead if non-negative",
                                              conditional)):
            for i in group:
                clause = ast.unparse(node.args[i])
                if (node.lineno, clause) in seen:
                    continue
                seen.add((node.lineno, clause))
                yield node.lineno, ast.unparse(node), clause, level


def scan(path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    return analyse(tree)


def main(roots):
    checked = hits = 0
    for root in roots:
        files = sorted(Path(root).rglob("*.py")) if Path(root).is_dir() \
            else [Path(root)]
        for path in files:
            for lineno, expr, clause, level in scan(path):
                hits += 1
                print(f"{path}:{lineno}: {level} `{clause}` in `{expr}`")
            checked += 1
    print(f"\n{checked} files, {hits} findings")


if __name__ == "__main__":
    main(sys.argv[1:] or ["."])

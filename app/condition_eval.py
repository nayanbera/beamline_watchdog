"""
Condition evaluation for PV monitoring.

All condition operators express the ALARM condition — when the function returns
True the PV is in alarm state; False means OK; None means the check could not
be performed (disconnected / type error).
"""


def is_in_alarm(value, operator, threshold, threshold2=None):
    """Return True if PV value satisfies the alarm condition."""
    if value is None:
        return None
    try:
        fv = float(value)
        t1 = float(threshold)
    except (TypeError, ValueError):
        return None

    if operator == '>':
        return fv > t1
    if operator == '<':
        return fv < t1
    if operator == '>=':
        return fv >= t1
    if operator == '<=':
        return fv <= t1
    if operator == '==':
        return abs(fv - t1) < 1e-9
    if operator == '!=':
        return abs(fv - t1) >= 1e-9
    if operator in ('in_range', 'out_range') and threshold2 is not None:
        try:
            t2 = float(threshold2)
        except (TypeError, ValueError):
            return None
        inside = min(t1, t2) <= fv <= max(t1, t2)
        return inside if operator == 'in_range' else not inside
    return None


def evaluate_compound_rule(rule, pv_status_map):
    """
    Evaluate a CompoundRule given a dict mapping pv_id → status string.
    Returns True (alarm), False (ok), or None (cannot determine).
    """
    expr = rule.get_expression()
    logic = expr.get('logic', 'AND')
    conditions = expr.get('conditions', [])

    if not conditions:
        return None

    results = []
    for cond in conditions:
        pv_id = cond.get('pv_id')
        expected = cond.get('expected_status', 'ALARM')
        actual = pv_status_map.get(pv_id)
        if actual in (None, 'UNKNOWN', 'ERROR', 'DISCONNECTED'):
            return None
        results.append(actual == expected)

    if logic == 'AND':
        return all(results)
    if logic == 'OR':
        return any(results)
    return None

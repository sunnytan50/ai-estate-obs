"""Lint for the provisioned Grafana dashboards (`hub/grafana/dashboards/*.json`).

The dashboards are code (spec C4: "provisioned from JSON in the repo -- no
click-built panels"), and the repo is bi-session, so they get the same
regression protection as the collector. Checks:

- every dashboard parses, carries its expected uid, and has unique panel ids
- estate.json: no two top-level panels overlap on the grid
- estate.json: the pushed cumulative counters (`aiobs_tokens_total`,
  `aiobs_cost_usd_total`) are never read through `increase()` -- verified
  unreliable on this day-granular data (README, "Extending the dashboards").
  increase() on the GPU box's scraped counters is fine, even in the same
  expression; the check looks at what each increase() call actually wraps
- estate.json: daily counter bars look FORWARD one day (`offset -1d`) so the
  bar stamped at local midnight D carries D's usage and today is the last
  bar -- the backward form put every bar one day late (verified 2026-09-27)
- estate.json: month-to-date counter panels anchor the month start with
  `@ ${__from:date:seconds}` and look ahead one step (`offset -$__interval`)
  so the value includes the newest push whatever step Grafana picks
- estate.json: each provider wears one colour everywhere it is overridden
- estate.json: every `$var` a query references is a defined template
  variable (or a Grafana built-in), so a renamed variable can't silently
  blank a panel
- estate.json: the model-visibility variables (provider / model / kind)
  exist, are multi-select with an All option, and the `kind` filter is never
  applied to `aiobs_cost_usd_total` (cost series carry no `kind` label, so a
  kind filter would blank every cost panel the moment a kind is selected)
- estate.json: the per-model panels exist by title
"""

import json
import os
import re
import unittest

DASH_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "hub", "grafana", "dashboards")
)

EXPECTED_UIDS = {
    "estate.json": "aiobs-estate",
    "gpu-detail.json": "aiobs-gpu",
    "inference-detail.json": "aiobs-inference",
}

# Grafana built-ins a query may reference without a matching template variable.
BUILTIN_VARS = {
    "__all",
    "__from",
    "__to",
    "__interval",
    "__interval_ms",
    "__range",
    "__range_s",
    "__range_ms",
    "__rate_interval",
    "__dashboard",
    "__name",
}

COUNTER_METRICS = ("aiobs_tokens_total", "aiobs_cost_usd_total")
VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _load(name: str) -> dict:
    with open(os.path.join(DASH_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _walk(panels):
    """Yield every panel, descending into (collapsed) row panels."""
    for panel in panels:
        yield panel
        if panel.get("panels"):
            yield from _walk(panel["panels"])


def _exprs(dash: dict):
    for panel in _walk(dash.get("panels", [])):
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if expr:
                yield panel, expr


def _variables(dash: dict) -> dict:
    return {v["name"]: v for v in dash.get("templating", {}).get("list", [])}


def _call_args(expr: str, func: str):
    """Yield the argument text of every `func(...)` call in expr, balancing parentheses."""
    token = f"{func}("
    i = 0
    while (j := expr.find(token, i)) >= 0:
        k, depth = j + len(token), 1
        while k < len(expr) and depth:
            depth += {"(": 1, ")": -1}.get(expr[k], 0)
            k += 1
        yield expr[j + len(token) : k - 1]
        i = k


class TestAllDashboards(unittest.TestCase):
    def test_every_dashboard_parses_with_expected_uid(self):
        found = {
            name: _load(name).get("uid")
            for name in os.listdir(DASH_DIR)
            if name.endswith(".json")
        }
        self.assertEqual(found, EXPECTED_UIDS)

    def test_panel_ids_unique_within_each_dashboard(self):
        for name in EXPECTED_UIDS:
            ids = [p.get("id") for p in _walk(_load(name)["panels"])]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            self.assertEqual(dupes, [], f"{name}: duplicate panel ids {dupes}")
            self.assertNotIn(None, ids, f"{name}: a panel has no id")


class TestEstateDashboard(unittest.TestCase):
    def setUp(self):
        self.dash = _load("estate.json")

    def test_top_level_panels_do_not_overlap(self):
        rects = []
        for p in self.dash["panels"]:
            g = p["gridPos"]
            rects.append((p["id"], g["x"], g["y"], g["x"] + g["w"], g["y"] + g["h"]))
            self.assertLessEqual(g["x"] + g["w"], 24, f"panel {p['id']} runs off the 24-column grid")
        for i, a in enumerate(rects):
            for b in rects[i + 1 :]:
                overlap = a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]
                self.assertFalse(overlap, f"panels {a[0]} and {b[0]} overlap: {a} vs {b}")

    def test_pushed_counters_never_read_through_increase(self):
        for panel, expr in _exprs(self.dash):
            if any(m in expr for m in COUNTER_METRICS):
                for arg in _call_args(expr, "increase"):
                    for metric in COUNTER_METRICS:
                        self.assertNotIn(
                            metric,
                            arg,
                            f"panel {panel['id']} ({panel.get('title')}) reads {metric} through increase()",
                        )
                self.assertIn(
                    "max_over_time(",
                    expr,
                    f"panel {panel['id']} ({panel.get('title')}) must use the two-point "
                    "max_over_time subtraction",
                )

    def test_daily_counter_bars_look_forward(self):
        for panel in _walk(self.dash["panels"]):
            if panel.get("interval") != "1d":
                continue
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if any(m in expr for m in COUNTER_METRICS):
                    self.assertIn(
                        "offset -1d",
                        expr,
                        f"panel {panel['id']} ({panel.get('title')}): daily counter bars must look forward "
                        "(offset -1d) so the bar at local midnight D is D's usage",
                    )
                    self.assertNotRegex(
                        expr,
                        r"offset 1d\b",
                        f"panel {panel['id']} ({panel.get('title')}): backward offset 1d puts every bar a day late",
                    )

    def test_month_to_date_anchors_and_looks_ahead(self):
        for panel in _walk(self.dash["panels"]):
            if panel.get("timeFrom") != "now/M":
                continue
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if any(m in expr for m in COUNTER_METRICS):
                    self.assertIn("@ ${__from:date:seconds}", expr, f"panel {panel['id']}: month start via @")
                    self.assertIn(
                        "offset -$__interval",
                        expr,
                        f"panel {panel['id']}: now-side must look ahead one step or the value lags a step",
                    )

    def test_each_provider_has_one_colour(self):
        seen = {}
        for panel in _walk(self.dash["panels"]):
            for ov in panel.get("fieldConfig", {}).get("overrides", []):
                matcher = ov.get("matcher", {})
                name = matcher.get("options")
                if matcher.get("id") == "byRegexp" and isinstance(name, str):
                    m = re.fullmatch(r"\^([a-z0-9-]+) / \.\*", name)
                    name = m.group(1) if m else None
                elif matcher.get("id") != "byName":
                    continue
                for prop in ov.get("properties", []):
                    if prop.get("id") == "color" and name:
                        seen.setdefault(name, set()).add(prop["value"].get("fixedColor"))
        for name in ("claude-code", "codex", "droid", "openrouter"):
            self.assertIn(name, seen, f"no colour override for provider {name}")
            self.assertEqual(len(seen[name]), 1, f"provider {name} wears several colours: {seen[name]}")

    def test_every_referenced_variable_is_defined(self):
        defined = set(_variables(self.dash)) | BUILTIN_VARS
        for panel, expr in _exprs(self.dash):
            for var in VAR_RE.findall(expr):
                self.assertIn(
                    var, defined, f"panel {panel['id']} ({panel.get('title')}) references undefined ${var}"
                )

    def test_model_visibility_variables(self):
        variables = _variables(self.dash)
        for name in ("provider", "model", "kind"):
            self.assertIn(name, variables, f"template variable '{name}' missing")
            v = variables[name]
            self.assertTrue(v.get("multi"), f"'{name}' must be multi-select")
            self.assertTrue(v.get("includeAll"), f"'{name}' must offer All")
            self.assertEqual(v.get("allValue"), ".*", f"'{name}' All must expand to a match-everything regex")
        # model depends on provider so the dropdown narrows as the user drills in
        model_query = variables["model"]["query"]
        model_query = model_query["query"] if isinstance(model_query, dict) else model_query
        self.assertIn("$provider", model_query)

    def test_kind_filter_never_applied_to_cost(self):
        for panel, expr in _exprs(self.dash):
            for selector in re.findall(r"aiobs_cost_usd_total\{([^}]*)\}", expr):
                self.assertNotIn(
                    "kind=",
                    selector,
                    f"panel {panel['id']} ({panel.get('title')}) filters cost by kind, "
                    "but cost series carry no kind label",
                )

    def test_model_panels_exist(self):
        titles = {p.get("title") for p in _walk(self.dash["panels"])}
        for title in (
            "Daily Tokens by Model (30d)",
            "Cost per Day by Model (30d)",
            "Model Breakdown (30d)",
            "Cost Month-to-Date",
        ):
            self.assertIn(title, titles)


if __name__ == "__main__":
    unittest.main()

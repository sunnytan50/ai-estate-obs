"""Lint for the provisioned Grafana dashboards (`hub/grafana/dashboards/*.json`).

The dashboards are code (spec C4: "provisioned from JSON in the repo -- no
click-built panels"), and the repo is bi-session, so they get the same
regression protection as the collector. Checks:

- every dashboard parses, carries its expected uid, and has unique panel ids
- every dashboard: no two top-level panels overlap on the grid, and every
  `$var` a query references is a defined template variable (or a Grafana
  built-in), so a renamed variable can't silently blank a panel
- every dashboard: integrate() wraps a raw series selector -- integrate(max(m)[..])
  is an implicit subquery that resamples the series (read 0.85% high against a
  raw-sample trapezoid, 2026-09-27)
- every dashboard: header cards are zoom-proof -- Grafana ignores a panel's
  relative-time override once a drag-zoom makes the dashboard range absolute,
  so a card either pairs its range query (gated with `and on()` to the live
  panel range) with an instant twin pinned to now (`unless on()` + `@`), or is
  instant and pinned to now() outright
- every dashboard: a `[$__interval:<step>]` subquery runs on a panel whose
  min interval is that step (no empty windows, no 100k-points overrun)
- every dashboard: integrate() windows are short (5m or one step) -- a
  whole-day integrate() range query carried the last reading through an
  outage (read 27% high for 10 Sep)
- gpu-detail.json: queries select the box by its `host` label, never by
  `instance` (the exporter listens on loopback, so every box shares one
  instance value), and the daily energy bars look forward one day
- gpu-detail.json: the headline cards are pinned to a short window (or are
  instant) so a long dashboard range never shows an hours-old step as "now",
  and the Exporter / Throttled panels are evaluated now (instant)
- estate.json: the pushed cumulative counters (`aiobs_tokens_total`,
  `aiobs_cost_usd_total`) are never read through `increase()` -- verified
  unreliable on this day-granular data (README, "Extending the dashboards").
  increase() on the GPU box's scraped counters is fine, even in the same
  expression; the check looks at what each increase() call actually wraps
- estate.json: daily counter bars look FORWARD one day (`offset -1d`) so the
  bar stamped at local midnight D carries D's usage and today is the last
  bar -- the backward form put every bar one day late (verified 2026-09-27)
- estate.json: month-to-date counter panels anchor the month start with
  `@ ${__from:date:seconds}` and look ahead two steps (`offset -2i`) so the
  value includes the newest push whatever step Grafana (and VictoriaMetrics'
  UTC re-alignment of 50+-point queries) picks
- estate.json: each provider wears one colour everywhere it is overridden
- inference-detail.json: engine-agnostic -- any panel that reads SGLang also
  reads vLLM (the old dashboard was SGLang-only and went blank when the box
  moved to llama.cpp / vLLM), and the headline cards are pinned or instant
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

COUNTER_METRICS = ("aiobs_tokens_total", "aiobs_cost_usd_total", "aiobs_codex_speed_tokens_total",
                   "aiobs_codex_allowance_estimate_total", "aiobs_codex_purchased_credits_estimate_total")
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

    def test_top_level_panels_do_not_overlap(self):
        for name in EXPECTED_UIDS:
            rects = []
            for p in _load(name)["panels"]:
                g = p["gridPos"]
                rects.append((p["id"], g["x"], g["y"], g["x"] + g["w"], g["y"] + g["h"]))
                self.assertLessEqual(g["x"] + g["w"], 24, f"{name}: panel {p['id']} runs off the 24-column grid")
            for i, a in enumerate(rects):
                for b in rects[i + 1 :]:
                    overlap = a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]
                    self.assertFalse(overlap, f"{name}: panels {a[0]} and {b[0]} overlap: {a} vs {b}")

    def test_every_referenced_variable_is_defined(self):
        for name in EXPECTED_UIDS:
            dash = _load(name)
            defined = set(_variables(dash)) | BUILTIN_VARS
            for panel, expr in _exprs(dash):
                for var in VAR_RE.findall(expr):
                    self.assertIn(
                        var, defined, f"{name}: panel {panel['id']} ({panel.get('title')}) references undefined ${var}"
                    )

    def test_integrate_windows_are_short(self):
        # range queries only: the instant path of integrate() does not carry a reading through a gap
        for name in EXPECTED_UIDS:
            for panel in _walk(_load(name)["panels"]):
                for target in panel.get("targets", []):
                    if target.get("instant") or not target.get("expr"):
                        continue
                    self._check_integrate_windows(name, panel, target["expr"])

    def _check_integrate_windows(self, name, panel, expr):
        if True:
            for arg in _call_args(expr, "integrate"):
                windows = re.findall(r"\[([^\]]+)\]", arg)
                for window in windows:
                    self.assertIn(
                        window,
                        ("5m", "$__interval"),
                        f"{name}: panel {panel['id']} ({panel.get('title')}) integrates over [{window}] -- "
                        "sum 5-minute windows instead: sum_over_time(integrate(m[5m])[<range>:5m])",
                    )

    def test_header_cards_are_zoom_proof(self):
        for name in EXPECTED_UIDS:
            for panel in _load(name)["panels"]:
                if panel.get("type") != "stat" or panel["gridPos"]["y"] != 0:
                    continue
                targets = panel.get("targets", [])
                ranged = [t for t in targets if not t.get("instant")]
                where = f"{name}: card {panel['id']} ({(targets or [{}])[0].get('legendFormat')})"
                if ranged:
                    for t in ranged:
                        self.assertIn("and on()", t["expr"], f"{where}: range query not gated to the live panel range")
                    twins = [t for t in targets if t.get("instant") and "unless on()" in t["expr"] and "@" in t["expr"]]
                    self.assertTrue(twins, f"{where}: no instant twin pinned to now for zoomed ranges")
                else:
                    for t in targets:
                        self.assertIn("now()", t["expr"], f"{where}: instant card not pinned to now()")

    def test_fixed_step_subqueries_match_the_panel_step(self):
        # `[$__interval:<step>]` needs a panel min interval of <step>: shorter outer steps leave windows with no
        # subquery points (gaps), and a finer subquery step overruns VictoriaMetrics' 100k points-per-subquery limit
        # on long ranges (a 15s step errored at 30 days).
        for name in EXPECTED_UIDS:
            for panel, expr in _exprs(_load(name)):
                for step in re.findall(r"\[\$__interval:([0-9]+[smhd])\]", expr):
                    self.assertEqual(
                        panel.get("interval"), step, f"{name}: panel {panel['id']} ({panel.get('title')}) subquery step {step}"
                    )

    def test_integrate_wraps_a_raw_selector(self):
        for name in EXPECTED_UIDS:
            for panel, expr in _exprs(_load(name)):
                for arg in _call_args(expr, "integrate"):
                    self.assertNotRegex(
                        arg,
                        r"\)\s*\[",
                        f"{name}: panel {panel['id']} ({panel.get('title')}) integrates a subquery -- "
                        "aggregate outside: sum(integrate(m[..]))",
                    )


class TestEstateDashboard(unittest.TestCase):
    def setUp(self):
        self.dash = _load("estate.json")

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
                if target.get("instant"):
                    continue
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
                if target.get("instant"):
                    continue
                if any(m in expr for m in COUNTER_METRICS):
                    self.assertIn("@ ${__from:date:seconds}", expr, f"panel {panel['id']}: month start via @")
                    self.assertIn(
                        "offset -2i",
                        expr,
                        f"panel {panel['id']}: now-side must look ahead two steps (VictoriaMetrics re-aligns long "
                        "queries to UTC step multiples, one step can fall short)",
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
            "Daily Tokens by Model",
            "Cost per Day by Model",
            "Model Breakdown (30d)",
            "Cost Month-to-Date",
        ):
            self.assertIn(title, titles)

    def test_astra_speed_estimates_are_separate_from_usd(self):
        panels = {p["id"]: p for p in self.dash["panels"]}
        for i in (201, 202, 203, 204):
            for t in panels[i]["targets"]:
                self.assertTrue(t["instant"])
                self.assertIn("@ now()", t["expr"])
                self.assertIn("2592000", t["expr"])
                self.assertIn('model="gpt-6-astra"', t["expr"])
                self.assertIn('$provider', t["expr"])
                self.assertIn('$model', t["expr"])
                self.assertNotIn("aiobs_cost_usd_total", t["expr"])
        self.assertIn("8×", panels[205]["options"]["content"])
        self.assertIn("6×", panels[205]["options"]["content"])
        self.assertIn("Unknown history is excluded", panels[205]["options"]["content"])
        self.assertIn("not actual quota percentage", panels[205]["options"]["content"])


def _cards_pinned_or_instant(test, dash):
    for panel in dash["panels"]:
        if panel.get("type") != "stat" or panel["gridPos"]["y"] != 0:
            continue
        instant = all(t.get("instant") for t in panel.get("targets", []))
        test.assertTrue(
            panel.get("timeFrom") or instant,
            f"card {panel['id']} follows the dashboard range -- at 30d its last step is hours old",
        )


class TestInferenceDashboard(unittest.TestCase):
    def setUp(self):
        self.dash = _load("inference-detail.json")

    def test_no_panel_is_sglang_only(self):
        for panel in _walk(self.dash["panels"]):
            exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []))
            if "sglang:" in exprs:
                self.assertIn("vllm:", exprs, f"panel {panel['id']} ({panel.get('title')}) reads SGLang but not vLLM")

    def test_headline_cards_are_pinned_or_instant(self):
        _cards_pinned_or_instant(self, self.dash)


class TestGpuDashboard(unittest.TestCase):
    def setUp(self):
        self.dash = _load("gpu-detail.json")

    def test_selects_by_host_not_loopback_instance(self):
        for panel, expr in _exprs(self.dash):
            self.assertNotIn(
                "instance=",
                expr,
                f"panel {panel['id']} ({panel.get('title')}) filters on instance -- every box's exporter is 127.0.0.1",
            )
        for var in _variables(self.dash).values():
            query = var.get("query", "")
            query = query.get("query", "") if isinstance(query, dict) else query
            self.assertNotIn("instance", query, f"variable {var['name']} keys on instance")

    def test_headline_cards_are_pinned_or_instant(self):
        _cards_pinned_or_instant(self, self.dash)

    def test_health_panels_are_evaluated_now(self):
        titles = {p.get("title"): p for p in _walk(self.dash["panels"])}
        for title in ("Exporter", "Throttled"):
            self.assertIn(title, titles)
            for target in titles[title]["targets"]:
                self.assertTrue(target.get("instant"), f"{title} {target['refId']} must be an instant query")

    def test_daily_energy_looks_forward(self):
        for panel in _walk(self.dash["panels"]):
            if panel.get("interval") != "1d":
                continue
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if "integrate(" in expr:
                    self.assertIn("offset -1d", expr, f"panel {panel['id']}: daily energy must look forward")


if __name__ == "__main__":
    unittest.main()

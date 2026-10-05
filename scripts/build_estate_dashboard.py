#!/usr/bin/env python3
"""Build hub/grafana/dashboards/estate.json -- the AI Estate overview.

    python3 scripts/build_estate_dashboard.py            # (re)write estate.json
    python3 scripts/build_estate_dashboard.py --check    # exit 1 when estate.json is stale
    python3 scripts/build_estate_dashboard.py --uid aiobs-estate-preview --out /tmp/preview.json

Spec: docs/superpowers/specs/2026-10-05-aiobs-list-value-readable-dashboard-design.md (section 5).
The local-GPU and pipeline panels are carried over verbatim, by panel id,
from the current estate.json, so their verified queries never drift; every
other panel is generated here. Query rules (workstation/tests/test_dashboards.py):
period totals subtract the counter at the picked range's two ends with
millisecond-exact anchors, daily bars look one day forward, live cards pin now().
"""

import argparse
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "hub", "grafana", "dashboards", "estate.json")
DS = {"type": "prometheus", "uid": "vm"}

VALUE = "aiobs_list_value_usd_total"
FALLBACK = "aiobs_list_value_fallback_usd_total"
TOKENS = "aiobs_usage_tokens_total"
OR_COST = "aiobs_cost_usd_total"
OR_TOKENS = "aiobs_tokens_total"
SPEED = "aiobs_codex_speed_tokens_total"
LIMIT = "aiobs_codex_limit_used_ratio"
RESETS = "aiobs_codex_limit_resets_at_seconds"
CREDITS = "aiobs_codex_credits_balance"

COLORS = {"claude-code": "#d95926", "codex": "#3987e5", "droid": "#199e70",
          "grok": "#c98500", "openrouter": "#9085e9"}
ACCENT, MUTED, GOOD, WARN, BAD = "#5fb7d4", "#898781", "#0ca30c", "#e0b400", "#d03b3b"

FILTER = 'provider=~"$provider",model=~"$model"'
SEL = 'origin="client",' + FILTER
SEL_OR = 'origin="client",provider="openrouter",' + FILTER
TO, FROM = "(${__to} / 1000)", "(${__from} / 1000)"
OFFSET = ("(round(((${__to:date:H} * 3600 + ${__to:date:m} * 60 + ${__to:date:s} - (${__to:date:seconds} % 86400)"
          " + 129600) % 86400 - 43200) / 900) * 900)")
MIDNIGHT = f"(floor((now() + {OFFSET}) / 86400) * 86400 - {OFFSET})"
PIPELINE = ("(((sum((1 - last_over_time(aiobs_lane_up[20m])) or (last_over_time(aiobs_lane_up[30d]) * 0 + 1))"
            " or vector(0)) + (sum((1 - max(last_over_time(up{job=\"gpu\"}[2m])))"
            " or (max(last_over_time(up{job=\"gpu\"}[30d])) * 0 + 1)) or vector(0))) @ now())")

# Carried-over panels: id -> (x, y, w, h) inside their collapsed row.
LOCAL_PANELS = {24: (0, 54, 6, 4), 25: (6, 54, 18, 4), 3: (0, 58, 8, 8), 5: (8, 58, 8, 8),
                4: (16, 58, 8, 8), 19: (0, 66, 8, 8), 28: (8, 66, 8, 8), 8: (16, 66, 8, 8)}
PIPELINE_PANELS = {26: (0, 55, 16, 7), 17: (16, 55, 8, 7), 16: (0, 62, 24, 8)}

TEXT = {
    "value": "What this period's AI usage would cost at the vendors' official API list prices -- Claude Code, "
             "Codex (including Fast and Ultrafast) and Droid -- plus real OpenRouter spend. Claude and Codex "
             "are subscriptions, so this is value used, not money charged. Models with no official price use "
             "tokscale's estimate (Pricing column in Models). Follows the time picker.",
    "tokens": "Every token this period, cache reads included (usually most of them). Follows the time picker.",
    "today": "API list-price value since local midnight, whatever the time picker says.",
    "openrouter": "Real money spent on OpenRouter this period (its own billing data). Follows the time picker.",
    "limit": "Share of your Codex weekly limit used, as OpenAI reported it on your latest Codex request, and the "
             "time until it resets. Reads 0% once a reset has passed, until Codex is used again.",
    "credits": "Purchased Codex credits left, from your latest Codex request.",
    "pipeline": "Data paths down right now: each collector lane (fresh within 20 minutes) and the GPU exporter "
                "(2 minutes). Details in Pipeline & Hermes at the bottom.",
    "per_day": "API list-price value per local day, stacked by provider; today is the last bar. A day's numbers "
               "freeze once it is two days old.",
    "by_provider": "API list-price value per provider for the period (OpenRouter: real spend).",
    "models": "Every model used this period, ranked by API value. Output includes reasoning tokens. Pricing: "
              "list price = official vendor rate; estimate = no official rate, tokscale's estimate; real spend "
              "= OpenRouter's own bill.",
    "per_model": "The 10 biggest models over the period, one bar per local day; each model is a shade of its "
                 "provider's colour.",
    "tokens_day": "Tokens per local day by provider.",
    "kinds": "Where this period's tokens went. Cache reads are context re-sent from the provider's cache, billed "
             "at a fraction of the input price.",
    "speed": "Astra tokens this period by speed tier. Fast costs 2x and Ultrafast 6x the Standard API price "
             "(your plan allowance burns 2.5x / 8x). Speed is recorded from 26 Sep 2026; earlier usage shows "
             "as Unknown and is valued at Standard.",
    "limits": "Weekly limit used (left axis) and purchased credits left (right axis), from your Codex logs.",
}


def period(metric, sel, by=None):
    """Counter growth over the picked range: the counter at its end minus at its start."""
    end = f"max_over_time({metric}{{{sel}}}[400d] @ {TO})"
    start = f"max_over_time({metric}{{{sel}}}[400d] @ {FROM})"
    inner = f"{end} - ({start} or {end} * 0)"
    return f"sum by ({by})({inner})" if by else f"sum({inner})"


def since_midnight(metric, sel):
    end = f"max_over_time({metric}{{{sel}}}[400d] @ now())"
    start = f"max_over_time({metric}{{{sel}}}[400d] @ {MIDNIGHT})"
    return f"sum({end} - ({start} or {end} * 0))"


def daily(metric, sel, by):
    """Forward-looking daily delta: the bar at local midnight D carries D's usage."""
    ahead = f"max_over_time({metric}{{{sel}}}[400d] offset -1d)"
    here = f"max_over_time({metric}{{{sel}}}[400d])"
    return f"(sum by ({by})({ahead} - ({here} or {ahead} * 0)) > 0)"


def union(a, b):
    """Two series sets whose label sets never overlap (the provider differs)."""
    return f"({a} or {b})"


def add(a, b):
    """Two series sets that may share label sets: their sum, keeping one-sided series."""
    return f"(({a} or {b} * 0) + ({b} or {a} * 0))"


def scalar_sum(a, b):
    return f"({a} or vector(0)) + ({b} or vector(0))"


def target(ref, expr, legend, instant=False, table=False):
    t = {"datasource": DS, "refId": ref, "expr": expr, "legendFormat": legend,
         "instant": instant, "range": not instant}
    if table:
        t["format"] = "table"
    return t


def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": color, "value": value} for value, color in pairs]}


def override(name, *props, regexp=False):
    return {"matcher": {"id": "byRegexp" if regexp else "byName", "options": name},
            "properties": [{"id": key, "value": value} for key, value in props]}


def provider_colors(shades=False):
    if shades:
        return [override(f"^{p} / .*", ("color", {"mode": "shades", "fixedColor": c}), regexp=True)
                for p, c in COLORS.items()]
    return [override(p, ("color", {"mode": "fixed", "fixedColor": c})) for p, c in COLORS.items()]


def card(pid, x, w, exprs, unit, decimals, text, color=ACCENT, thresholds=None, mappings=None, overrides=None,
         value_size=26):
    return {
        "id": pid, "type": "stat", "title": "", "description": text, "datasource": DS,
        "gridPos": {"x": x, "y": 0, "w": w, "h": 4},
        "targets": [target(chr(65 + i), expr, legend, instant=True) for i, (legend, expr) in enumerate(exprs)],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "orientation": "auto", "textMode": "value_and_name", "wideLayout": False,
                    "colorMode": "value" if thresholds else "none", "graphMode": "none",
                    "justifyMode": "auto", "showPercentChange": False,
                    "text": {"valueSize": value_size, "titleSize": 13}},
        "fieldConfig": {"defaults": {
            "color": {"mode": "thresholds"} if thresholds else {"mode": "fixed", "fixedColor": color},
            "unit": unit, "decimals": decimals, "mappings": mappings or [],
            "thresholds": thresholds or steps((None, "green"))}, "overrides": overrides or []},
    }


BAR_STYLE = {"drawStyle": "bars", "lineInterpolation": "smooth", "lineWidth": 0, "fillOpacity": 88,
             "gradientMode": "none", "barAlignment": 0, "barWidthFactor": 0.62, "showPoints": "never",
             "pointSize": 5, "spanNulls": False, "insertNulls": False, "axisPlacement": "auto",
             "axisBorderShow": False, "axisCenteredZero": False, "axisColorMode": "text", "axisSoftMin": 0,
             "scaleDistribution": {"type": "linear"}, "stacking": {"mode": "normal", "group": "A"},
             "thresholdsStyle": {"mode": "off"}, "hideFrom": {"legend": False, "tooltip": False, "viz": False}}


def bars(pid, pos, title, text, expr, legend, unit, shades=False, legend_table=False):
    x, y, w, h = pos
    legend_opts = ({"showLegend": True, "displayMode": "table", "placement": "right", "calcs": ["sum"],
                    "sortBy": "Total", "sortDesc": True, "width": 380} if legend_table
                   else {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []})
    return {"id": pid, "type": "timeseries", "title": title, "description": text, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "interval": "1d",
            "targets": [target("A", expr, legend)],
            "options": {"legend": legend_opts, "tooltip": {"mode": "multi", "sort": "desc", "hideZeros": True}},
            "fieldConfig": {"defaults": {"color": {"mode": "palette-classic-by-name"}, "unit": unit,
                                         "custom": copy.deepcopy(BAR_STYLE), "mappings": [],
                                         "thresholds": steps((None, "green"))},
                            "overrides": provider_colors(shades)}}


def bar_list(pid, pos, title, text, expr, legend, unit, decimals, overrides):
    x, y, w, h = pos
    return {"id": pid, "type": "bargauge", "title": title, "description": text, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [target("A", expr, legend, instant=True)],
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "orientation": "horizontal", "displayMode": "basic", "valueMode": "text",
                        "namePlacement": "left", "showUnfilled": True, "sizing": "manual",
                        "minVizWidth": 8, "minVizHeight": 18, "maxVizHeight": 36,
                        "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom", "calcs": []},
                        "text": {"titleSize": 13, "valueSize": 17}},
            "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": MUTED}, "unit": unit,
                                         "min": 0, "decimals": decimals, "mappings": [],
                                         "thresholds": steps((None, "green"))},
                            "overrides": overrides}}


def row(pid, y, title, collapsed=False, panels=None):
    return {"id": pid, "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels or []}


def header_cards():
    # `@ now()` sits inside the window: VictoriaMetrics rounds an outer `f(m[w]) @ t` back to an earlier
    # grid point, which hid the reset time (its only samples are minutes old).
    used = f'last_over_time({LIMIT}{{window="weekly"}}[2h] @ now())'
    # `> 0` hides a reset time that has already passed (the last sample stays in the window).
    resets = f'(last_over_time({RESETS}{{window="weekly"}}[2h] @ now()) - now()) > 0'
    pipeline_map = [{"type": "value", "options": {"0": {"text": "✓ All up", "color": GOOD, "index": 0},
                                                  **{str(n): {"text": f"✕ {n} down", "color": BAD, "index": n}
                                                     for n in range(1, 7)}}}]
    return [
        card(101, 0, 4, [("API value", scalar_sum(period(VALUE, SEL), period(OR_COST, SEL_OR)))],
             "currencyUSD", 2, TEXT["value"]),
        card(102, 4, 3, [("Tokens", scalar_sum(period(TOKENS, SEL), period(OR_TOKENS, SEL_OR)))],
             "short", 1, TEXT["tokens"]),
        card(103, 7, 3, [("Today", scalar_sum(since_midnight(VALUE, SEL), since_midnight(OR_COST, SEL_OR)))],
             "currencyUSD", 2, TEXT["today"]),
        card(104, 10, 3, [("OpenRouter", f"{period(OR_COST, SEL_OR)} or vector(0)")],
             "currencyUSD", 2, TEXT["openrouter"], color=COLORS["openrouter"]),
        card(105, 13, 5, [("Codex limit", used), ("resets in", resets)], "percentunit", 0, TEXT["limit"],
             thresholds=steps((None, GOOD), (0.7, WARN), (0.9, BAD)),
             overrides=[override("resets in", ("unit", "s"), ("decimals", 1),
                                 ("color", {"mode": "fixed", "fixedColor": MUTED}))], value_size=22),
        card(106, 18, 3, [("Codex credits", f"last_over_time({CREDITS}[2h] @ now())")], "short", 1,
             TEXT["credits"]),
        card(107, 21, 3, [("Pipeline", PIPELINE)], "none", 0, TEXT["pipeline"],
             thresholds=steps((None, GOOD), (1, BAD)), mappings=pipeline_map, value_size=22),
    ]


def models_table(pid, pos):
    by = "provider,model"
    value = union(period(VALUE, SEL, by), period(OR_COST, SEL_OR, by))
    tokens = union(period(TOKENS, SEL, by), period(OR_TOKENS, SEL_OR, by))
    output = union(period(TOKENS, SEL + ',kind="output"', by), period(OR_TOKENS, SEL_OR + ',kind="output"', by))
    pricing = union(f"({period(FALLBACK, SEL, by)} > 0) * 0 + 1", f"({period(OR_COST, SEL_OR, by)} > 0) * 0 + 2")
    exprs = {"A": tokens, "B": output, "C": value, "D": f"{value} / scalar(sum({value}))",
             "E": f"{value} / {tokens} * 1e6", "F": pricing}
    names = {"A": "Tokens", "B": "Output", "C": "API value", "D": "Share", "E": "$ / 1M tokens", "F": "Pricing"}
    order = ["C", "D", "A", "B", "E", "F"]
    x, y, w, h = pos
    pills = {p: {"color": c, "index": i} for i, (p, c) in enumerate(COLORS.items())}
    pricing_map = [{"type": "value", "options": {"1": {"text": "estimate", "color": WARN, "index": 0},
                                                 "2": {"text": "real spend", "color": COLORS["openrouter"],
                                                       "index": 1}}},
                   {"type": "special", "options": {"match": "null",
                                                   "result": {"text": "list price", "color": MUTED, "index": 2}}}]
    return {
        "id": pid, "type": "table", "title": "Models", "description": TEXT["models"], "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(ref, exprs[ref], "__auto", instant=True, table=True) for ref in "ABCDEF"],
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""}},
        "fieldConfig": {"defaults": {"color": {"mode": "thresholds"}, "unit": "short",
                                     "custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                                "filterable": True, "inspect": False},
                                     "thresholds": steps((None, "text")), "mappings": []},
                        "overrides": [
                            override("Provider", ("custom.width", 130), ("mappings", [{"type": "value", "options": pills}]),
                                     ("custom.cellOptions", {"type": "pill"})),
                            override("Model", ("custom.minWidth", 220)),
                            override("API value", ("custom.width", 120), ("unit", "currencyUSD"), ("decimals", 2),
                                     ("custom.align", "right")),
                            override("Share", ("custom.width", 170), ("unit", "percentunit"), ("decimals", 1),
                                     ("min", 0), ("max", 1), ("color", {"mode": "fixed", "fixedColor": MUTED}),
                                     ("custom.cellOptions", {"type": "gauge", "mode": "basic",
                                                             "valueDisplayMode": "text"})),
                            override("Tokens", ("custom.width", 120), ("decimals", 2), ("custom.align", "right")),
                            override("Output", ("custom.width", 120), ("decimals", 2), ("custom.align", "right")),
                            override("$ / 1M tokens", ("custom.width", 130), ("unit", "currencyUSD"),
                                     ("decimals", 2), ("custom.align", "right")),
                            override("Pricing", ("custom.width", 110), ("mappings", pricing_map),
                                     ("custom.cellOptions", {"type": "color-text"})),
                        ]},
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "sortBy", "options": {"sort": [{"field": "Time", "desc": False}]}},
            {"id": "groupBy", "options": {"fields": {
                "provider": {"aggregations": [], "operation": "groupby"},
                "model": {"aggregations": [], "operation": "groupby"},
                **{f"Value #{ref}": {"aggregations": ["lastNotNull"], "operation": "aggregate"} for ref in "ABCDEF"}}}},
            {"id": "organize", "options": {
                "excludeByName": {},
                "indexByName": {"provider": 0, "model": 1,
                                **{f"Value #{ref} (lastNotNull)": i + 2 for i, ref in enumerate(order)}},
                "renameByName": {"provider": "Provider", "model": "Model",
                                 **{f"Value #{ref} (lastNotNull)": names[ref] for ref in "ABCDEF"}}}},
            {"id": "filterByValue", "options": {"filters": [{"fieldName": "Tokens", "config": {
                "id": "greater", "options": {"value": 0}}}], "type": "include", "match": "all"}},
            {"id": "sortBy", "options": {"sort": [{"field": "API value", "desc": True}]}},
            {"id": "limit", "options": {"limitField": 25}},
        ],
    }


def limits_chart(pid, pos):
    x, y, w, h = pos
    line = {"drawStyle": "line", "lineInterpolation": "stepAfter", "lineWidth": 2, "fillOpacity": 10,
            "gradientMode": "opacity", "showPoints": "never", "pointSize": 4, "spanNulls": True,
            "insertNulls": False, "axisPlacement": "auto", "axisBorderShow": False, "axisCenteredZero": False,
            "axisColorMode": "text", "scaleDistribution": {"type": "linear"},
            "stacking": {"mode": "none", "group": "A"}, "thresholdsStyle": {"mode": "off"},
            "hideFrom": {"legend": False, "tooltip": False, "viz": False}}
    return {"id": pid, "type": "timeseries", "title": "Codex weekly limit & credits", "description": TEXT["limits"],
            "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [target("A", f'max(max_over_time({LIMIT}{{window="weekly"}}[$__interval]))', "weekly limit used"),
                        target("B", f"max(last_over_time({CREDITS}[$__interval]))", "credits left")],
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
                        "tooltip": {"mode": "multi", "sort": "none"}},
            "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": COLORS["codex"]}, "unit": "short",
                                         "custom": line, "mappings": [], "thresholds": steps((None, "green"))},
                            "overrides": [
                                override("weekly limit used", ("unit", "percentunit"), ("min", 0), ("max", 1),
                                         ("decimals", 0), ("color", {"mode": "fixed", "fixedColor": COLORS["codex"]})),
                                override("credits left", ("custom.axisPlacement", "right"), ("decimals", 0),
                                         ("color", {"mode": "fixed", "fixedColor": MUTED})),
                            ]}}


def body_panels():
    kinds = {"cache_read": "Cache reads", "input": "Input", "output": "Output (incl. reasoning)",
             "cache_write": "Cache writes"}
    speeds = {"standard": ("Standard (1×)", "#9ec5f4"), "fast": ("Fast (2×)", "#3987e5"),
              "ultrafast": ("Ultrafast (6×)", "#184f95"), "unknown": ("Unknown (before 26 Sep)", "#55534e")}
    astra = 'origin="client",provider="codex",model="gpt-6-astra",billing!="api",' + FILTER
    return [
        row(300, 4, "Where the value went"),
        bars(310, (0, 5, 16, 9), "API value per day", TEXT["per_day"],
             union(daily(VALUE, SEL, "provider"), daily(OR_COST, SEL_OR, "provider")), "{{provider}}", "currencyUSD"),
        bar_list(311, (16, 5, 8, 9), "By provider", TEXT["by_provider"],
                 f"sort_desc({union(period(VALUE, SEL, 'provider'), period(OR_COST, SEL_OR, 'provider'))} > 0)",
                 "{{provider}}", "currencyUSD", 2, provider_colors()),
        row(301, 14, "Models"),
        models_table(312, (0, 15, 24, 10)),
        bars(313, (0, 25, 24, 10), "API value per day by model", TEXT["per_model"],
             f"topk_max(10, {union(daily(VALUE, SEL, 'provider,model'), daily(OR_COST, SEL_OR, 'provider,model'))})",
             "{{provider}} / {{model}}", "currencyUSD", shades=True, legend_table=True),
        row(302, 35, "Tokens"),
        bars(314, (0, 36, 16, 8), "Tokens per day", TEXT["tokens_day"],
             union(daily(TOKENS, SEL, "provider"), daily(OR_TOKENS, SEL_OR, "provider")), "{{provider}}", "short"),
        bar_list(315, (16, 36, 8, 8), "Tokens by kind", TEXT["kinds"],
                 f"sort_desc({add(period(TOKENS, SEL, 'kind'), period(OR_TOKENS, SEL_OR, 'kind'))} > 0)",
                 "{{kind}}", "short", 2,
                 [override(k, ("displayName", v)) for k, v in kinds.items()]),
        row(303, 44, "Codex"),
        bar_list(316, (0, 45, 8, 8), "Astra speed mix", TEXT["speed"],
                 f"sort_desc({period(SPEED, astra, 'speed')} > 0)", "{{speed}}", "short", 2,
                 [override(k, ("displayName", name), ("color", {"mode": "fixed", "fixedColor": color}))
                  for k, (name, color) in speeds.items()]),
        limits_chart(317, (8, 45, 16, 8)),
    ]


def _walk(panels):
    for panel in panels:
        yield panel
        yield from _walk(panel.get("panels") or [])


def build(existing: dict, uid: str) -> dict:
    carried = {p["id"]: p for p in _walk(existing.get("panels", []))}

    def carry(pid, pos):
        if pid not in carried:
            raise SystemExit(f"panel {pid} is missing from the current estate.json")
        panel = copy.deepcopy(carried[pid])
        x, y, w, h = pos
        panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        return panel

    panels = header_cards() + body_panels()
    panels.append(row(304, 53, "Local GPU & inference", collapsed=True,
                      panels=[carry(pid, pos) for pid, pos in LOCAL_PANELS.items()]))
    panels.append(row(305, 54, "Pipeline & Hermes", collapsed=True,
                      panels=[carry(pid, pos) for pid, pos in PIPELINE_PANELS.items()]))

    def variable(name, label, expr, text):
        query = f"query_result(sum by ({name})({expr}))"
        return {"type": "query", "name": name, "label": label, "description": text, "datasource": DS,
                "definition": query, "query": query, "regex": f'/{name}="([^"]+)"/', "refresh": 1, "sort": 1,
                "includeAll": True, "multi": True, "allValue": ".*",
                "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
                "options": [], "hide": 0, "skipUrlSync": False}

    def link(title, url, tip, period_link):
        return {"asDropdown": False, "icon": "dashboard" if period_link else "external link",
                "includeVars": period_link, "keepTime": not period_link, "tags": [], "targetBlank": False,
                "title": title, "tooltip": tip, "type": "link", "url": url}

    base = "/d/aiobs-estate/ai-estate"
    return {
        "id": None, "uid": uid, "title": "AI Estate" if uid == "aiobs-estate" else "AI Estate (preview)",
        "description": "What your AI usage is worth at official API list prices, where it went (provider, model, "
                       "tokens), your real Codex limits and the local GPU box. Pick a period with the time picker "
                       "or the This month / Last month buttons; totals follow it. Spec: docs/superpowers/specs/"
                       "2026-10-05-aiobs-list-value-readable-dashboard-design.md.",
        "tags": ["aiobs"], "timezone": "browser", "editable": True, "graphTooltip": 1, "schemaVersion": 39,
        "version": existing.get("version", 1), "refresh": "5m",
        "time": {"from": "now/M", "to": "now"},
        "timepicker": {"refresh_intervals": ["1m", "5m", "15m", "1h"]},
        "fiscalYearStartMonth": 0, "liveNow": False,
        "links": [link("This month", f"{base}?from=now%2FM&to=now", "This calendar month so far", True),
                  link("Last month", f"{base}?from=now-1M%2FM&to=now-1M%2FM", "The whole previous calendar month", True),
                  link("Last 30 days", f"{base}?from=now-30d&to=now", "A rolling 30 days", True),
                  link("GPU Detail", "/d/aiobs-gpu/gpu-detail", "Open the GPU detail dashboard", False),
                  link("Inference Detail", "/d/aiobs-inference/inference-detail",
                       "Open the inference detail dashboard", False)],
        "templating": {"list": [
            variable("provider", "Provider",
                     f'max_over_time({VALUE}{{origin="client"}}[400d]) or '
                     f'max_over_time({OR_COST}{{origin="client",provider="openrouter"}}[400d])',
                     "Providers with any priced usage in the last 400 days."),
            variable("model", "Model",
                     f'max_over_time({VALUE}{{origin="client",provider=~"$provider"}}[400d]) or '
                     f'max_over_time({OR_COST}{{origin="client",provider="openrouter",provider=~"$provider"}}[400d])',
                     "Models of the selected providers with any priced usage in the last 400 days."),
        ]},
        "annotations": existing.get("annotations", {"list": []}),
        "panels": panels,
    }


def render(dash: dict) -> str:
    return json.dumps(dash, indent=2, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the AI Estate dashboard JSON.")
    parser.add_argument("--check", action="store_true", help="exit 1 when the output file is stale")
    parser.add_argument("--uid", default="aiobs-estate")
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    with open(DEFAULT_OUT, encoding="utf-8") as handle:
        existing = json.load(handle)
    text = render(build(existing, args.uid))
    if args.check:
        with open(args.out, encoding="utf-8") as handle:
            if handle.read() != text:
                print(f"{args.out} is stale: run python3 scripts/build_estate_dashboard.py", file=sys.stderr)
                return 1
        return 0
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

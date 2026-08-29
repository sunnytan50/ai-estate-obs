"""aiobs_collector: workstation observability collector for the AI estate.

The sample schema, Prometheus exposition renderer, estate.env config loader,
JSON state I/O, and the per-lane run harness live in `aiobs_collector.core`
(Task 8) -- that is the module later tasks import from:

    from aiobs_collector.core import Sample, Lane, run_lanes, ...

Lane implementations (tokscale, OpenRouter) and the push/backfill/launchd
entrypoint are added by later tasks as sibling modules in this package.
"""

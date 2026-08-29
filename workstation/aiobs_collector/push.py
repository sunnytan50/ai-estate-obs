"""Push collector samples to VictoriaMetrics.

Stdlib only (urllib). POSTs the Prometheus text exposition for a batch of
`Sample`s to VictoriaMetrics's `/api/v1/import/prometheus` endpoint, which is
idempotent on identical series+timestamp -- that idempotency is what makes
`--backfill` (Task 11's `__main__.py`) safe to replay.
"""

import urllib.error
import urllib.request

from aiobs_collector.core import Sample, render_exposition

_IMPORT_PATH = "/api/v1/import/prometheus"
_TIMEOUT_S = 60
_BODY_PREFIX_CHARS = 200


def push_samples(vm_base_url: str, samples: list[Sample]) -> None:
    """POST `samples` to `{vm_base_url}/api/v1/import/prometheus`.

    `vm_base_url` is joined with exactly one `/` regardless of whether it
    already ends in one. The body is `render_exposition(samples)` encoded as
    UTF-8 (an empty sample list posts an empty body -- harmless, VM treats
    it as a no-op import).

    Raises `RuntimeError` on any non-2xx response (urllib itself raises
    `HTTPError`, a `URLError` subclass, for those) or on a lower-level
    transport failure (`URLError`: DNS, connection refused, timeout, ...).
    In both cases the message names what's known: for an HTTP failure, the
    status code and the first 200 characters of the response body; for a
    transport failure, `URLError.reason`. Never includes request headers or
    any secret -- there are none in a Sample.
    """
    url = vm_base_url.rstrip("/") + _IMPORT_PATH
    body = render_exposition(samples).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S):
            pass  # 2xx: urlopen returns normally, nothing else to do
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"push to {url} failed: HTTP {exc.code}: {resp_body[:_BODY_PREFIX_CHARS]}"
        ) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"push to {url} failed: {exc.reason}") from None

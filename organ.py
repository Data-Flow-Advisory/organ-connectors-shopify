#!/usr/bin/env python3
"""
Shopify-Connector Organ — extracted decision logic from discovery-engine
``lib/dataflow_core/connectors/shopify.py``.

A pure, stdlib-only decider that reads ``{state, context}`` JSON on stdin (or
the file named by ``ORGAN_INPUT``) and writes ``{output, rationale,
self_metric}`` on stdout, per the orchestrator
[`CONTRACT.md`](https://github.com/Data-Flow-Advisory/orchestrator).

WHAT IT DECIDES
---------------
``ShopifyConnector`` braids two things together: side effects (build a base
URL, fire ``requests.get`` at the Shopify Admin API) and *pure judgement* (is
the connector configured? how does a raw Shopify payload map onto the
connector's normalized shape?). This organ is the judgement, sliced out.

The side effects — the actual HTTP calls — are **excluded**. The organ is
*handed* the facts: the store config (``store_url`` / token-present flag, or
the raw env values), and — for the projection ops — the **already-fetched**
raw Shopify JSON (``products`` / ``orders`` / ``customers`` arrays). It builds
the readiness gate and the normalized projections that the original
``connect`` / ``get_products`` / ``get_order`` / ``get_customer`` /
``summarise`` / ``query`` methods compute. It makes no network call, reads no
env on its own behalf (the caller passes env values in ``context``), and never
raises.

CONTRACT
--------
INPUT::

    {
      "state": {
        "op": "config_gate",            # which decision; default "config_gate"
        "store_url": "mystore.myshopify.com",
        "access_token": "shpat_...",    # presence only — never echoed
        "products": [ {raw Shopify product}, ... ],   # for normalize_products/summarise/query
        "orders":   [ {raw Shopify order}, ... ],      # for normalize_order
        "customers":[ {raw Shopify customer}, ... ],   # for normalize_customer
        "limit": 50
      },
      "context": {
        "env_store_url": "...",         # SHOPIFY_STORE_URL fallback
        "env_access_token": "...",      # SHOPIFY_ACCESS_TOKEN fallback (presence only)
        "api_version": "2024-01"
      }
    }

ops:
  - ``config_gate``       — is the connector configured? build base_url. (default)
  - ``normalize_products``— project raw products onto {id,title,description,price,available,url}
  - ``normalize_order``   — pick & project the first matching order (None if empty)
  - ``normalize_customer``— pick & project the first matching customer (None if empty)
  - ``summarise``         — store summary {source, product_count, products[:10]}
  - ``query``             — tabular projection {columns, rows, row_count}

OUTPUT::

    {
      "output": { "op": "...", "decision": "...", ...op-specific... },
      "rationale": "...",
      "self_metric": { "confidence": 0.95, ... }
    }

Fail-safe: malformed/empty input fails to a low-confidence ``refuse`` (for the
gate) or an empty projection (for the data ops); ``decide()`` never raises.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

DEFAULT_API_VERSION = "2024-01"

KNOWN_OPS = (
    "config_gate",
    "normalize_products",
    "normalize_order",
    "normalize_customer",
    "summarise",
    "query",
)

QUERY_COLUMNS = ["id", "title", "price", "available"]


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------

def _str(value: Any) -> str:
    """Coerce to a stripped string; '' for None/non-str-coercible."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_list(value: Any) -> List[Any]:
    """Return value if it's a list, else []."""
    return value if isinstance(value, list) else []


def _present(value: Any) -> bool:
    """Truthy non-empty signal — used for token/credential presence."""
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def resolve_store_config(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the effective store config, mirroring ``__init__``/``connect``.

    ``store_url`` is taken from state, falling back to ``context.env_store_url``
    (the caller's ``SHOPIFY_STORE_URL``). Token presence likewise. The base URL
    is built exactly as the connector does:
    ``https://{store_url}/admin/api/{api_version}``.
    """
    store_url = _str(state.get("store_url")) or _str(context.get("env_store_url"))

    token_present = _present(state.get("access_token"))
    if not token_present:
        token_present = _present(context.get("env_access_token"))

    api_version = _str(state.get("api_version")) or _str(context.get("api_version")) or DEFAULT_API_VERSION

    base_url = f"https://{store_url}/admin/api/{api_version}" if store_url else ""

    return {
        "store_url": store_url,
        "token_present": token_present,
        "api_version": api_version,
        "base_url": base_url,
    }


def normalize_product(p: Any, store_url: str) -> Dict[str, Any]:
    """Project one raw Shopify product onto the connector's normalized shape.

    Mirrors ``get_products``: price is the first variant's price (or ''),
    ``available`` is true iff any variant has ``inventory_quantity > 0``,
    and the URL is built from the store + product handle.
    """
    if not isinstance(p, dict):
        p = {}
    variants = _as_list(p.get("variants"))
    first_variant = variants[0] if variants and isinstance(variants[0], dict) else {}
    price = _str(first_variant.get("price")) if first_variant else ""
    available = any(
        isinstance(v, dict) and _to_int(v.get("inventory_quantity")) > 0
        for v in variants
    )
    handle = _str(p.get("handle"))
    url = f"https://{store_url}/products/{handle}" if (store_url and handle) else ""
    return {
        "id": p.get("id"),
        "title": _str(p.get("title")),
        "description": p.get("body_html", "") if p.get("body_html") is not None else "",
        "price": price,
        "available": available,
        "url": url,
    }


def _to_int(value: Any) -> int:
    """Best-effort int coercion; 0 on failure (matches the >0 inventory check)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_products(raw: Any, store_url: str) -> List[Dict[str, Any]]:
    """Project a raw products array."""
    return [normalize_product(p, store_url) for p in _as_list(raw)]


def normalize_order(orders: Any) -> Optional[Dict[str, Any]]:
    """Pick the first order and project it; None when the array is empty.

    Mirrors ``get_order``: ``status`` falls back to 'unfulfilled', tracking is
    read from the first fulfillment's ``tracking_url`` (None if no fulfillments).
    """
    order_list = _as_list(orders)
    if not order_list:
        return None
    order = order_list[0]
    if not isinstance(order, dict):
        return None
    fulfillments = _as_list(order.get("fulfillments"))
    tracking = None
    if fulfillments and isinstance(fulfillments[0], dict):
        tracking = fulfillments[0].get("tracking_url")
    line_items = _as_list(order.get("line_items"))
    return {
        "id": order.get("id"),
        "number": order.get("name"),
        "status": order.get("fulfillment_status") or "unfulfilled",
        "total": order.get("total_price"),
        "created_at": order.get("created_at"),
        "items": [
            {"title": li.get("title"), "quantity": li.get("quantity")}
            for li in line_items
            if isinstance(li, dict)
        ],
        "tracking": tracking,
    }


def normalize_customer(customers: Any) -> Optional[Dict[str, Any]]:
    """Pick the first customer and project it; None when the array is empty.

    Mirrors ``get_customer``: ``name`` is ``first last`` stripped.
    """
    customer_list = _as_list(customers)
    if not customer_list:
        return None
    c = customer_list[0]
    if not isinstance(c, dict):
        return None
    name = f"{_str(c.get('first_name'))} {_str(c.get('last_name'))}".strip()
    return {
        "id": c.get("id"),
        "name": name,
        "email": c.get("email"),
        "orders_count": c.get("orders_count", 0),
        "total_spent": c.get("total_spent", "0"),
    }


def summarise_products(raw: Any, store_url: str) -> Dict[str, Any]:
    """Store summary, mirroring ``summarise`` (top-10 title/price)."""
    products = normalize_products(raw, store_url)
    return {
        "source": f"Shopify store: {store_url}",
        "product_count": len(products),
        "products": [{"title": p["title"], "price": p["price"]} for p in products[:10]],
    }


def build_query_result(raw: Any, store_url: str) -> Dict[str, Any]:
    """Tabular projection, mirroring ``query`` (id/title/price/available)."""
    products = normalize_products(raw, store_url)
    rows = [[p["id"], p["title"], p["price"], p["available"]] for p in products]
    return {
        "columns": list(QUERY_COLUMNS),
        "rows": rows,
        "row_count": len(rows),
    }


# ---------------------------------------------------------------------------
# decider
# ---------------------------------------------------------------------------

def decide(state: Optional[Dict[str, Any]], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pure decider — return ``{output, rationale, self_metric}``. Never raises."""
    try:
        state = state if isinstance(state, dict) else {}
        context = context if isinstance(context, dict) else {}

        op = _str(state.get("op")) or "config_gate"
        if op not in KNOWN_OPS:
            return _unknown_op(op)

        cfg = resolve_store_config(state, context)
        store_url = cfg["store_url"]

        if op == "config_gate":
            return _decide_config_gate(cfg)
        if op == "normalize_products":
            return _decide_normalize_products(state, store_url)
        if op == "normalize_order":
            return _decide_normalize_order(state)
        if op == "normalize_customer":
            return _decide_normalize_customer(state)
        if op == "summarise":
            return _decide_summarise(state, store_url)
        if op == "query":
            return _decide_query(state, store_url)

        return _unknown_op(op)  # pragma: no cover - guarded above

    except Exception as exc:  # pragma: no cover - defensive
        return {
            "output": {"op": "error", "decision": "refuse", "error": True},
            "rationale": f"Error during decision; failed safe: {exc}",
            "self_metric": {"confidence": 0.0},
        }


def _decide_config_gate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    store_url = cfg["store_url"]
    token_present = cfg["token_present"]
    has_store = bool(store_url)
    configured = has_store and token_present

    if not has_store:
        decision, reason, confidence = "refuse", "missing_store_url", 0.3
    elif not token_present:
        decision, reason, confidence = "refuse", "missing_access_token", 0.9
    else:
        decision, reason, confidence = "allow", None, 0.95

    output = {
        "op": "config_gate",
        "decision": decision,
        "configured": configured,
        "refusal_reason": reason,
        "store_url": store_url,
        "token_present": token_present,
        "api_version": cfg["api_version"],
        "base_url": cfg["base_url"],
    }
    if decision == "allow":
        rationale = (
            f"ALLOW: store '{store_url}' configured with an access token; "
            f"base URL {cfg['base_url']} can be built."
        )
    elif reason == "missing_store_url":
        rationale = "REFUSE: no store_url supplied (state or SHOPIFY_STORE_URL) — connector cannot target a store."
    else:
        rationale = (
            f"REFUSE: store '{store_url}' has no access token "
            f"(state or SHOPIFY_ACCESS_TOKEN) — Admin API calls would be unauthenticated."
        )
    return {
        "output": output,
        "rationale": rationale,
        "self_metric": {
            "confidence": round(confidence, 3),
            "configured": configured,
            "has_store": has_store,
            "token_present": token_present,
        },
    }


def _decide_normalize_products(state: Dict[str, Any], store_url: str) -> Dict[str, Any]:
    has_payload = "products" in state and isinstance(state.get("products"), list)
    products = normalize_products(state.get("products"), store_url)
    decision = "ok" if products else "empty"
    confidence = 0.95 if has_payload else 0.4
    output = {
        "op": "normalize_products",
        "decision": decision,
        "products": products,
        "product_count": len(products),
        "store_url": store_url,
    }
    rationale = (
        f"Projected {len(products)} product(s) onto the normalized "
        f"{{id,title,description,price,available,url}} shape."
        if products
        else "No products in the payload — returning an empty projection."
    )
    return {
        "output": output,
        "rationale": rationale,
        "self_metric": {
            "confidence": round(confidence, 3),
            "product_count": len(products),
            "has_payload": has_payload,
            "available_count": sum(1 for p in products if p["available"]),
        },
    }


def _decide_normalize_order(state: Dict[str, Any]) -> Dict[str, Any]:
    has_payload = "orders" in state and isinstance(state.get("orders"), list)
    order = normalize_order(state.get("orders"))
    found = order is not None
    decision = "found" if found else "not_found"
    confidence = 0.95 if has_payload else 0.4
    output = {
        "op": "normalize_order",
        "decision": decision,
        "order": order,
        "found": found,
    }
    rationale = (
        f"Order {order.get('number')!r} normalized (status={order.get('status')!r})."
        if found
        else "No matching order in the payload — returning null (the service returns None)."
    )
    return {
        "output": output,
        "rationale": rationale,
        "self_metric": {
            "confidence": round(confidence, 3),
            "found": found,
            "has_payload": has_payload,
        },
    }


def _decide_normalize_customer(state: Dict[str, Any]) -> Dict[str, Any]:
    has_payload = "customers" in state and isinstance(state.get("customers"), list)
    customer = normalize_customer(state.get("customers"))
    found = customer is not None
    decision = "found" if found else "not_found"
    confidence = 0.95 if has_payload else 0.4
    output = {
        "op": "normalize_customer",
        "decision": decision,
        "customer": customer,
        "found": found,
    }
    rationale = (
        f"Customer {customer.get('email')!r} normalized."
        if found
        else "No matching customer in the payload — returning null (the service returns None)."
    )
    return {
        "output": output,
        "rationale": rationale,
        "self_metric": {
            "confidence": round(confidence, 3),
            "found": found,
            "has_payload": has_payload,
        },
    }


def _decide_summarise(state: Dict[str, Any], store_url: str) -> Dict[str, Any]:
    has_payload = "products" in state and isinstance(state.get("products"), list)
    summary = summarise_products(state.get("products"), store_url)
    confidence = 0.95 if has_payload else 0.4
    output = {
        "op": "summarise",
        "decision": "ok",
        "summary": summary,
    }
    return {
        "output": output,
        "rationale": (
            f"Store summary for '{store_url}': {summary['product_count']} product(s), "
            f"top {len(summary['products'])} listed for context."
        ),
        "self_metric": {
            "confidence": round(confidence, 3),
            "product_count": summary["product_count"],
            "has_payload": has_payload,
        },
    }


def _decide_query(state: Dict[str, Any], store_url: str) -> Dict[str, Any]:
    has_payload = "products" in state and isinstance(state.get("products"), list)
    result = build_query_result(state.get("products"), store_url)
    decision = "ok" if result["row_count"] else "empty"
    confidence = 0.95 if has_payload else 0.4
    output = {
        "op": "query",
        "decision": decision,
        "result": result,
    }
    return {
        "output": output,
        "rationale": (
            f"Tabular projection: {result['row_count']} row(s) "
            f"over columns {result['columns']}."
        ),
        "self_metric": {
            "confidence": round(confidence, 3),
            "row_count": result["row_count"],
            "has_payload": has_payload,
        },
    }


def _unknown_op(op: str) -> Dict[str, Any]:
    return {
        "output": {
            "op": op,
            "decision": "refuse",
            "refusal_reason": "unknown_op",
            "known_ops": list(KNOWN_OPS),
        },
        "rationale": f"REFUSE: unknown op {op!r}. Known ops: {', '.join(KNOWN_OPS)}.",
        "self_metric": {"confidence": 0.0, "unknown_op": True},
    }


# ---------------------------------------------------------------------------
# Orchestrator entrypoint plumbing
# ---------------------------------------------------------------------------

def run_organ(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Run the organ on a ``{state, context}`` dict, returning the result dict."""
    if not isinstance(input_data, dict):
        input_data = {}
    return decide(input_data.get("state"), input_data.get("context"))


def _error_envelope(message: str) -> Dict[str, Any]:
    return {
        "output": {"op": "error", "decision": "refuse", "error": True},
        "rationale": message,
        "self_metric": {"confidence": 0.0},
    }


def main() -> None:
    """CLI entry point: read JSON from ORGAN_INPUT (path or literal) or stdin."""
    try:
        input_str = os.environ.get("ORGAN_INPUT")
        if input_str:
            if os.path.isfile(input_str):
                with open(input_str, "r") as fh:
                    input_str = fh.read()
        else:
            input_str = sys.stdin.read()

        input_data = json.loads(input_str)
        result = run_organ(input_data)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")

    except json.JSONDecodeError as exc:
        json.dump(_error_envelope(f"Invalid JSON input: {exc}"), sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)
    except Exception as exc:
        json.dump(_error_envelope(f"Error: {exc}"), sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

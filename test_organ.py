#!/usr/bin/env python3
"""Tests for the Shopify-connector organ."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import organ
from organ import (
    decide,
    run_organ,
    resolve_store_config,
    normalize_product,
    normalize_products,
    normalize_order,
    normalize_customer,
    summarise_products,
    build_query_result,
    KNOWN_OPS,
    QUERY_COLUMNS,
    DEFAULT_API_VERSION,
)

HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# config_gate
# ---------------------------------------------------------------------------

def test_config_gate_allows_when_store_and_token_present():
    r = decide({"op": "config_gate", "store_url": "mystore.myshopify.com", "access_token": "shpat_x"})
    out = r["output"]
    assert out["decision"] == "allow"
    assert out["configured"] is True
    assert out["refusal_reason"] is None
    assert out["base_url"] == "https://mystore.myshopify.com/admin/api/2024-01"
    assert out["token_present"] is True
    assert r["self_metric"]["confidence"] >= 0.9


def test_config_gate_default_op_is_config_gate():
    # No op key -> defaults to config_gate.
    r = decide({"store_url": "s.myshopify.com", "access_token": "t"})
    assert r["output"]["op"] == "config_gate"
    assert r["output"]["decision"] == "allow"


def test_config_gate_refuses_missing_store_url():
    r = decide({"op": "config_gate", "access_token": "shpat_x"})
    out = r["output"]
    assert out["decision"] == "refuse"
    assert out["configured"] is False
    assert out["refusal_reason"] == "missing_store_url"
    assert out["base_url"] == ""
    # Low confidence — decision rests on absent input.
    assert r["self_metric"]["confidence"] < 0.5


def test_config_gate_refuses_missing_token():
    r = decide({"op": "config_gate", "store_url": "mystore.myshopify.com"})
    out = r["output"]
    assert out["decision"] == "refuse"
    assert out["configured"] is False
    assert out["refusal_reason"] == "missing_access_token"
    # Confident refusal — store is present, token clearly absent.
    assert r["self_metric"]["confidence"] >= 0.9


def test_config_gate_uses_env_fallbacks_from_context():
    r = decide(
        {"op": "config_gate"},
        {"env_store_url": "envstore.myshopify.com", "env_access_token": "env-token"},
    )
    out = r["output"]
    assert out["decision"] == "allow"
    assert out["store_url"] == "envstore.myshopify.com"
    assert out["base_url"] == "https://envstore.myshopify.com/admin/api/2024-01"


def test_config_gate_state_overrides_env():
    r = decide(
        {"op": "config_gate", "store_url": "statestore.myshopify.com", "access_token": "t"},
        {"env_store_url": "envstore.myshopify.com"},
    )
    assert r["output"]["store_url"] == "statestore.myshopify.com"


def test_config_gate_custom_api_version():
    r = decide({"op": "config_gate", "store_url": "s.myshopify.com", "access_token": "t", "api_version": "2025-04"})
    assert r["output"]["api_version"] == "2025-04"
    assert r["output"]["base_url"] == "https://s.myshopify.com/admin/api/2025-04"


def test_resolve_store_config_default_api_version():
    cfg = resolve_store_config({"store_url": "s.myshopify.com"}, {})
    assert cfg["api_version"] == DEFAULT_API_VERSION
    assert cfg["base_url"] == "https://s.myshopify.com/admin/api/2024-01"


def test_token_presence_does_not_echo_token_value():
    r = decide({"op": "config_gate", "store_url": "s.myshopify.com", "access_token": "shpat_SECRET"})
    blob = json.dumps(r)
    assert "shpat_SECRET" not in blob
    assert r["output"]["token_present"] is True


# ---------------------------------------------------------------------------
# normalize_products
# ---------------------------------------------------------------------------

RAW_PRODUCT = {
    "id": 101,
    "title": "Widget",
    "body_html": "<p>desc</p>",
    "handle": "widget",
    "variants": [
        {"price": "9.99", "inventory_quantity": 5},
        {"price": "10.99", "inventory_quantity": 0},
    ],
}


def test_normalize_product_price_is_first_variant():
    p = normalize_product(RAW_PRODUCT, "mystore.myshopify.com")
    assert p["price"] == "9.99"
    assert p["title"] == "Widget"
    assert p["description"] == "<p>desc</p>"
    assert p["url"] == "https://mystore.myshopify.com/products/widget"


def test_normalize_product_available_true_when_any_variant_in_stock():
    p = normalize_product(RAW_PRODUCT, "s.myshopify.com")
    assert p["available"] is True


def test_normalize_product_available_false_when_all_out_of_stock():
    raw = {
        "id": 1, "title": "X", "handle": "x",
        "variants": [{"price": "1", "inventory_quantity": 0}],
    }
    p = normalize_product(raw, "s.myshopify.com")
    assert p["available"] is False


def test_normalize_product_no_variants_blank_price_unavailable():
    raw = {"id": 2, "title": "NoVar", "handle": "novar", "variants": []}
    p = normalize_product(raw, "s.myshopify.com")
    assert p["price"] == ""
    assert p["available"] is False


def test_normalize_product_missing_body_html_blank_description():
    raw = {"id": 3, "title": "Y", "handle": "y", "variants": [{"price": "2", "inventory_quantity": 1}]}
    p = normalize_product(raw, "s.myshopify.com")
    assert p["description"] == ""


def test_normalize_product_inventory_as_string_coerces():
    raw = {"id": 4, "title": "Z", "handle": "z", "variants": [{"price": "3", "inventory_quantity": "7"}]}
    p = normalize_product(raw, "s.myshopify.com")
    assert p["available"] is True


def test_decide_normalize_products_ok():
    r = decide({"op": "normalize_products", "store_url": "s.myshopify.com", "products": [RAW_PRODUCT]})
    out = r["output"]
    assert out["decision"] == "ok"
    assert out["product_count"] == 1
    assert out["products"][0]["title"] == "Widget"
    assert r["self_metric"]["available_count"] == 1
    assert r["self_metric"]["confidence"] >= 0.9


def test_decide_normalize_products_empty():
    r = decide({"op": "normalize_products", "store_url": "s.myshopify.com", "products": []})
    out = r["output"]
    assert out["decision"] == "empty"
    assert out["product_count"] == 0


def test_decide_normalize_products_missing_payload_low_confidence():
    r = decide({"op": "normalize_products", "store_url": "s.myshopify.com"})
    assert r["output"]["decision"] == "empty"
    assert r["self_metric"]["has_payload"] is False
    assert r["self_metric"]["confidence"] < 0.5


# ---------------------------------------------------------------------------
# normalize_order
# ---------------------------------------------------------------------------

RAW_ORDER = {
    "id": 555,
    "name": "#1001",
    "fulfillment_status": "fulfilled",
    "total_price": "19.98",
    "created_at": "2026-06-01T10:00:00Z",
    "line_items": [{"title": "Widget", "quantity": 2}],
    "fulfillments": [{"tracking_url": "https://track/abc"}],
}


def test_normalize_order_projects_first():
    o = normalize_order([RAW_ORDER])
    assert o["number"] == "#1001"
    assert o["status"] == "fulfilled"
    assert o["total"] == "19.98"
    assert o["items"] == [{"title": "Widget", "quantity": 2}]
    assert o["tracking"] == "https://track/abc"


def test_normalize_order_status_fallback_unfulfilled():
    raw = {"id": 1, "name": "#2", "fulfillment_status": None, "total_price": "1",
           "created_at": "x", "line_items": [], "fulfillments": []}
    o = normalize_order([raw])
    assert o["status"] == "unfulfilled"
    assert o["tracking"] is None


def test_normalize_order_empty_returns_none():
    assert normalize_order([]) is None


def test_decide_normalize_order_found():
    r = decide({"op": "normalize_order", "orders": [RAW_ORDER]})
    assert r["output"]["decision"] == "found"
    assert r["output"]["found"] is True
    assert r["output"]["order"]["number"] == "#1001"


def test_decide_normalize_order_not_found():
    r = decide({"op": "normalize_order", "orders": []})
    assert r["output"]["decision"] == "not_found"
    assert r["output"]["order"] is None
    assert r["output"]["found"] is False


# ---------------------------------------------------------------------------
# normalize_customer
# ---------------------------------------------------------------------------

RAW_CUSTOMER = {
    "id": 777,
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "orders_count": 3,
    "total_spent": "120.00",
}


def test_normalize_customer_builds_name():
    c = normalize_customer([RAW_CUSTOMER])
    assert c["name"] == "Ada Lovelace"
    assert c["email"] == "ada@example.com"
    assert c["orders_count"] == 3


def test_normalize_customer_partial_name_stripped():
    raw = {"id": 1, "first_name": "Solo", "last_name": "", "email": "s@x.com"}
    c = normalize_customer([raw])
    assert c["name"] == "Solo"
    # defaults applied
    assert c["orders_count"] == 0
    assert c["total_spent"] == "0"


def test_normalize_customer_empty_returns_none():
    assert normalize_customer([]) is None


def test_decide_normalize_customer_found():
    r = decide({"op": "normalize_customer", "customers": [RAW_CUSTOMER]})
    assert r["output"]["decision"] == "found"
    assert r["output"]["customer"]["name"] == "Ada Lovelace"


def test_decide_normalize_customer_not_found():
    r = decide({"op": "normalize_customer", "customers": []})
    assert r["output"]["decision"] == "not_found"
    assert r["output"]["customer"] is None


# ---------------------------------------------------------------------------
# summarise + query
# ---------------------------------------------------------------------------

def test_summarise_products_shape():
    s = summarise_products([RAW_PRODUCT], "mystore.myshopify.com")
    assert s["source"] == "Shopify store: mystore.myshopify.com"
    assert s["product_count"] == 1
    assert s["products"] == [{"title": "Widget", "price": "9.99"}]


def test_summarise_caps_at_ten():
    raw = [dict(RAW_PRODUCT, id=i, title=f"P{i}", handle=f"p{i}") for i in range(15)]
    s = summarise_products(raw, "s.myshopify.com")
    assert s["product_count"] == 15
    assert len(s["products"]) == 10


def test_decide_summarise():
    r = decide({"op": "summarise", "store_url": "s.myshopify.com", "products": [RAW_PRODUCT]})
    assert r["output"]["decision"] == "ok"
    assert r["output"]["summary"]["product_count"] == 1


def test_build_query_result_shape():
    q = build_query_result([RAW_PRODUCT], "s.myshopify.com")
    assert q["columns"] == QUERY_COLUMNS
    assert q["row_count"] == 1
    assert q["rows"][0] == [101, "Widget", "9.99", True]


def test_decide_query_ok():
    r = decide({"op": "query", "store_url": "s.myshopify.com", "products": [RAW_PRODUCT]})
    assert r["output"]["decision"] == "ok"
    assert r["output"]["result"]["row_count"] == 1


def test_decide_query_empty():
    r = decide({"op": "query", "store_url": "s.myshopify.com", "products": []})
    assert r["output"]["decision"] == "empty"
    assert r["output"]["result"]["row_count"] == 0


# ---------------------------------------------------------------------------
# robustness / contract
# ---------------------------------------------------------------------------

def test_unknown_op_refuses():
    r = decide({"op": "delete_everything"})
    assert r["output"]["decision"] == "refuse"
    assert r["output"]["refusal_reason"] == "unknown_op"
    assert r["self_metric"]["confidence"] == 0.0


def test_empty_state_is_config_gate_refuse():
    r = decide({})
    assert r["output"]["op"] == "config_gate"
    assert r["output"]["decision"] == "refuse"
    assert "confidence" in r["self_metric"]


def test_none_state_does_not_raise():
    r = decide(None)
    assert r["output"]["decision"] == "refuse"
    assert r["self_metric"]["confidence"] <= 1.0


def test_non_dict_state_does_not_raise():
    r = decide("not a dict")  # type: ignore[arg-type]
    assert r["output"]["decision"] == "refuse"


def test_run_organ_wraps_state_context():
    r = run_organ({"state": {"op": "config_gate", "store_url": "s.myshopify.com", "access_token": "t"}})
    assert r["output"]["decision"] == "allow"


def test_run_organ_non_dict_input():
    r = run_organ(None)  # type: ignore[arg-type]
    assert "output" in r and "self_metric" in r


def test_contract_shape_on_every_op():
    payloads = [
        {"op": "config_gate", "store_url": "s.myshopify.com", "access_token": "t"},
        {"op": "normalize_products", "products": [RAW_PRODUCT]},
        {"op": "normalize_order", "orders": [RAW_ORDER]},
        {"op": "normalize_customer", "customers": [RAW_CUSTOMER]},
        {"op": "summarise", "products": [RAW_PRODUCT]},
        {"op": "query", "products": [RAW_PRODUCT]},
    ]
    for s in payloads:
        r = decide(s)
        assert set(["output", "rationale", "self_metric"]).issubset(r.keys())
        assert isinstance(r["self_metric"], dict)
        c = r["self_metric"]["confidence"]
        assert isinstance(c, (int, float)) and 0.0 <= c <= 1.0


def test_all_known_ops_routed():
    for op in KNOWN_OPS:
        r = decide({"op": op})
        assert r["output"]["op"] == op


# ---------------------------------------------------------------------------
# CLI / subprocess (matches the conformance harness)
# ---------------------------------------------------------------------------

def _run_cli(input_obj):
    env = os.environ.copy()
    env["ORGAN_INPUT"] = json.dumps(input_obj)
    proc = subprocess.run(
        [sys.executable, str(HERE / "organ.py")],
        env=env, capture_output=True, text=True,
    )
    return proc


def test_cli_valid_input():
    proc = _run_cli({"state": {"op": "config_gate", "store_url": "s.myshopify.com", "access_token": "t"}})
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["output"]["decision"] == "allow"


def test_cli_invalid_json_exits_nonzero():
    env = os.environ.copy()
    env["ORGAN_INPUT"] = "{not json"
    proc = subprocess.run(
        [sys.executable, str(HERE / "organ.py")],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["self_metric"]["confidence"] == 0.0


@pytest.mark.parametrize("sample", sorted((HERE / "samples").glob("*.json")))
def test_samples_conform(sample):
    proc = _run_cli(json.loads(sample.read_text()))
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    for key in ("output", "rationale", "self_metric"):
        assert key in data
    assert 0.0 <= data["self_metric"]["confidence"] <= 1.0

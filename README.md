# organ-connectors-shopify

A **pure decider** organ extracted from discovery-engine
`lib/dataflow_core/connectors/shopify.py` (the `ShopifyConnector`). It answers:

> Given a store config and an **already-fetched** raw Shopify Admin API
> payload — is the connector ready to operate, and what is the *normalized*
> projection of that payload?

It reads `{state, context}` JSON on stdin (or the file named by `ORGAN_INPUT`)
and writes `{output, rationale, self_metric}` on stdout, per the orchestrator
[`CONTRACT.md`](https://github.com/Data-Flow-Advisory/orchestrator).

## Why this is an organ

`ShopifyConnector` braids two concerns:

1. **Side effects** — build a base URL, fire `requests.get` at
   `…/products.json`, `…/orders.json`, `…/customers/search.json`. These are
   **excluded** from the organ.
2. **Pure judgement** — *is the connector configured?* and *how does a raw
   Shopify product / order / customer payload map onto the connector's
   normalized shape?* (first-variant price, `available` = any variant in
   stock, status fallback to `unfulfilled`, `name` = `first last` stripped,
   …). This is what the organ computes.

The organ is **handed** the facts it needs — the store config and the raw JSON
arrays the caller already fetched — and computes the gate + projections. It
makes no network call, reads no env on its own behalf (env values arrive in
`context`), and **never raises** (fail-safe to `refuse`/empty).

## Operations (`state.op`)

| op | mirrors | decides |
|----|---------|---------|
| `config_gate` *(default)* | `__init__` / `connect` | configured? builds `base_url`; refuses on missing store_url / access_token |
| `normalize_products` | `get_products` | projects raw products → `{id,title,description,price,available,url}` |
| `normalize_order` | `get_order` | first order → `{id,number,status,total,created_at,items,tracking}`; `null` if empty |
| `normalize_customer` | `get_customer` | first customer → `{id,name,email,orders_count,total_spent}`; `null` if empty |
| `summarise` | `summarise` | `{source, product_count, products[:10]}` |
| `query` | `query` | tabular `{columns:[id,title,price,available], rows, row_count}` |

## Contract

**Input**

```json
{
  "state": {
    "op": "config_gate",
    "store_url": "mystore.myshopify.com",
    "access_token": "shpat_...",
    "products": [],
    "orders": [],
    "customers": [],
    "api_version": "2024-01"
  },
  "context": {
    "env_store_url": "...",
    "env_access_token": "...",
    "api_version": "2024-01"
  }
}
```

`store_url` / token presence resolve from `state` first, then the
`context.env_*` fallbacks (the connector's `SHOPIFY_STORE_URL` /
`SHOPIFY_ACCESS_TOKEN`). **The token value is never echoed** — only a
`token_present` boolean.

**Output**

```json
{
  "output": { "op": "config_gate", "decision": "allow", "configured": true,
              "base_url": "https://mystore.myshopify.com/admin/api/2024-01", "...": "..." },
  "rationale": "ALLOW: store '…' configured with an access token; …",
  "self_metric": { "confidence": 0.95, "configured": true }
}
```

Fail-safe: empty/malformed input fails to a low-confidence `refuse` (the gate)
or an empty projection (the data ops). `decide()` never raises.

## Run

```bash
echo '{"state":{"op":"config_gate","store_url":"s.myshopify.com","access_token":"t"}}' | python3 organ.py
ORGAN_INPUT=samples/normalize_products.json python3 organ.py
```

## Test

```bash
python -m pytest -q          # unit + CLI + sample conformance
python3 check_contract.py    # contract shape on every sample + empty state
```

CI (`.github/workflows/conformance.yml`) runs both on every push: the organ
must satisfy the contract on every sample and its tests must pass before the
always-fed train trusts it.

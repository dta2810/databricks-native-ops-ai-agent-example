# Beverage Ops Agent: synthetic RAG corpus

Synthetic documents for the fictitious company Acme Beverages. They feed the `search_documents` tool (Lakebase hybrid search: pgvector + BM25 + RRF). All names, SKUs, distributors and figures match `data/entities.py` and the generated tables (data as of the 2026-11-08 inventory snapshot). Each file has YAML front matter (`doc_id`, `title`, `doc_type`, `market`, `valid_from`, `valid_to`) that maps to the metadata filters.

## Documents

| doc_id | doc_type | market | Purpose (demo case) |
|---|---|---|---|
| PB-CA-PR-0422 | promo_brief | CA | Case 2: PR-0422 mechanic, +45% forecast, display deadlines per distributor |
| FR-CA-D1-2026-W45 | field_report | CA | Case 2: why CA-D1 is at −5% (display installed in 22% of doors) |
| FR-CA-D2-D3-2026-W45 | field_report | CA | Case 2 → 1 link: promo works at CA-D2/CA-D3, OOS risk, rebalance from CA-D1 |
| POL-REPL-2026 | policy | ALL | Case 1: cover targets, OOS threshold, min order, approval limits, lead times, credit-hold rule |
| DA-FL-D2-2026 | distributor_agreement | FL | Case 1: FL-D2 credit hold since 2026-10-28 → reject Harbor Lager top-up |
| DA-TX-D1-2026 | distributor_agreement | TX | Case 1: why TX Volt Original is low (can-line allocation), next slot |
| DA-CA-D1-2026 | distributor_agreement | CA | Case 2: execution compliance clauses 6.2–6.4, co-funding pro rata |
| PS-VT-ORIG-473C | product_spec | ALL | Case 1: Volt pack hierarchy, OOS impact, can-line constraint |
| PS-HB-LAGR-355B | product_spec | ALL | Case 1: Harbor glass pack, freshness rule, December seasonality |
| PS-FZ-COLA-600P | product_spec | ALL | Case 1: Fizzo pack range (NY Fizzo Cola 600ml) |
| PS-AP-STILL-500P | product_spec | ALL | Case 1: Aqua Pura range (CA Aqua Pura 500ml) |
| PS-SG-ORNG-1000T | product_spec | ALL | Case 2: Sunny Grove range, display kits SG-DSP-FLR-24 / SG-DSP-CTR-06 |
| MP-CA-2026 | market_playbook | CA | Case 2: in-flight review rule, rebalance on measured uplift |
| MP-FL-2026 | market_playbook | FL | Case 1: credit-hold handling, December Harbor build |

## Retrieval eval questions

| # | Question | Expected doc_id | Tests |
|---|---|---|---|
| 1 | Why is PR-0422 underperforming at Golden Coast Retail Partners? | FR-CA-D1-2026-W45 | semantic |
| 2 | What was the display installation deadline for CA-D1 in PR-0422? | PB-CA-PR-0422 | exact term (PR-0422) |
| 3 | Can we approve a top-up of HB-LAGR-355B for FL-D2? | DA-FL-D2-2026 (+ POL-REPL-2026) | exact terms + policy |
| 4 | Who needs to approve a replenishment request of 800 cases? | POL-REPL-2026 | semantic |
| 5 | Why is Volt Original stock low in Texas and when does supply recover? | DA-TX-D1-2026 | semantic |
| 6 | How many units per case and cases per pallet for VT-ORIG-473C? | PS-VT-ORIG-473C | exact term (SKU) |
| 7 | What does clause 6.4 of DA-CA-D1-2026 say about co-funding when display compliance is low? | DA-CA-D1-2026 | exact term (contract ID) |
| 8 | What is the target stock cover for a Traditional Trade distributor? | POL-REPL-2026 | semantic |
| 9 | How should we cover convenience-store demand for Harbor Lager while FL-D2 is on credit hold? | MP-FL-2026 | semantic, market filter FL |
| 10 | What is the capacity of the SG-DSP-FLR-24 display? | PS-SG-ORNG-1000T | exact term (POS kit code) |

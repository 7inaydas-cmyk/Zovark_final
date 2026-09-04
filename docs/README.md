> ### Archived snapshot, not the current codebase
>
> This repository is an April 2026 snapshot of the Zovark v1.5.1 prototype. It is kept
> because the decision history is worth reading. Nothing here has been maintained since.
>
> **The current work lives in two repositories:**
>
> | Repository | What it is |
> |---|---|
> | **[zovark-runtime](https://github.com/7inaydas-cmyk/zovark-runtime)** | The go-forward runtime. Eight slices shipped, 343 tests, an offline verifier that re-derives the verdict from evidence and fails closed, and a reproducible SHA-256 anchor published in the README that you can check yourself in five minutes. |
> | **[zovark-architecture](https://github.com/7inaydas-cmyk/zovark-architecture)** | The architecture authority. 27 decision records, a register of 39 invariants each marked covered, partial or deferred, and 510 tests. |
>
> Start with [zovark-runtime](https://github.com/7inaydas-cmyk/zovark-runtime) if you want
> to run something. Start with
> [zovark-architecture](https://github.com/7inaydas-cmyk/zovark-architecture) if you want to
> read why it is built the way it is.

---

# ZOVARK Documentation

**Version:** v1.5.1 | **Date:** 2026-03-24

## Current Technical Docs
| Document | Description |
|----------|-------------|
| [Architecture](ARCHITECTURE.md) | System architecture, V2 pipeline |
| [Deployment Guide](DEPLOYMENT_GUIDE.md) | Docker Compose + K8s setup |
| [Developer Reference](DEVELOPER_REFERENCE.md) | Coding conventions, API patterns |
| [Sandbox Security](SANDBOX_SECURITY.md) | 4-layer sandbox isolation model |
| [SIEM Integration](SIEM_INTEGRATION.md) | Splunk/Elastic webhook setup |
| [Model Tiers](MODEL_TIER_STRATEGY.md) | Fast/Standard/Reasoning model routing |
| [Whitepaper](WHITEPAPER.md) | Academic paper on air-gapped SOC investigation |

## Benchmarks
| Document | Description |
|----------|-------------|
| [Juice Shop Benchmark](JUICE_SHOP_BENCHMARK.md) | 100-alert real-traffic benchmark (99/100) |

## Operational
| Document | Description |
|----------|-------------|
| [Scaling](SCALING.md) | Horizontal scaling patterns |
| [Disaster Recovery](disaster-recovery.md) | DR procedures and failover |
| [Multi-Region](multi-region.md) | Multi-region K8s deployment |
| [Failure Modes](FAILURE_MODES.md) | Known failure modes and mitigations |

## API Reference
| Document | Description |
|----------|-------------|
| [OpenAPI Spec](openapi.yaml) | Full API specification (v1.2.0+) |
| [Webhooks](webhooks.md) | Webhook event catalog |
| [Pipeline Stages](pipeline_stages.md) | V2 pipeline stage contracts |

## Marketing (in /marketing/)
- CISO Brief, BlackHat CFP, Cold Email Templates, Enterprise Overview

## Archive (in docs/archive/)
- Historical audit reports, sprint reports, development session artifacts

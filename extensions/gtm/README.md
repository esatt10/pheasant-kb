# Pheasant GTM extension

GTM researches companies from public evidence and publishes curated profiles through the normal Pheasant sync pipeline. This directory contains the Python distribution, browser contribution, parser image and storage migrations.

From the repository root:

```sh
docker compose -f compose.yaml -f deploy/compose/docker-compose.gtm.yml up -d --build
```

For a Python checkout, install with `python -m pip install -e ./extensions/gtm`. Use `PHEASANT_BUILD_EXTENSIONS=gtm` when building the UI. See the [host protocol](../../docs/extensions.md) and the [GTM runbook](../../docs/how-to/gtm-research.md).

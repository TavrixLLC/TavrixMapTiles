# TavrixMap Tiles Gateway Integration Contract

TavrixMap Tiles is an internal map engine. It does not own public customers, public API keys, authorization, rate limits, billing, usage storage, pricing, invoices, or rollups. Those responsibilities belong to the Tavrix Gateway.

## Production Roles

- Gateway is the public entry point at `https://api.tavrix.com/maps`.
- Tiles `map-api` is internal only, for example `http://tavrixmaptiles-map-api:8090` on the private Docker/server network.
- Nginx/static hosting serves PMTiles, glyph PBFs, sprites, and static manifests at `https://tiles.tavrix.com`.
- Tiles returns style, manifest, metadata, health, and internal diagnostics.
- Tiles does not count, store, price, or roll up customer usage.

## Endpoint Classification

Public through Gateway:

```text
GET /maps/style.json
GET /maps/style/{region}.json
GET /maps/manifest.json
GET /maps/manifest/{region}.json
GET /maps/regions
GET /maps/styles
```

Internal only:

```text
GET /api/health/dependencies
GET /api/metrics
GET /api/health/metrics
POST /api/cache/clear
POST /api/cache/warm
GET /api/tiles/inspect/{region}/{z}/{x}/{y}
GET /api/vector/...
GET /api/raster/...
```

Static:

```text
https://tiles.tavrix.com/tiles/...
https://tiles.tavrix.com/fonts/...
https://tiles.tavrix.com/sprites/...
https://tiles.tavrix.com/manifests/...
```

## Gateway Responsibilities

The Gateway owns:

- public API token validation
- customer identity
- authorization
- rate limits and abuse controls
- public request logging
- usage events
- map-load counting
- plans, pricing, invoices, and rollups

The Gateway may choose to count a successful authenticated style response as one `map_load`, typically:

```text
GET /maps/style.json
GET /maps/style/{region}.json
```

That is a Gateway policy. Tiles does not validate or persist this usage.

## Tiles Responsibilities

Tiles owns:

- reading active local manifests
- returning MapLibre styles with public static PMTiles URLs
- returning manifests, regions, styles, and metadata
- serving internal diagnostics only to trusted internal callers
- enforcing that public direct vector/raster APIs remain disabled in production unless explicitly overridden
- exposing secret-free internal metrics for operations

Tiles must not:

- store raw public API tokens
- define customer billing tables
- calculate invoices or customer charges
- emit customer usage events
- make static PMTiles range requests billable

## Static Request Monitoring

PMTiles range requests are served by Nginx/static hosting, not by Tiles `map-api`. Static logs may be analyzed by infrastructure, Gateway, or CDN tooling for:

- bandwidth planning
- fair-use review
- abuse investigation
- cache and range-request debugging
- infrastructure cost estimation

Static logs are not customer billing state in this repo.

## Internal Authentication

When the Gateway calls internal Tiles diagnostics, it must use either `MAP_INTERNAL_TOKEN` or an explicitly configured trusted header/value pair:

```text
X-Internal-Token: <MAP_INTERNAL_TOKEN>
Authorization: Bearer <MAP_INTERNAL_TOKEN>
X-Tavrix-Gateway-Token: <trusted gateway secret>
```

Trusted headers are safe only on the private server/Docker network. Do not accept trusted internal headers from public clients.

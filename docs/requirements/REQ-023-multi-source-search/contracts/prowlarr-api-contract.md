# Prowlarr API Contract Evidence

Status: offline test contract only. This document does not claim that Prowlarr
is connected or that the Watch Assistant adapter is production-ready.

## Verified public source

The wire contract is pinned to the official Prowlarr repository commit
[`c6034c86f0d6b5e7a04b7dd944c9e1067808b026`](https://github.com/Prowlarr/Prowlarr/tree/c6034c86f0d6b5e7a04b7dd944c9e1067808b026):

- [OpenAPI document](https://github.com/Prowlarr/Prowlarr/blob/c6034c86f0d6b5e7a04b7dd944c9e1067808b026/src/Prowlarr.Api.V1/openapi.json)
- [Search controller](https://github.com/Prowlarr/Prowlarr/blob/c6034c86f0d6b5e7a04b7dd944c9e1067808b026/src/Prowlarr.Api.V1/Search/SearchController.cs)
- [Search resource](https://github.com/Prowlarr/Prowlarr/blob/c6034c86f0d6b5e7a04b7dd944c9e1067808b026/src/Prowlarr.Api.V1/Search/SearchResource.cs)
- [Release resource](https://github.com/Prowlarr/Prowlarr/blob/c6034c86f0d6b5e7a04b7dd944c9e1067808b026/src/Prowlarr.Api.V1/Search/ReleaseResource.cs)
- [API-key authentication handler](https://github.com/Prowlarr/Prowlarr/blob/c6034c86f0d6b5e7a04b7dd944c9e1067808b026/src/Prowlarr.Http/Authentication/ApiKeyAuthenticationHandler.cs)

The official OpenAPI document declares the default server as
`http://localhost:9696`, a global `X-Api-Key` header scheme and an `apikey`
query scheme. The Watch Assistant adapter contract deliberately selects the
header scheme only, so a credential cannot enter a URL, query log, or cache
key. The local mock creates an ephemeral per-test value and never records the
value.

## Frozen wire shape

`GET /api/v1/search` accepts these query parameters:

- `query` and `type` as strings;
- `indexerIds` and `categories` as integer arrays;
- `limit` and `offset` as 32-bit integers.

The successful response is a bare JSON array of `ReleaseResource` objects. It
does not contain a cursor or a total field. `ReleaseResource.protocol` is the
official enum `unknown`, `usenet`, or `torrent`; the Watch Assistant unified
model maps `usenet` to the user-facing NZB kind and keeps it separate from a
torrent. `infoHash` is nullable and is a string in the official schema.

Prowlarr does not define Watch Assistant's infohash normalization, duplicate
merge, source observation, timeout fallback, pagination loop, or Chinese error
catalog. Those are local policies covered by the strict pending tests in
`tests/contracts/test_prowlarr_contract.py` and must be reviewed when the
adapter is connected.

## Pending Watch Assistant seam

The pending tests use an explicit, injectable target seam so they cannot
silently bind to a real service:

- `watch_assistant.adapters.prowlarr.ProwlarrClient(base_url, api_key,
  timeout_seconds, transport)` with an async `search` method using the frozen
  query names;
- `watch_assistant.services.search_aggregation.aggregate_sources(sources)`
  returning an object whose async `search` result exposes `results`,
  `warnings`, and `error_code`;
- normalized candidates expose `kind` (`torrent` or `nzb`), lowercase
  `infohash` when present, `source_id`, and duplicate `observations`.

This is a Watch Assistant test seam, not an assertion about an undocumented
Prowlarr endpoint. The main implementation may use an equivalent adapter
interface, but it must update this contract test before removing the strict
pending marker.

## Local evidence

`tests/contract_support/prowlarr_mock.py` uses `httpx.MockTransport`; it never
opens a listener or connects to Prowlarr. The synthetic fixtures contain no
credentials or real direct links.
The pending adapter constructor includes an injected transport so those tests
remain offline after the product module is connected.

The currently available tests prove the mock's route, header-only request
recording, repeated search arrays, protocol values, bare-array pagination
boundary, and Chinese error descriptors. Adapter and multi-source assertions
are strict `xfail` until the target modules exist; an unexpected pass is a
test failure so the marker cannot silently hide a connected implementation.

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
official enum `unknown`, `usenet`, or `torrent`; the Watch Assistant adapter
keeps `usenet` outside the supported magnet resource and push path. `infoHash`
is nullable and is a string in the official schema.

Prowlarr does not define Watch Assistant's infohash normalization, duplicate
merge, source observation, timeout fallback, pagination loop, or Chinese error
catalog. Those are local policies covered by the passing offline contract tests
in `tests/contracts/test_prowlarr_contract.py`.
An all-zero BTIH is rejected as an unsupported identity before it can enter the
common resource model.

## Watch Assistant seam

The contract tests use the product adapter and search service directly with an
explicit injected HTTP transport, so they cannot silently bind to a real
service:

- `watch_assistant.adapters.prowlarr.ProwlarrClient(base_url, api_key,
  timeout, client)` uses the frozen query names and returns
  `ProwlarrSearchResult`;
- The Prowlarr and PanSou adapters force `follow_redirects=False` on upstream
  requests, including injected HTTP clients, so a read-only call cannot follow
  a redirect to another target.
- `watch_assistant.services.search.SearchService._query_sources` preserves a
  healthy PanSou result when Prowlarr fails and reports a partial warning;
- `SearchService` normalization exposes the common magnet canonical key and
  preserves source observations in metadata. NZB/usenet releases remain
  unsupported and never enter the magnet push path.
- Source degradation emits the registered Chinese `search.source_degraded`
  event with only the source, status, and query counts; terminal search
  failures use an allowlisted Chinese `error_code` and never render exception
  text.

This is a Watch Assistant test seam, not an assertion about an undocumented
Prowlarr endpoint.

## Local evidence

`tests/contract_support/prowlarr_mock.py` uses `httpx.MockTransport`; it never
opens a listener or connects to Prowlarr. The synthetic fixtures contain no
credentials or real direct links.
The adapter receives an injected `httpx.AsyncClient` backed by that transport,
so the product calls remain offline.

The currently available tests prove the mock's route, header-only request
recording, repeated search arrays, protocol values, bare-array pagination
boundary, Chinese error descriptors, adapter query construction, NZB filtering,
bounded pagination, cross-source deduplication, and partial-source fallback.

## Offline readiness additions

The adapter now classifies only the failure classes supported by the frozen
wire contract and `httpx` transport: `408` and transport timeouts become
`prowlarr_timeout`, `429` becomes `prowlarr_rate_limited` (with a bounded
numeric `Retry-After` hint), `401`/`403` become `prowlarr_auth_required`, and
`5xx` becomes `prowlarr_server_error`.  The upstream response body is never
copied into an exception, log event, or API response.

Each configured Prowlarr client shares an in-memory source tracker with the
settings service.  Its safe state sequence is `unverified` -> `available`, or
`degraded`/`backoff` after a classified failure; three consecutive failures
open a bounded local circuit.  After the retry window, exactly one probe is
allowed.  The source-status endpoint exposes only the state, safe reason code,
Chinese reason text, counters, and timestamps.  It does not expose the API
key, request header, query text, response body, or retry header value.

Aggregation continues to execute PanSou and Prowlarr independently.  A
Prowlarr timeout, rate limit, server failure, or open circuit leaves successful
PanSou candidates available and marks the result partial.  Magnet candidates
are deduplicated only by normalized BTIH; 115 share identities remain distinct
unless their verified share identity is equal.  Canonical candidates retain
safe `source_observations` containing only normalized source IDs and capture
times.  Prowlarr query text is intentionally not copied into normalized
metadata; the existing PanSou query metadata behavior is unchanged.

This remains offline readiness evidence.  No real Prowlarr host, API key,
indexer configuration, or indexer write operation has been used or verified.

## Follow-up review evidence

The readiness seam also protects the local health state from abandoned probes:
argument validation happens before a circuit probe is reserved, and cancelled
half-open requests release their reservation so a later probe can run. Public
adapter errors suppress the underlying HTTPX exception chain; request query
text and authentication headers therefore do not appear in an emitted error
traceback. Health responses retain only bounded retry timing, stable reason
codes, and Chinese explanations for timeout, rate-limit, authentication,
response-shape, and server failures.

The frontend API type seam now carries the existing source-health response,
including state, reason, and retry fields, without adding any credential or
request-detail field. The settings page remains outside this change; this is a
typed backend dependency for a later status presentation update.

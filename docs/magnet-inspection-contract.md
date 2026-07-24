# Magnet inspection contract

Status: frozen design contract for a later API integration. This change does not
add routes, persistence, configuration, or deployment.

## Adapter boundary

`QbittorrentClient.inspect(magnets)` accepts at most 30 magnet URIs. Valid BTIH
hashes are normalized to lowercase 40-character hexadecimal values and
deduplicated before qBittorrent is called. Invalid magnets produce an
`unsupported` result with `invalid_magnet`.

The adapter logs neither request bodies nor remote exception text. Results do
not echo magnets or trackers. `largest_video_name` is a basename, not a torrent
path.

Each call creates a random `wa-inspect-<uuid>` marker and submits it as both the
tag and category with `stopCondition=MetadataReceived`. After authentication,
the adapter calls `GET /api/v2/app/version` and accepts only parseable versions
at or above `v4.5.0`; an unavailable, malformed, or older version is rejected
as `unsupported/incompatible_qbittorrent` before `torrents/add` is called.
The concurrency semaphore belongs to the client instance, so simultaneous
`inspect()` calls share one global limit of four by default. An instance-level
hash mutex serializes exists/add/poll/cleanup for the same infohash. Its
protected reference count removes the lock entry after the final waiter exits.
Each item has a 60-second metadata deadline. The adapter checks for an existing
hash before adding it. Cleanup queries or deletes only the exact hash carrying
the current call's tag. A pre-existing torrent is returned as
`unsupported/existing_torrent` and is never deleted. If an inspect task is
cancelled after add was attempted, cleanup runs under the hash mutex through a
shielded task and the cancellation is re-raised only after cleanup completes.

Only `pausedDL` and `stoppedDL` are accepted as metadata-complete states and
cause `torrents/files` to be read. `metaDL` continues polling. Any other state,
including `downloading`, `forcedDL`, `error`, `missingFiles`, and `unknown`, is
cleaned immediately and returned as `failed/metadata_stop_failed`.

## Proposed HTTP API

### Start a batch

`POST /api/v1/resources/inspect`

```json
{
  "resource_ids": ["res_abcd", "res_efgh"]
}
```

`resource_ids` is required and contains 1 to 30 unique strings. The API resolves
resource IDs to magnets internally and preserves request order;
clients must not submit magnets. Authorization and resource ownership checks
reuse the future resource API policy and occur before the batch is queued.

Accepted response (`202`):

```json
{
  "batch_id": "01K...",
  "status": "queued",
  "submitted_count": 2,
  "completed_count": 0,
  "results": []
}
```

Validation failures return the application's normal `422` envelope. A request
containing an inaccessible or unknown resource ID is rejected as a whole; it is
not partially queued.

### Read a batch

`GET /api/v1/resources/inspect/{batch_id}`

```json
{
  "batch_id": "01K...",
  "status": "partial",
  "submitted_count": 2,
  "completed_count": 2,
  "results": [
    {
      "resource_id": "res_abcd",
      "infohash": "0123456789abcdef0123456789abcdef01234567",
      "status": "verified",
      "total_size_bytes": 7516192768,
      "file_count": 6,
      "video_file_count": 2,
      "video_size_bytes": 7500000000,
      "subtitle_count": 2,
      "sample_count": 1,
      "largest_video_name": "Movie.2026.2160p.mkv",
      "content_summary": "2 video(s), 2 subtitle(s), 1 suspicious file(s)",
      "error_code": null
    }
  ]
}
```

In each result, `resource_id` is a `string` using the `res_...` namespace and
`infohash` is `string | null`; it is `null` when no normalized BTIH hash was
available (for example, an invalid magnet).

Batch statuses are mutually exclusive and are selected in this priority order
once all item work has reached a terminal state:

- `queued`: accepted but no worker has claimed the batch.
- `running`: at least one item is in progress.
- `failed`: a batch-level dependency failed before item processing could start;
  this takes priority over every item-derived status. Otherwise, it is used
  when no item is `verified` or `unsupported`.
- `completed`: every item is `verified` or `unsupported`.
- `partial`: at least one item is `verified` or `unsupported` and at least one
  item is `timeout` or `failed`.

Item statuses are `verified`, `timeout`, `failed`, and `unsupported`. Every
non-`verified` item has a stable `error_code`; user-facing code must not expose
exception messages or tracebacks. Frozen adapter error codes are:

- `invalid_magnet`
- `authentication_failed`
- `login_unavailable`
- `existing_torrent`
- `incompatible_qbittorrent`
- `metadata_stop_unsupported`
- `metadata_stop_failed`
- `ownership_conflict`
- `metadata_timeout`
- `api_unavailable`
- `malformed_response`
- `add_failed`
- `cleanup_failed`
- `internal_error`

Unknown future error codes must be handled as a generic inspection failure.

## Content field definitions

- `total_size_bytes`: sum of all file sizes reported by `torrents/files`.
- `file_count`: number of rows reported by `torrents/files`.
- `video_file_count` and `video_size_bytes`: all files ending in `mkv`, `mp4`,
  `avi`, `mov`, `m2ts`, or `ts`, including suspicious extras.
- `subtitle_count`: files ending in `srt`, `ass`, `ssa`, `sub`, or `vtt`.
- `sample_count`: files whose path components or basename contain the token
  `sample`, `trailer`, or `proof`, plus executable or installer extensions such
  as `exe`, `bat`, `cmd`, `com`, `msi`, and `scr`. This is the frozen
  suspicious-content count; the field name is retained for API brevity.
- `largest_video_name`: basename of the largest non-suspicious video, falling
  back to the largest video if every video is suspicious.
- `content_summary`: deterministic, non-localized summary derived from the
  numeric fields. Clients should display the numeric fields when localization
  is needed.

## Sidecar deployment recommendation

Run a dedicated, supported qBittorrent instance for inspection rather than
sharing a user's download client. Give it a separate profile, credentials,
state directory, incomplete directory, network identity, and disk quota. Do
not publish its Web UI port publicly; expose it only to the backend network and
apply an egress policy that permits BitTorrent metadata discovery.

Before enabling traffic, verify in staging that the deployed qBittorrent Web
API honors `stopCondition=MetadataReceived` and preserves the supplied tag.
Keep payload storage ephemeral and small, monitor `cleanup_failed`, and alert on
orphaned `wa-inspect-*` tags. An operator may reap old tagged inspection tasks,
but that policy is deliberately outside this adapter. Never point the adapter
at a qBittorrent profile containing pre-existing user torrents.

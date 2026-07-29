import asyncio

import pytest

from watch_assistant.adapters.p115_playback_contract import (
    DynamicLinkOutcome,
    FakeP115PlaybackGateway,
    PlaybackContractError,
    PlaybackGate,
    PlaybackStatus,
    classify_playback_exception,
    evaluate_playback_gate,
    forwarding_policy,
    make_playback_request,
    parse_byte_range,
)

MANIFEST_ID = "strm_7qXh5Mptv9K2dR4a"
SECRET_URL = "https://cdn.example.invalid/video?token=token-like-value"
COOKIE = "UID=secret-cookie"
PICKCODE = "pickcode-secret"


def _enabled_gate() -> PlaybackGate:
    return PlaybackGate(enabled=True, contract_verified=True)


def test_playback_is_explicitly_disabled_and_unverified_by_default():
    decision = evaluate_playback_gate(PlaybackGate())
    assert decision.allowed is False
    assert decision.error_code == "playback_disabled"
    assert (
        evaluate_playback_gate(PlaybackGate(enabled=True)).error_code
        == "contract_unverified"
    )


def test_only_head_get_and_one_valid_byte_range_are_accepted():
    assert parse_byte_range("bytes=0-499").start == 0
    assert parse_byte_range("bytes=500-").end is None
    assert parse_byte_range("bytes=-500").start is None
    for invalid in ("items=0-1", "bytes=1-0", "bytes=0-1,2-3", "bytes=", 1):
        with pytest.raises(PlaybackContractError, match="invalid_range"):
            parse_byte_range(invalid)  # type: ignore[arg-type]
    with pytest.raises(PlaybackContractError, match="invalid_method"):
        make_playback_request(MANIFEST_ID, "POST")


def test_head_never_forwards_range_and_get_forwards_a_valid_range():
    head = make_playback_request(MANIFEST_ID, "HEAD", "bytes=0-99")
    get = make_playback_request(MANIFEST_ID, "GET", "bytes=0-99")
    assert forwarding_policy(head).forward_range is False
    assert forwarding_policy(get).forward_range is True


@pytest.mark.asyncio
async def test_fake_requires_a_managed_opaque_id_and_keeps_dynamic_link_transient():
    gateway = FakeP115PlaybackGateway(
        {MANIFEST_ID: DynamicLinkOutcome(PlaybackStatus.READY, url=SECRET_URL)}
    )
    disabled = await gateway.resolve(
        make_playback_request(MANIFEST_ID, "GET"), gate=PlaybackGate()
    )
    assert disabled.status is PlaybackStatus.DISABLED
    assert gateway.calls == []

    missing = await gateway.resolve(
        make_playback_request("strm_xYz9AbCdEfGhJkLm", "GET"), gate=_enabled_gate()
    )
    assert (missing.status, missing.error_code) == (
        PlaybackStatus.NOT_FOUND,
        "manifest_out_of_scope",
    )
    with pytest.raises(PlaybackContractError, match="invalid_manifest_id"):
        make_playback_request(PICKCODE, "GET")

    outcome = await gateway.resolve(
        make_playback_request(MANIFEST_ID, "GET", "bytes=100-"), gate=_enabled_gate()
    )
    assert outcome.status is PlaybackStatus.READY
    assert outcome.url == SECRET_URL
    assert outcome.policy is not None and outcome.policy.forward_range is True


@pytest.mark.asyncio
async def test_timeout_is_uncertain_and_cancellation_propagates_without_leaking():
    gateway = FakeP115PlaybackGateway(
        {MANIFEST_ID: TimeoutError(f"{SECRET_URL} {COOKIE}")}
    )
    timeout = await gateway.resolve(
        make_playback_request(MANIFEST_ID, "GET"), gate=_enabled_gate()
    )
    assert (timeout.status, timeout.error_code) == (PlaybackStatus.UNCERTAIN, "timeout")

    cancelled = FakeP115PlaybackGateway({MANIFEST_ID: asyncio.CancelledError(PICKCODE)})
    with pytest.raises(asyncio.CancelledError):
        await cancelled.resolve(
            make_playback_request(MANIFEST_ID, "GET"), gate=_enabled_gate()
        )


@pytest.mark.asyncio
async def test_diagnostic_dtos_errors_and_fake_calls_redact_url_cookie_pickcode_and_tokens():
    request = make_playback_request(MANIFEST_ID, "GET", "bytes=0-99")
    outcome = DynamicLinkOutcome(PlaybackStatus.READY, url=SECRET_URL)
    gateway = FakeP115PlaybackGateway({MANIFEST_ID: outcome})
    await gateway.resolve(request, gate=_enabled_gate())
    failed = classify_playback_exception(
        RuntimeError(f"{SECRET_URL} {COOKIE} {PICKCODE}")
    )
    rendered = (
        repr(request)
        + repr(outcome)
        + repr(gateway)
        + repr(gateway.calls)
        + repr(failed)
    )
    for secret in (MANIFEST_ID, SECRET_URL, COOKIE, PICKCODE, "token-like-value"):
        assert secret not in rendered
    assert failed.error_code == "remote_failed"
    with pytest.raises(PlaybackContractError, match="invalid_error_code"):
        DynamicLinkOutcome(PlaybackStatus.FAILED, SECRET_URL)

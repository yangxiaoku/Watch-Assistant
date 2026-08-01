from watch_assistant.schemas import (
    ProwlarrSettingsResponse,
    ProwlarrVerifyResponse,
    SearchSourcesResponse,
)


def test_prowlarr_public_responses_do_not_expose_api_key():
    assert "api_key" not in ProwlarrSettingsResponse.model_fields
    assert "api_key" not in ProwlarrVerifyResponse.model_fields
    assert set(SearchSourcesResponse.model_fields) == {"pansou", "prowlarr"}


def test_prowlarr_settings_response_has_stable_source_and_revision_fields():
    fields = ProwlarrSettingsResponse.model_fields
    assert {
        "source",
        "enabled",
        "configured",
        "base_url",
        "api_key_configured",
        "api_key_source",
        "last_updated_at",
        "revision",
    } <= set(fields)

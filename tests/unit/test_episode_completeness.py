from watch_assistant.services.episode_completeness import (
    EpisodeBaseline,
    EpisodeFileReference,
    EpisodeMatrixStatus,
    build_episode_matrix,
)


def test_matrix_distinguishes_owned_multiple_unaired_special_and_unrecognized():
    matrix = build_episode_matrix(
        (
            EpisodeBaseline(1, aired=True),
            EpisodeBaseline(2, aired=False),
            EpisodeBaseline(3, aired=True),
            EpisodeBaseline(4, aired=True, special=True),
        ),
        (
            EpisodeFileReference("file-a", (1, 2)),
            EpisodeFileReference("file-b", (1,)),
            EpisodeFileReference("file-special", (), special=True),
            EpisodeFileReference("file-unknown", (), recognized=False),
        ),
        inventory_complete=True,
    )

    assert [item.status for item in matrix.items] == [
        EpisodeMatrixStatus.MULTIPLE,
        EpisodeMatrixStatus.UNAIRED,
        EpisodeMatrixStatus.MISSING,
        EpisodeMatrixStatus.SPECIAL,
    ]
    assert matrix.items[0].file_ids == ("file-a", "file-b")
    assert matrix.missing_episodes == (3,)
    assert matrix.duplicate_episodes == (1,)
    assert matrix.unrecognized_file_ids == ("file-special", "file-unknown")
    assert matrix.conclusion_available is True


def test_one_multi_episode_file_is_not_a_duplicate():
    matrix = build_episode_matrix(
        (EpisodeBaseline(1, aired=True), EpisodeBaseline(2, aired=True)),
        (EpisodeFileReference("season-pack", (1, 2)),),
        inventory_complete=True,
    )
    assert [item.status for item in matrix.items] == [
        EpisodeMatrixStatus.OWNED,
        EpisodeMatrixStatus.OWNED,
    ]


def test_incomplete_inventory_never_concludes_missing():
    matrix = build_episode_matrix(
        (EpisodeBaseline(1, aired=True), EpisodeBaseline(2, aired=True)),
        (),
        inventory_complete=False,
    )
    assert all(item.status == EpisodeMatrixStatus.UNKNOWN for item in matrix.items)
    assert matrix.missing_episodes == ()
    assert matrix.conclusion_available is False


def test_duplicate_baseline_is_rejected():
    try:
        build_episode_matrix(
            (EpisodeBaseline(1, aired=True), EpisodeBaseline(1, aired=True)),
            (),
            inventory_complete=True,
        )
    except ValueError as error:
        assert str(error) == "duplicate baseline episode"
    else:
        raise AssertionError("duplicate baseline should be rejected")

import json

from scripts.p115_strm_playback_live_runner import main, run_acceptance


def test_playback_runner_is_closed_without_live_gate(tmp_path, capsys):
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"parent_ids": ["7"]}), encoding="utf-8")

    assert (
        run_acceptance(
            root_id="7",
            file_id="8",
            cookie_path=tmp_path / "cookie",
            managed_scope_path=scope,
            environment={},
        )
        == {"status": "blocked", "error_code": "live_gate_closed", "write_started": False}
    )
    assert capsys.readouterr().out == ""


def test_playback_runner_cli_requires_explicit_live_flag(tmp_path, capsys):
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"parent_ids": ["7"]}), encoding="utf-8")

    assert (
        main(
            [
                "--root-id",
                "7",
                "--file-id",
                "8",
                "--cookie-path",
                str(tmp_path / "cookie"),
                "--managed-scope-path",
                str(scope),
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == "live_gate_closed"

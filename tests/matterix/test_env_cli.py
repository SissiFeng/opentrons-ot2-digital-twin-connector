"""CLI tests for ot2-matterix-env (import-safe, no Isaac Lab)."""

from __future__ import annotations

import json
from pathlib import Path

from unitelabs.opentrons_ot2.matterix.env_cli import main


def test_env_cli_check_reports_not_ready(capsys) -> None:
    code = main(["check", "--json"])
    assert code == 0  # non-strict: informational exit
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False
    assert any(item["name"] == "Matterix OT-2 gym task registration" for item in report["checks"])


def test_env_cli_check_strict_exits_nonzero(capsys) -> None:
    code = main(["check", "--strict", "--json"])
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False


def test_env_cli_generate_writes_scaffold(tmp_path: Path, capsys) -> None:
    target = tmp_path / "matterix_ot2_task"
    code = main(["generate", str(target)])
    assert code == 0
    env_module = target / "matterix_ot2_env.py"
    assert env_module.is_file()
    assert "Matterix-OT2-LiquidHandler-v1" in env_module.read_text(encoding="utf-8")
    env_source = env_module.read_text(encoding="utf-8")
    assert "asset.validate_joint_alignment()" in env_source
    assert "apply_ot2_joint_alignment(" in env_source
    assert (target / "README.md").is_file()


def test_env_cli_generate_requires_force_to_overwrite(tmp_path: Path, capsys) -> None:
    target = tmp_path / "matterix_ot2_task"
    target.mkdir()
    (target / "README.md").write_text("keep", encoding="utf-8")
    code = main(["generate", str(target)])
    assert code == 0
    assert (target / "README.md").read_text(encoding="utf-8") == "keep"
    code = main(["generate", str(target), "--force"])
    assert code == 0
    assert (target / "README.md").read_text(encoding="utf-8") != "keep"


def test_env_cli_generate_writes_actions_module(tmp_path: Path, capsys) -> None:
    target = tmp_path / "matterix_ot2_task"
    code = main(["generate", str(target)])
    assert code == 0
    actions = (target / "matterix_ot2_actions.py").read_text(encoding="utf-8")
    assert "def build_ot2_action_cfg(action)" in actions
    assert "alignment=asset.joint_alignment" in actions
    assert "tip_rack_bindings=asset.tip_rack_bindings" in actions
    assert "MatterixMoveToJointConfig" in actions
    assert "MatterixSemanticAction" in actions
    assert "MatterixWait" in actions
    env_source = (target / "matterix_ot2_env.py").read_text(encoding="utf-8")
    assert "asset.validate_reference_frame_alignment()" in env_source
    assert "asset.validate_tip_rack_bindings(connector)" in env_source
    assert "make_ot2_tip_rack_with_tips_cfg" in env_source
    assert '"pick_and_return_tip": _pick_and_return_tip_workflow()' in env_source
    assert "ReturnTipCfg" in env_source


def test_env_cli_generated_modules_are_syntactically_valid(tmp_path: Path, capsys) -> None:
    import ast

    target = tmp_path / "matterix_ot2_task"
    assert main(["generate", str(target)]) == 0
    for name in ("matterix_ot2_env.py", "matterix_ot2_actions.py", "__init__.py"):
        ast.parse((target / name).read_text(encoding="utf-8"), filename=name)

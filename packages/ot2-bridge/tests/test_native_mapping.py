import sys
from types import SimpleNamespace

import pytest

from ot2_bridge import MatterixAdapter, Operation, native_liquid_builders


def test_native_liquid_uses_upstream_composition_without_reimplementing_wait(monkeypatch):
    calls = []

    def cfg(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setitem(
        sys.modules,
        "matterix_sm.compositional_actions.ot2_liquid_handling",
        SimpleNamespace(AspirateOT2Cfg=cfg, DispenseOT2Cfg=cfg),
    )
    builders = native_liquid_builders("robot")
    params = {"mount": "LEFT", "channels": 1, "volume_ul": 20, "flow_rate_ul_s": 10}
    op = Operation("r", "s", "ot2", "aspirate", params)
    configs = builders["aspirate"](op)
    assert len(configs) == 1
    assert calls == [{"asset_name": "robot", "volume": 20, "flow_rate": 10}]
    for changes in ({"channels": 8}, {"mount": "RIGHT"}, {"volume_ul": -1}, {"well": "A1"}):
        with pytest.raises(ValueError):
            builders["aspirate"](Operation("r", "s", "ot2", "aspirate", params | changes))
    assert len(calls) == 1


@pytest.mark.parametrize("configs", [[], [None], [[object()]]])
def test_matterix_rejects_empty_or_nested_sequences(configs):
    adapter = MatterixAdapter("ot2", {"home": lambda op: configs}, lambda *args: None)
    with pytest.raises(ValueError):
        adapter.prepare(Operation("r", "s", "ot2", "home", {}))

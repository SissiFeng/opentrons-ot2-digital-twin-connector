"""Bounded client schema from the physical connector captured on 2026-09-30."""

from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

pytest.importorskip("lxml")

from ot2_bridge.flex_sila import FlexSiLATransport
from ot2_bridge.ot2_sila import OT2SiLATransport


XML = Path(__file__).with_name("fixtures").joinpath("ot2-motion-20260930.xml").read_text()
NS = "{http://www.sila-standard.org}"
REQUIRED = ("Home", "EmergencyStop", "GetPosition", "HomedFlags", "IsSimulating")


def test_home_client_preserves_live_wire_contract_and_errors():
    original = ET.fromstring(XML)
    selected = ET.fromstring(OT2SiLATransport()._client_feature_definition("MotionControlFeature", XML))
    assert selected.attrib == original.attrib
    for node in original:
        identifier = node.findtext(NS + "Identifier")
        matches = [item for item in selected if item.tag == node.tag and item.findtext(NS + "Identifier") == identifier]
        if node.tag in {NS + "Command", NS + "Property"} and identifier not in REQUIRED:
            assert matches == []
        else:
            assert len(matches) == 1
            assert ET.tostring(matches[0]) == ET.tostring(node)
    assert FlexSiLATransport()._client_feature_definition("MotionController", XML) == XML


@pytest.mark.parametrize("endpoint", REQUIRED)
@pytest.mark.parametrize("change", ["missing", "duplicate"])
def test_incomplete_or_ambiguous_live_contract_is_rejected(endpoint, change):
    root = ET.fromstring(XML)
    node = next(item for item in root if item.findtext(NS + "Identifier") == endpoint)
    if change == "missing":
        root.remove(node)
    else:
        root.append(deepcopy(node))
    with pytest.raises(ValueError, match=f"exactly one {endpoint}"):
        OT2SiLATransport()._client_feature_definition("MotionControlFeature", ET.tostring(root, encoding="unicode"))


@pytest.mark.parametrize("xml", ["<!DOCTYPE Feature>" + XML, '<Feature xmlns="other"/>'])
def test_invalid_xml_contract_is_rejected(xml):
    with pytest.raises(ValueError):
        OT2SiLATransport()._client_feature_definition("MotionControlFeature", xml)

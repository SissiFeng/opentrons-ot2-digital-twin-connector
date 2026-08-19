# OT-2 Matterix Integration and Acceptance Boundary

This document defines what the connector implements and what still requires
evidence from the real OT-2 and the exact Isaac Sim composite. It deliberately
does not treat the standalone Franka asset qualification as native OT-2 proof.

## Executable chain

The intended integration is:

```text
connector deck target (millimetres)
  -> accepted world/base/deck transform
  -> verified X/Y/Z/A joint alignment
  -> safe vertical, XY, contact vertical
  -> explicit well-to-nested-child selection
  -> PR47 IsTipAttached attach/detach transition
  -> physics settling in the PR7 rack collider
```

Every arrow is fail-closed. Source inspection, a plausible identity transform,
or a successful Franka test cannot change a gate to `VERIFIED`.

## P1: joint alignment

The connector maps the four physical articulation joints only:

| Connector axis | Matterix joint |
|---|---|
| X | `PrismaticJointMiddleBar` |
| Y | `PrismaticJointPipetteHolder` |
| Z | `PrismaticJointLeftPipette` |
| A | `PrismaticJointRightPipette` |

B/C are pipette-plunger semantics; they are not articulation joints in the
current OT-2 USD. A candidate becomes executable only after the real and
simulation evidence both contain reference, signed probe, and endpoint results
for X/Y/Z/A and the combined profile validates the exact equation and ranges.

The active config is never overwritten by the evidence tool:

```sh
uv run ot2-joint-alignment build-candidate \
  --hardware-evidence <hardware.json> \
  --simulation-evidence <simulation.json> \
  --alignment-id <reviewed-id> \
  --output <candidate-config.json>
```

## P1/P2: nested rigid and individual tip addressing

The source-inspected PR7 layout contains the rack and 96 tip transforms:

- `tiprack_mesh` — used by the separately spawned static rack;
- `pipette_tip_mesh_00` through `pipette_tip_mesh_95` — 96 independently
  addressable tips.

The connector config stores every A1–H12 mapping explicitly. The current map is
column-major (`A1=00`, `B1=01`, ..., `H12=95`) based on the inspected USD. It
remains `UNVERIFIED` until the runtime manifest and visible rack orientation
confirm that well ordering.

Current PR47 (`79efd967`) filters the layout to the 96 tip identifiers and
spawns each from `pipette_tip_inst.usda`; the empty rack is a separate static
scene object and is not part of the nested manifest. A runtime manifest artifact
has this strict shape:

```json
{
  "schema_version": "1.0",
  "asset_name": "tips",
  "child_ids": ["pipette_tip_mesh_00", "pipette_tip_mesh_01"],
  "evidence": "Matterix run ID and exact PR47/PR7 revisions"
}
```

The real artifact must list all 96 tip child IDs. Order is irrelevant; duplicates,
missing IDs, unexpected IDs, wrong asset names, wrong well geometry, mount
mismatch, and multi-channel mismatch are rejected. A canonical hash includes
the asset name and sorted complete child list.

```sh
uv run ot2-tip-rack-binding build-candidate \
  --connector-config <reviewed-connector.json> \
  --manifest <runtime-manifest.json> \
  --labware-id tips_300 \
  --binding-id <reviewed-id> \
  --output <candidate-config.json>
```

PR47 currently exposes one selected nested child through `IsTipAttached` and
`Ot2TipAttachment`. Therefore this implementation supports a single-channel
pipette route and rejects an eight-channel connector profile. Multi-channel
pickup needs a PR47-compatible group-of-children attachment contract before it
can be represented honestly.

## P3: world/base/deck alignment

The connector uses physical deck millimetres; Matterix uses world metres. The
stored chain is:

```text
world_from_deck = world_from_base @ base_from_deck
```

Each transform is fitted from at least three non-collinear paired fiducials.
The fit rejects degenerate observations, non-finite values, reflections, and
RMS error above the operator-supplied tolerance. Derivation creates reviewable
artifacts; joining them creates a separate full candidate config.

```sh
uv run ot2-frame-alignment derive --input <world-base.json> --output <world-base-fit.json>
uv run ot2-frame-alignment derive --input <base-deck.json> --output <base-deck-fit.json>
uv run ot2-frame-alignment build-candidate \
  --world-from-base <world-base-fit.json> \
  --base-from-deck <base-deck-fit.json> \
  --alignment-id <reviewed-id> \
  --output <candidate-config.json>
```

## Native pickup and return

The generated `pick_and_return_tip` workflow uses the OT-2 pipette, not a
Franka arm or a gripper:

1. raise the active mount to the configured safe travel height;
2. translate X/Y above the selected well;
3. descend to the connector-resolved pickup pose;
4. attach the exact nested child from the accepted well map;
5. retract to safe height;
6. repeat the safe route to the same well;
7. detach the same child and let physics settle it in the rack;
8. retract again.

`ReconcileTip(present=true)` is rejected because it cannot identify a physical
nested child. `DropTip` may detach the currently selected child at the configured
trash target. No action teleports a released tip.

## Final acceptance order

1. Pin exact Matterix, PR47, PR7, connector, OT-2 USD, and LFS revisions.
2. Collect and review X/Y/Z/A real and simulation evidence.
3. Survey and review world/base/deck fiducials.
4. Enumerate the exact 96-tip runtime manifest and visually confirm A1–H12
   orientation.
5. Ensure the connector pipette mount/channel count matches the PR47 attachment
   route.
6. Run `ot2-matterix-preflight --strict` and `ot2-matterix-env check --strict`.
7. Run one visible native OT-2 pickup/return episode, then repeat the accepted
   workflow for the required cycle count and retain JSON/log/video evidence.

The code is ready for these evidence steps; the checked configuration is not
hardware authorization.

# OT-2 motion definition regression

`ot2-motion-20260930.xml` is a reduced feature definition obtained through the
physical connector's read-only `SiLAService.GetFeatureDefinition` on 2026-09-30.
It retains the home/readback adapter's five endpoints, all execution errors,
and the unused `MoveThrough` command that reproduces the sila2 0.14 codegen
failure (`MoveThrough_Parameters.Waypoints_Struct` is not defined).
No endpoint definitions or parameter/response types were rewritten. Other
commands and properties were removed to keep the regression focused.

The wire test substitutes this deployed definition during discovery against an
explicit local connector simulator. It verifies that the selected schema still
decodes real SiLA messages. It does not command physical hardware.

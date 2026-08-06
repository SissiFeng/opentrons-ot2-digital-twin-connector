# Changelog

All notable changes to the independent
`sissifeng-opentrons-ot2-dt-connector` package are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the package uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-05

### Added

- Independent Git and package identity for Sissi Feng's OT-2 connector.
- Hash-pinned SiLA FDL/configuration contract and startup drift gate.
- Versioned device, deck, robot-state, motion, pipette, tip, and
  liquid-handling features.
- Durable evidence-qualified state with `SOFTWARE_TRACKED` and `UNRECONCILED`
  tip semantics.
- Connector-owned atomic tip pickup and release under the hardware lock.
- External `robot.*` workflow parser, contract-derived gRPC transport,
  fail-closed preflight, and append-only side-effect audit.
- Matterix OT-2 semantic action planner, workflow-level StateMachine boundary,
  asset/config pins, shadow comparison, and import-safe readiness CLI.
- Local real-gRPC simulator test covering an audited tip-transfer lifecycle.

### Changed

- Package name changed to `sissifeng-opentrons-ot2-dt-connector`.
- Liquid-operation inputs remain strictly positive while tracked response
  volume permits a valid `0 µL` result after dispense.
- Duplicate same-type module discovery now fails closed.
- Hardware GPIO imports are deferred in explicit simulator mode without
  introducing fallback-to-simulation behavior.

### Safety

- The checked example calibration remains unconfirmed and cannot authorize
  physical motion.
- Real Matterix OT-2 execution remains gated on a verified USD asset, OT-2
  compositional-action extension, Linux Isaac Lab environment, and physical
  calibration.

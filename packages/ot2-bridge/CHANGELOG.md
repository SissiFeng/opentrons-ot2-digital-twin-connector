# Changelog

## 0.5.1 — 2026-09-30

- Compile only the live OT-2 Home, EmergencyStop, GetPosition, HomedFlags and IsSimulating endpoints consumed by this adapter. The deployed connector's unrelated MoveThrough definition triggered a sila2 0.14 codegen error and blocked discovery. Preserve selected endpoint definitions and reject missing or duplicate endpoints.
- Reproduce the deployed feature-definition failure in the local SiLA wire regression. Read-only discovery and state retrieval also pass against the physical OT-2; no motion was issued during this verification.
- Bind the Mac SSH forward to its observed wired interface (`en8`) to avoid a link-local route through Wi-Fi, and document browser authentication and in-place updates.
- Isolate the bridge installer/backend Python from inherited Isaac PYTHONPATH; keep the Matterix process in its existing Isaac environment.

## 0.5.0 — 2026-09-30

- Add a Mac-browser / Ubuntu-backend deployment over the existing Tailscale network, with password-protected remote console access and an explicit Mac SiLA tunnel endpoint.
- Save each reviewed plan and Matterix launch bundle on the Ubuntu backend, so browser downloads do not need to be transferred back.
- Bind the OT-2 home/readback profile to left P10 GEN1 and right P300 GEN2 eight-channel nominal assets and independent source/asset revisions.
- Keep this OT-2 task free of tip and liquid semantics. Nominal asset geometry does not qualify tool alignment, collisions or liquid handling.
- Provide isolated Ubuntu setup commands and pin compatible SiLA/gRPC runtime and code-generator versions.
- Preserve the existing standalone mapping, Flex, mirror and predictive runner APIs.

Local CPU/transport tests and nominal USD checks pass. Native Isaac/PhysX and the physical paired run require field verification.

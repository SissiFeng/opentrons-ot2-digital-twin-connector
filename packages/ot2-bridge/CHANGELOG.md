# Changelog

## 0.5.0 — 2026-09-30

- Add a Mac-browser / Ubuntu-backend deployment over the existing Tailscale network, with password-protected remote console access and an explicit Mac SiLA tunnel endpoint.
- Save each reviewed plan and Matterix launch bundle on the Ubuntu backend, so browser downloads do not need to be transferred back.
- Bind the OT-2 home/readback profile to left P10 GEN1 and right P300 GEN2 eight-channel nominal assets and independent source/asset revisions.
- Keep this OT-2 task free of tip and liquid semantics. Nominal asset geometry does not qualify tool alignment, collisions or liquid handling.
- Provide isolated Ubuntu setup commands and pin compatible SiLA/gRPC runtime and code-generator versions.
- Preserve the existing standalone mapping, Flex, mirror and predictive runner APIs.

Local CPU/transport tests and nominal USD checks pass. Native Isaac/PhysX and the physical paired run require field verification.

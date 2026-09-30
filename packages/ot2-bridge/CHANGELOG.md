# Changelog

## 0.6.0 — 2026-09-30

- Add an outbound authenticated device gateway, configured device catalog and replaceable local backend provider. Browser clients no longer enter device connection details in managed application mode.
- Launch the exact reviewed native Matterix run automatically in the configured Isaac environment. Surface startup, binding errors, native logs and child exit; stop owned child processes on completion/shutdown.
- Display native RGB frames with run ID, capture time and live/last-frame labels. Missing or failed native imagery stays unavailable; no schematic fallback.
- Preserve one-shot reviews, durable device-side attempts, ordered operations, independent Stop, and held state on lost results. Cleanup failures retain collected evidence.
- Provide one-time Mac gateway and Ubuntu site configuration commands and a browser acceptance guide. Runtime paths may be overridden per device for distinct pinned OT-2 and Flex checkouts.
- Local CPU, HTTP and browser acceptance uses explicit fixtures. Native Ubuntu/GPU imagery and real device execution still require field acceptance.

## 0.5.2 — 2026-09-30

- Keep all console SiLA requests and paired runs on one persistent asyncio loop. Repeated HTTP checks previously created and closed separate loops, leaving gRPC completion callbacks targeting closed loops. Shut down pending transports before closing the console loop; cancellation still does not confirm a physical stop.
- Show checking, success and failure feedback immediately below Check connector, disable duplicate checks while pending, and include the backend's target address and troubleshooting guidance in connection timeouts.
- Document the required Ubuntu-to-Mac TCP 15051 Tailscale grant. Tailscale ping and Mac-to-Ubuntu browser access alone do not verify this reverse connection. The field test found no matching inbound allow rule for this endpoint; an administrator must authorize the access.

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

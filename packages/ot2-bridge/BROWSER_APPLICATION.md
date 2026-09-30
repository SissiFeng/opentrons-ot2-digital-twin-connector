# Browser-operated Matterix and instrument application

## Product requirement

An operator uses an authenticated browser to select an instrument and workflow,
preview it, run Matterix on the Ubuntu GPU workstation, see the native Matterix
view and execution observations, and then explicitly run the device or select
paired execution. The browser computer has no instrument-control role.

This is the target application design accepted during the 2026-09-30 field test.
It is not a claim that the current 0.5.2 console implements the managed runtime,
viewer or outbound gateway described below.

```mermaid
flowchart LR
    Browser["Operator browser"]
    App["Run application · Ubuntu\nWorkflow review, run lifecycle, observations"]
    Matterix["Managed Matterix runtime · Ubuntu GPU\nNative simulation and viewport"]
    Gateway["Device gateway · instrument-side computer\nBackend adapter and local connection"]
    Backend["Instrument connector / orchestrator"]
    Robot["OT-2 / Flex / another instrument"]
    Browser -->|"Select, preview, simulate, confirm, run"| App
    App -->|"Native view, progress, results"| Browser
    App <-->|"Launch + operation / observation mapping"| Matterix
    Gateway -->|"Initiates authenticated outbound session"| App
    App -.->|"Commands and results use that session"| Gateway
    Gateway <-->|"SiLA, HTTP or another backend adapter"| Backend
    Backend <--> Robot
```

## Operator flow

1. Open the application and select a registered instrument and supported workflow.
   The application displays device-gateway availability and Matterix availability.
   Connector addresses and Python paths are site setup, not per-operator inputs.
2. Preview the planned operations. Label this preview as planned, with the selected
   instrument model, labware and mapping scope.
3. Select **Simulate**. The application starts the configured native Matterix
   process on Ubuntu, loads the reviewed plan and waits for its actual readiness
   handshake. Show startup progress and actionable startup errors in the page.
4. Display the actual Matterix viewport or camera stream alongside operation
   progress and source-labelled observations. A schematic or animation is not a
   substitute for this native simulation view. A missing stream stays unavailable.
5. After reviewing the rehearsal, select **Run device**, or review a new paired
   run and select **Run both**. Bind the workflow/profile hash, the selected device
   identity and the operator's authorization to that execution. A rehearsal and a
   physical execution have distinct run IDs linked to the same reviewed workflow.
6. Show real and simulated progress and observations separately. Paired mode uses
   operation boundaries, not a promise of matching wall-clock or physical state.

Simulation completion does not authorize real motion automatically and is not
proof of physical success. Report observations the connector actually provides;
do not infer attachment, dropped tools or collisions from agreement with a model.

## Responsibility boundaries

| Component | Owns | Does not own |
|---|---|---|
| Browser | Operator interaction and presentation | Hardware connectivity or persistent execution |
| Run application | Reviewed runs, authorization, device reservation, runtime startup, reports and session routing | Physical truth or Matterix's native state |
| Thin bridge core | Operation/observation mapping, correlation, units/frames and evidence labels | Scheduling, retry, recovery, process supervision or video streaming |
| Matterix runtime | Simulation execution, native state and native visual output | Verification of unobserved real events |
| Device gateway | Authenticated outbound session and locally configured backend adapter | Workflow decisions or automatic retries of uncertain actions |
| Connector / orchestrator | Its device-control or workflow authority and actual supported observations | Browser-specific configuration |

The existing Operation, Outcome, Observation and adapter contracts remain the
integration boundary. An application may use SiLA adapters now and another
orchestrator's adapter later. Network sessions, process supervision and the viewer
belong outside the thin core.

## Deployment and connection direction

During the current test, the Mac is physically wired to the OT-2, so it is the
temporary device gateway. Its role is independent of whichever computer opens
the browser. A lab computer or suitable robot-side process can later provide the
gateway without changing the operator workflow.

The gateway initiates its session to the application endpoint and receives work
over that established session. No inbound instrument-control listener on each
operator's computer is required. The endpoint still needs the site's approved
network reachability and authentication; an outbound session is not permission
to bypass network policy.

One-time site configuration supplies the native Isaac environment, source/assets
paths, device identities and adapters. It is reasonable to install/start these
services once; a user should not copy a shell command for every simulation run.

## Execution and failure contracts

- Register gateways with distinct device credentials and known capabilities.
  Browser credentials do not serve as gateway credentials.
- Accept only reviewed operations for the registered device/profile. Never accept
  arbitrary Python, shell commands, connector URLs or module imports from the UI.
- A delivered hardware operation is never automatically replayed after disconnect,
  gateway restart or an uncertain result. Result delivery can be deduplicated.
- Disconnect during execution produces an unknown/held state. Reconnection alone
  does not make a device ready; reconcile its reported state explicitly.
- Keep progress/control independent of video backpressure. Report stale frames
  with timestamps and retain backend error details without leaking credentials.
- Preserve the current OT-2 stop limitation: its connector can queue Stop behind
  Home. A browser stop request is not an immediate physical emergency stop.

## Current implementation and acceptance gap

Version 0.5.2 provides the authenticated remote console, native-plan export,
thin OT-2/Flex mappings, independent observations, paired-step execution and a
persistent console asyncio loop. Native Matterix startup is still manual, and
the direct SiLA path still needs backend-to-connector network access.

The following must be implemented and verified before calling this a complete
browser-operated application:

- A registered outbound device gateway and corresponding backend adapter path.
- An Ubuntu Matterix process supervisor driven from the reviewed browser run.
- Native Matterix visual output in the browser with source and freshness labels.
- Instrument selection without per-user host/port or terminal configuration.
- End-to-end browser acceptance for simulate, device-only and paired execution,
  using the actual Ubuntu GPU runtime and actual instrument separately from
  synthetic transport/UI fixtures.

The first physical acceptance scope remains OT-2 Home and position readback, with
left P10 GEN1 and right P300 GEN2 eight-channel nominal visual assets. Tool
calibration, tip/liquid semantics and qualified collision checks remain separate
work.

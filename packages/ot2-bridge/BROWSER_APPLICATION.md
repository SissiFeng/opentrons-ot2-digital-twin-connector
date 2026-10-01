# Browser-operated Matterix and instrument application

## Product requirement

An operator uses an authenticated browser to select an instrument and workflow,
preview it, run Matterix on the Ubuntu GPU workstation, see the native Matterix
view and execution observations, and then explicitly run the device or select
paired execution. The browser computer has no instrument-control role.

Version 0.7.0 implements the managed runtime, native frame feed and outbound
gateway described below. See [the executable OT-2 test guide](src/ot2_bridge/guides/browser-ot2-test.md).
CPU/HTTP/browser fixture acceptance is separate from native GPU and physical acceptance.

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
| Device gateway | Authenticated outbound session, locally configured adapter and durable attempt records | Workflow decisions or automatic retries of uncertain actions |
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
  does not make a device ready. The Hub persists that hold across restarts; use a
  fresh connector check and explicitly record operator reconciliation before a new run.
- Keep progress/control independent of video backpressure. Report stale frames
  with timestamps and retain backend error details without leaking credentials.
- Preserve the current OT-2 stop limitation: its connector can queue Stop behind
  Home. A browser stop request is not an immediate physical emergency stop.

## Implementation and field acceptance

Version 0.7.0 adds explicit deployment origins, same-host gateway routing,
installation credential rotation, durable execution records and operator reconciliation
to the managed gateway/native runtime introduced in 0.6.0. Browser restoration
reattaches to a job; Hub restart holds unfinished work and never resumes it. Direct legacy CLI/console operation remains
available when `--site-config` is omitted.

Gateway requests are delivered at most once by the mailbox and recorded before
local dispatch. Lost response => held/unknown, never inferred success. Physical
side attempt files prevent replay of a run ID. A local provider factory is the
extension point for another orchestrator; the hub only sees operations and
observations. One application currently reserves one run at a time.

CPU tests cover all three browser API paths through an outbound HTTP agent,
concurrent Stop, no replay, wrong identity/profile, missing responses, process
readiness/exit/timeout and native RGB encoding. Browser fixture acceptance checks
visible controls and results. These tests do not establish native Isaac/GPU or
physical performance. The first field acceptance remains native OT-2 Home and
position readback, followed by the independent physical run, then paired execution.

Nominal pipette geometry does not qualify tool alignment, collision checks or
liquid handling. Source state remains with each backend.

## Deployment and persistence contract (0.7.0)

See [network topology](NETWORK_TOPOLOGY.md) and the packaged
[deployment guide](src/ot2_bridge/guides/deployment.md) for supported commands.
Use personal user identities for browser computers and temporary personal gateways;
use persistent tagged identities for dedicated lab services. Group grants must be
reviewed alongside existing policy, including administrative access.

The Hub journal commits intent before gateway dispatch. A single Hub owns an
output directory; its SQLite database, run files and reconciliation notes must
be retained together. Gateways additionally retain local command and run attempt
ledgers. Moving a gateway requires stopping the previous controller, inspecting
unfinished work, rotating its installation credential and rechecking identity.
Revocation fences future API work; it cannot cancel a delivered physical command.
Local locks do not exclude controllers on other hosts.

An execution keeps its full plan/profile fingerprints for its native handshake.
Cross-run linkage uses `workflow_sha256`, `qualified_profile_sha256` (excluding
only gateway-local host/port) and an explicit `rehearsal_run_id`. A gateway can
change its local tunnel endpoint without changing the qualified device/model.
Site option `require_rehearsal: true` requires a matching successful sim run for
real/paired execution. It defaults to false for independent commissioning tests.
Neither setting automatically authorizes motion or proves physical safety.

Simulation-only holds require confirmation that the native process is idle;
physical holds also require a fresh identity check and a stopped previous gateway.
An operator note clears the hold for a new reviewed run, without changing old
outcomes or claiming automatic recovery. Hub crashes may leave a native child or
physical command active; inspect both before acknowledging reconciliation.

Real-data-driven model revisions and general training dataset export remain
future work. Current pinned model qualification limits continue to apply.

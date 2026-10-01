"""Runnable, explicitly mock-only concurrent real/sim adapter example."""

import asyncio

from ot2_bridge import CallableAdapter, Evidence, Fact, Mirror, Observation, Operation, Outcome, Status


async def main():
    started = {"real": asyncio.Event(), "sim": asyncio.Event()}

    def backend(side, evidence):
        def prepare(operation):
            if operation.action != "aspirate":
                raise ValueError("Mock supports aspirate only")

            async def execute():
                started[side].set()
                await started["sim" if side == "real" else "real"].wait()
                return Outcome(
                    Status.SUCCEEDED,
                    Observation(
                        f"mock-{side}", {"LEFT.volume": Fact(operation.parameters["volume_ul"], evidence, "uL")}
                    ),
                )

            return execute

        return CallableAdapter(prepare)

    mirror = Mirror(backend("sim", Evidence.SIMULATED), sim_timeout=2, tolerances={"LEFT.volume": 0.5})
    operation = Operation(
        "demo",
        "aspirate-1",
        "mock-ot2",
        "aspirate",
        {
            "mount": "LEFT",
            "channels": 1,
            "volume_ul": 10,
            "flow_rate_ul_s": 5,
        },
    )
    result = await mirror.run(operation, backend("real", Evidence.TRACKED))
    result.real.outcome.require_success()
    print("MOCK ONLY: both adapters reached the concurrent-start barrier")
    print(f"real={result.real.outcome.status.value}, sim={result.sim.outcome.status.value}")
    print(f"volume comparison={result.comparisons[0].status}")


if __name__ == "__main__":
    asyncio.run(main())

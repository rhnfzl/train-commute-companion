import asyncio

from runtime_rig import Provider, Rig

from train_commute_companion.vehicle import (
    PlatformFeature,
    SeatCapacity,
    StockUnit,
    TrainContext,
    VehiclePosition,
    VehiclePositions,
)


class ContextProvider(Provider):
    def __init__(self, clock, world):
        super().__init__(clock, list(world))
        self.optional_calls = []
        self.positions_entered = asyncio.Event()
        self.positions_gate = None
        self.context_error = None

    async def stations(self):
        return frozenset({"AAA", "UT", "RTD", "BBB"})

    async def vehicle_positions(self):
        self.optional_calls.append(("positions",))
        self.positions_entered.set()
        if self.positions_gate is not None:
            await self.positions_gate.wait()
        numbers = {leg.train.train_number for connection in self.world for leg in connection.legs}
        return VehiclePositions(
            self.clock.now(),
            tuple(
                VehiclePosition(number, number, 52.1, 5.1, "OBIS", "IC", None, None, None)
                for number in sorted(numbers)
            ),
        )

    async def train_context(self, train, station):
        self.optional_calls.append(("context", train, station))
        if self.context_error is not None:
            raise self.context_error
        return TrainContext(
            train,
            station,
            self.clock.now(),
            "DVS",
            "VIRM",
            "NS",
            "5",
            False,
            6,
            161,
            (StockUnit("1", "VIRM", ("WIFI",), SeatCapacity(10, 20, None, None, 2)),),
            (PlatformFeature("LIFT", "Lift listed"),),
            None,
        )


def make_rig(path):
    rig = Rig(path)
    rig.provider = ContextProvider(rig.clock, rig.provider.world)
    rig.rt = rig.new()
    return rig

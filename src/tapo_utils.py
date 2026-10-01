from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import asdict, dataclass
from typing import Optional

from tapo import ApiClient


USERNAME = os.getenv("TAPO_USERNAME", "juhyeun38@gmail.com")
PASSWORD = os.getenv("TAPO_PASSWORD", "ju369631")

# Tapo P110M plugs that power the experiment lights.
LIGHTS = {
    "light_1": "10.155.207.35",
    "light_2": "10.155.207.10",
}

LIGHT_MODEL_API = "p100"
DEFAULT_TIMEOUT_S = 8
LIGHT_CONDITIONS = {
    0: {
        "light_1": False,
        "light_2": False,
    },
    1: {
        "light_1": True,
        "light_2": False,
    },
    2: {
        "light_1": False,
        "light_2": True,
    },
    3: {
        "light_1": True,
        "light_2": True,
    },
}


@dataclass(frozen=True)
class LightState:
    name: str
    host: str
    requested_on: bool
    device_on: bool
    nickname: str
    model: str


class TapoLightController:
    def __init__(
        self,
        username: str = USERNAME,
        password: str = PASSWORD,
        lights: Optional[dict[str, str]] = None,
        model_api: str = LIGHT_MODEL_API,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ):
        self.username = username
        self.password = password
        self.lights = lights or LIGHTS
        self.model_api = model_api
        self.client = ApiClient(username, password, timeout_s=timeout_s)

    async def apply_condition(self, condition: int) -> dict[str, dict[str, object]]:
        desired_states = LIGHT_CONDITIONS[validate_light_condition(condition)]
        results: dict[str, dict[str, object]] = {}

        for name, requested_on in desired_states.items():
            results[name] = asdict(await self.set_light(name, requested_on))

        return results

    async def set_light(self, name: str, on: bool) -> LightState:
        if name not in self.lights:
            raise KeyError(f"Unknown Tapo light: {name}")

        host = self.lights[name]
        handler_factory = getattr(self.client, self.model_api)
        handler = await handler_factory(host)

        if on:
            await handler.on()
        else:
            await handler.off()

        await asyncio.sleep(0.4)
        info = await handler.get_device_info()
        return LightState(
            name=name,
            host=host,
            requested_on=bool(on),
            device_on=bool(info.device_on),
            nickname=str(info.nickname),
            model=str(info.model),
        )


async def apply_light_condition_async(condition: int) -> dict[str, dict[str, object]]:
    return await TapoLightController().apply_condition(condition)


def apply_light_condition(condition: int) -> dict[str, dict[str, object]]:
    return asyncio.run(apply_light_condition_async(condition))


def validate_light_condition(value: int) -> int:
    condition = int(value)
    if condition not in LIGHT_CONDITIONS:
        valid = ", ".join(str(item) for item in sorted(LIGHT_CONDITIONS))
        raise ValueError(f"light condition must be one of: {valid}.")
    return condition


def parse_light_condition(value: str) -> int:
    try:
        return validate_light_condition(int(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args():
    parser = argparse.ArgumentParser(description="Set Tapo light condition.")
    parser.add_argument(
        "--light-condition",
        "--light",
        dest="light_condition",
        type=parse_light_condition,
        required=True,
        help=(
            "Light condition bitmask: 0 both off, 1 light_1 on, "
            "2 light_2 on, 3 both on."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    states = apply_light_condition(args.light_condition)
    for name, state in states.items():
        print(f"{name}: requested_on={state['requested_on']} device_on={state['device_on']}")


if __name__ == "__main__":
    main()

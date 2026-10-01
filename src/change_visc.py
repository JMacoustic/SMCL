from __future__ import annotations

import argparse
import sys

from tapo_utils import apply_light_condition
from xarm_utils import (
    DEFAULT_ACCELERATION,
    DEFAULT_SPEED,
    DEFAULT_XARM_IP,
    XArmCameraController,
    print_pose,
)


DEFAULT_LIFT_Z_MM = 150.0


def run_viscosity_change(
    robot_ip: str = DEFAULT_XARM_IP,
    lift_z_mm: float = DEFAULT_LIFT_Z_MM,
    speed: float = DEFAULT_SPEED,
    acceleration: float = DEFAULT_ACCELERATION,
):
    controller = XArmCameraController(
        robot_ip=robot_ip,
        speed=speed,
        acceleration=acceleration,
    )

    print("Reading current xArm pose.")
    original_pose = controller.read_pose()
    lifted_pose = list(original_pose)
    lifted_pose[2] += float(lift_z_mm)

    print_pose("original", original_pose)
    print_pose("lifted", lifted_pose)

    print(f"Moving xArm up by {lift_z_mm:g} mm in world Z.")
    final_lifted_pose = controller.move_to_pose(lifted_pose)
    print_pose("at_lifted", final_lifted_pose)

    print("Turning off both Tapo lights.")
    light_states = apply_light_condition(0)
    for name, state in light_states.items():
        print(f"{name}: requested_on={state['requested_on']} device_on={state['device_on']}")

    input("Replace the fluid, then press Enter here to move the xArm back down.")

    print("Moving xArm back to the original pose.")
    final_pose = controller.move_to_pose(original_pose)
    print_pose("final", final_pose)
    print("Done. Tapo lights remain off.")


def parse_args():
    parser = argparse.ArgumentParser(description="Move xArm up for fluid replacement.")
    parser.add_argument("--ip", default=DEFAULT_XARM_IP, help="xArm controller IP address.")
    parser.add_argument(
        "--lift-z-mm",
        type=float,
        default=DEFAULT_LIFT_Z_MM,
        help="World Z lift distance in millimeters.",
    )
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="Move speed in mm/s.")
    parser.add_argument(
        "--acceleration",
        type=float,
        default=DEFAULT_ACCELERATION,
        help="Move acceleration in mm/s^2.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        run_viscosity_change(
            robot_ip=args.ip,
            lift_z_mm=args.lift_z_mm,
            speed=args.speed,
            acceleration=args.acceleration,
        )
    except KeyboardInterrupt:
        print("\nInterrupted. xArm may still be at its current position; check before continuing.")
        sys.exit(130)


if __name__ == "__main__":
    main()

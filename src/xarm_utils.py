from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from xarm.wrapper import XArmAPI


DEFAULT_XARM_IP = "192.168.1.238"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRESET_PATH = PROJECT_ROOT / "configs" / "xarm_camera_presets.json"
DEFAULT_SPEED = 20.0
DEFAULT_ACCELERATION = 200.0


@dataclass(frozen=True)
class CameraPreset:
    name: str
    pose: list[float]
    created_at: str
    source: str
    robot_ip: str
    note: str = ""


class XArmCameraController:
    def __init__(
        self,
        robot_ip: str = DEFAULT_XARM_IP,
        preset_path: Path = PRESET_PATH,
        speed: float = DEFAULT_SPEED,
        acceleration: float = DEFAULT_ACCELERATION,
    ):
        self.robot_ip = robot_ip
        self.preset_path = preset_path
        self.speed = float(speed)
        self.acceleration = float(acceleration)

    def read_pose(self) -> list[float]:
        arm = self.connect()
        try:
            return get_pose_or_raise(arm)
        finally:
            arm.disconnect()

    def save_preset(self, name: str, pose: list[float], note: str = "") -> CameraPreset:
        validate_preset_name(name)
        validate_pose(pose)
        presets = load_presets(self.preset_path)
        preset = CameraPreset(
            name=name,
            pose=[round(float(value), 6) for value in pose],
            created_at=datetime.now().astimezone().isoformat(),
            source="manual_pose",
            robot_ip=self.robot_ip,
            note=note,
        )
        presets[name] = asdict(preset)
        save_presets(self.preset_path, presets)
        return preset

    def move_to_preset(self, name: str) -> list[float]:
        preset = self.get_preset(name)
        return self.move_to_pose(preset.pose)

    def move_to_pose(self, pose: list[float]) -> list[float]:
        validate_pose(pose)
        arm = self.connect()
        try:
            prepare_motion(arm)
            code = arm.set_position(
                x=pose[0],
                y=pose[1],
                z=pose[2],
                roll=pose[3],
                pitch=pose[4],
                yaw=pose[5],
                speed=self.speed,
                mvacc=self.acceleration,
                wait=True,
            )
            if code != 0:
                raise RuntimeError(f"xArm set_position failed with code {code}")
            return get_pose_or_raise(arm)
        finally:
            arm.disconnect()

    def get_preset(self, name: str) -> CameraPreset:
        presets = load_presets(self.preset_path)
        if name not in presets:
            raise KeyError(f"Unknown camera preset: {name}")
        data = presets[name]
        return CameraPreset(
            name=name,
            pose=[float(value) for value in data["pose"]],
            created_at=str(data.get("created_at", "")),
            source=str(data.get("source", "")),
            robot_ip=str(data.get("robot_ip", "")),
            note=str(data.get("note", "")),
        )

    def list_presets(self) -> dict[str, Any]:
        return load_presets(self.preset_path)

    def connect(self) -> XArmAPI:
        arm = XArmAPI(self.robot_ip)
        if not arm.connected:
            raise RuntimeError(f"Could not connect to xArm at {self.robot_ip}")
        return arm


def prepare_motion(arm: XArmAPI):
    warn_code = arm.get_err_warn_code()
    if warn_code[0] != 0 or warn_code[1] != [0, 0]:
        raise RuntimeError(f"xArm has error/warning state: {warn_code}")

    for label, code in (
        ("motion_enable", arm.motion_enable(enable=True)),
        ("set_mode", arm.set_mode(0)),
        ("set_state", arm.set_state(0)),
    ):
        if code != 0:
            raise RuntimeError(f"xArm {label} failed with code {code}")

    time.sleep(0.5)


def get_pose_or_raise(arm: XArmAPI) -> list[float]:
    code, pose = arm.get_position(is_radian=False)
    if code != 0:
        raise RuntimeError(f"xArm get_position failed with code {code}")
    return [float(value) for value in pose]


def validate_pose(pose: list[float]):
    if len(pose) != 6:
        raise ValueError("xArm pose must contain six values: x, y, z, roll, pitch, yaw.")


def validate_preset_name(name: str):
    if not name.startswith("camera"):
        raise ValueError("Camera preset names must start with 'camera', e.g. camera0.")


def load_presets(path: Path = PRESET_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_presets(path: Path, presets: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(presets, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description="Manage xArm camera position presets.")
    parser.add_argument("--ip", default=DEFAULT_XARM_IP, help="xArm controller IP address.")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="Move speed in mm/s.")
    parser.add_argument(
        "--acceleration",
        type=float,
        default=DEFAULT_ACCELERATION,
        help="Move acceleration in mm/s^2.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Read and print current xArm pose.")
    subparsers.add_parser("list", help="List saved camera presets.")

    save_pose = subparsers.add_parser("save-pose", help="Save manually entered pose as a preset.")
    save_pose.add_argument("name", help="Preset name, e.g. camera1.")
    save_pose.add_argument(
        "--pose",
        nargs=6,
        type=float,
        metavar=("X", "Y", "Z", "ROLL", "PITCH", "YAW"),
        required=True,
        help="Pose in millimeters and degrees: x y z roll pitch yaw.",
    )
    save_pose.add_argument("--note", default="", help="Optional preset note.")

    move = subparsers.add_parser("move", help="Move to a saved camera preset.")
    move.add_argument("name", help="Preset name, e.g. camera0.")

    return parser.parse_args()


def main():
    args = parse_args()
    controller = XArmCameraController(
        robot_ip=args.ip,
        speed=args.speed,
        acceleration=args.acceleration,
    )

    if args.command == "status":
        print_pose("current", controller.read_pose())
    elif args.command == "list":
        print(json.dumps(controller.list_presets(), indent=2, ensure_ascii=False))
    elif args.command == "save-pose":
        preset = controller.save_preset(args.name, args.pose, note=args.note)
        print(json.dumps(asdict(preset), indent=2, ensure_ascii=False))
    elif args.command == "move":
        final_pose = controller.move_to_preset(args.name)
        print_pose("final", final_pose)


def print_pose(label: str, pose: list[float]):
    formatted = ", ".join(f"{value:.6f}" for value in pose)
    print(f"{label}: [{formatted}]")


if __name__ == "__main__":
    main()

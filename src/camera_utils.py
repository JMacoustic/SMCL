from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from typing import Iterable, Optional

import cv2


ORBBEC_RGB_VID_PID = "2bc5:0800"
ORBBEC_NAME_HINT = "Orbbec"
DEFAULT_FALLBACK_CAMERA_INDEX = 2
DEFAULT_CAMERA_MODES = (
    (3840, 2160, 60),
    (3840, 2160, 30),
    (2560, 1440, 60),
    (2560, 1440, 30),
    (1920, 1080, 120),
    (1920, 1080, 60),
    (1920, 1080, 30),
    (1280, 720, 120),
    (1280, 720, 60),
    (1280, 720, 30),
)


def default_camera_backends() -> tuple[int, ...]:
    backends = [
        getattr(cv2, "CAP_MSMF", None),
        cv2.CAP_ANY,
        getattr(cv2, "CAP_DSHOW", None),
    ]
    unique_backends: list[int] = []

    for backend in backends:
        if backend is None:
            continue
        if backend not in unique_backends:
            unique_backends.append(backend)

    return tuple(unique_backends)


@dataclass
class CameraProperties:
    index: int
    backend: int
    width: int
    height: int
    fps: float
    fourcc: str


def directshow_device_names() -> list[str]:
    """Return DirectShow device names when pygrabber is installed."""
    try:
        from pygrabber.dshow_graph import FilterGraph
    except ImportError:
        return []

    graph = FilterGraph()
    return list(graph.get_input_devices())


def windows_pnp_camera_names_for_vid_pid(vid_pid: str = ORBBEC_RGB_VID_PID) -> list[str]:
    if platform.system() != "Windows":
        return []

    normalized = vid_pid.replace(":", "").replace("_", "").replace("-", "").lower()
    if len(normalized) != 8:
        return []

    vid = normalized[:4].upper()
    pid = normalized[4:].upper()
    command = (
        f"$pattern='*VID_{vid}&PID_{pid}*'; "
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.PNPDeviceID -like $pattern } | "
        "Select-Object -ExpandProperty Name"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def find_preferred_camera_index(
    preferred_name: str = ORBBEC_NAME_HINT,
    preferred_vid_pid: str = ORBBEC_RGB_VID_PID,
    fallback_index: int = DEFAULT_FALLBACK_CAMERA_INDEX,
) -> int:
    names = directshow_device_names()
    pnp_names = windows_pnp_camera_names_for_vid_pid(preferred_vid_pid)

    for pnp_name in pnp_names:
        pnp = pnp_name.lower()
        for index, name in enumerate(names):
            directshow_name = name.lower()
            if pnp in directshow_name or directshow_name in pnp:
                return index

    preferred = preferred_name.lower()

    for index, name in enumerate(names):
        if preferred in name.lower():
            return index

    return fallback_index


def open_camera(
    camera_index: Optional[int] = None,
    backend: Optional[int] = None,
    preferred_name: str = ORBBEC_NAME_HINT,
) -> tuple[cv2.VideoCapture, int, int]:
    preferred_index = (
        find_preferred_camera_index(preferred_name=preferred_name)
        if camera_index is None
        else int(camera_index)
    )

    indexes = [preferred_index]
    if camera_index is None:
        indexes.extend(index for index in range(6) if index != preferred_index)

    backends = (backend,) if backend is not None else default_camera_backends()
    attempts: list[str] = []

    for index in indexes:
        for candidate_backend in backends:
            capture = create_capture(index, candidate_backend)
            attempts.append(f"index={index}, backend={backend_name(candidate_backend)}")
            if capture.isOpened() and capture_can_read(capture):
                return capture, index, candidate_backend
            capture.release()

    raise RuntimeError("Could not open camera. Tried " + "; ".join(attempts) + ".")


def open_configured_camera(
    camera_index: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
    backend: Optional[int] = None,
    preferred_name: str = ORBBEC_NAME_HINT,
    modes: Iterable[tuple[int, int, int]] = DEFAULT_CAMERA_MODES,
) -> tuple[cv2.VideoCapture, CameraProperties]:
    preferred_index = (
        find_preferred_camera_index(preferred_name=preferred_name)
        if camera_index is None
        else int(camera_index)
    )

    indexes = [preferred_index]
    if camera_index is None:
        indexes.extend(index for index in range(6) if index != preferred_index)

    backends = (backend,) if backend is not None else default_camera_backends()
    requested_modes = [(width, height, fps)] if width and height else list(modes)
    requested_modes.append((None, None, None))

    best: Optional[tuple[tuple[int, float], cv2.VideoCapture, CameraProperties]] = None
    attempts: list[str] = []

    for index in indexes:
        for candidate_backend in backends:
            for mode_width, mode_height, mode_fps in requested_modes:
                capture = create_capture(index, candidate_backend)
                attempt = f"index={index}, backend={backend_name(candidate_backend)}"
                if mode_width and mode_height:
                    attempt += f", mode={mode_width}x{mode_height}@{mode_fps or 'default'}"
                attempts.append(attempt)

                props = try_configured_capture(
                    capture,
                    index,
                    candidate_backend,
                    mode_width,
                    mode_height,
                    mode_fps,
                )
                if props is None:
                    capture.release()
                    continue

                score_fps = props.fps if props.fps > 1.0 else float(mode_fps or 0)
                score = (props.width * props.height, score_fps)
                if best is None or score > best[0]:
                    if best is not None:
                        best[1].release()
                    best = (score, capture, props)
                else:
                    capture.release()

                if width and height:
                    if best is None:
                        continue
                    return best[1], best[2]

    if best is None:
        raise RuntimeError("Could not open camera. Tried " + "; ".join(attempts) + ".")

    return best[1], best[2]


def try_configured_capture(
    capture: cv2.VideoCapture,
    camera_index: int,
    backend: int,
    width: Optional[int],
    height: Optional[int],
    fps: Optional[float],
) -> Optional[CameraProperties]:
    try:
        if not capture.isOpened():
            return None

        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if width and height:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if fps:
            capture.set(cv2.CAP_PROP_FPS, float(fps))

        if not capture_can_read(capture):
            return None

        return read_camera_properties(capture, camera_index, backend)
    except cv2.error:
        return None


def capture_can_read(capture: cv2.VideoCapture, attempts: int = 5) -> bool:
    for _ in range(attempts):
        try:
            ok, frame = capture.read()
        except cv2.error:
            return False
        if ok and frame is not None and frame.size > 0:
            return True
    return False


def create_capture(index: int, backend: int) -> cv2.VideoCapture:
    if backend == cv2.CAP_ANY:
        return cv2.VideoCapture(index)
    return cv2.VideoCapture(index, backend)


def backend_name(backend: int) -> str:
    names = {
        cv2.CAP_ANY: "CAP_ANY",
        getattr(cv2, "CAP_DSHOW", -1): "CAP_DSHOW",
        getattr(cv2, "CAP_MSMF", -1): "CAP_MSMF",
    }
    return names.get(backend, str(backend))


def opencv_has_gui() -> bool:
    for line in cv2.getBuildInformation().splitlines():
        if line.strip().startswith("GUI:"):
            return "NONE" not in line.upper()
    return True


def safe_destroy_all_windows():
    try:
        cv2.destroyAllWindows()
    except cv2.error:
        pass


def configure_capture(
    capture: cv2.VideoCapture,
    camera_index: int,
    backend: int = cv2.CAP_DSHOW,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
    modes: Iterable[tuple[int, int, int]] = DEFAULT_CAMERA_MODES,
) -> CameraProperties:
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

    if width and height:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if fps:
            capture.set(cv2.CAP_PROP_FPS, float(fps))
        return read_camera_properties(capture, camera_index, backend)

    best_score = (-1, -1.0)
    best_mode: Optional[tuple[int, int, int]] = None

    for mode_width, mode_height, mode_fps in modes:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, mode_width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, mode_height)
        capture.set(cv2.CAP_PROP_FPS, mode_fps)
        ok, _ = capture.read()
        if not ok:
            continue

        props = read_camera_properties(capture, camera_index, backend)
        score = (props.width * props.height, props.fps)
        if score > best_score:
            best_score = score
            best_mode = (mode_width, mode_height, mode_fps)

    if best_mode:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, best_mode[0])
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, best_mode[1])
        capture.set(cv2.CAP_PROP_FPS, best_mode[2])

    return read_camera_properties(capture, camera_index, backend)


def read_camera_properties(
    capture: cv2.VideoCapture,
    camera_index: int,
    backend: int,
) -> CameraProperties:
    fourcc_value = int(capture.get(cv2.CAP_PROP_FOURCC))
    fourcc = "".join(chr((fourcc_value >> (8 * i)) & 0xFF) for i in range(4)).strip()

    return CameraProperties(
        index=camera_index,
        backend=backend,
        width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        fps=float(capture.get(cv2.CAP_PROP_FPS) or 0.0),
        fourcc=fourcc,
    )

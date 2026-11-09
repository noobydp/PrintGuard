"""FlashForge Creator 5 integration over the printer's local HTTP API.

The Creator 5 and Creator 5 Pro serve a JSON API on port 8898 once LAN mode
is on. Every request is a POST carrying the printer's serial number and the
check code its screen shows. ``/detail`` reports the job, per-tool and bed
temperatures and the camera, and ``/control`` takes ``jobCtl_cmd`` to pause,
continue or cancel the job. Each reply carries ``code``, 0 on success and -2
while LAN mode is off. The firmware acknowledges a command it doesn't know
with success too, so the engine's read-back after an action is what confirms
it was taken.

The printer serves its built-in camera as MJPEG on port 8080. The camera
allows one reader at a time on the firmware tested, so another app reading it
at the same moment refuses PrintGuard's connection.

The Creator 5 is a four-tool changer. PrintGuard reads one nozzle, so the
tool with the highest target stands for it, the one printing or heating up.

API reference (unofficial, from the firmware): https://github.com/Parallel-7/flashforge-api-docs
"""

from __future__ import annotations

from typing import Any

from .base import DeviceAction, DeviceState, DeviceStatus, Heater, HttpFn, IntegrationAdapter, require_reply

_API_PORT = 8898
_CAMERA_PORT = 8080
_CONTROL_TIMEOUT_S = 15.0
_LAN_MODE_OFF = -2
_ACTIONS = {DeviceAction.PAUSE: "pause", DeviceAction.RESUME: "continue", DeviceAction.CANCEL: "cancel"}
_STATUS = {
    "printing": DeviceStatus.PRINTING,
    "building": DeviceStatus.PRINTING,
    "pausing": DeviceStatus.PAUSED,
    "paused": DeviceStatus.PAUSED,
    "pause": DeviceStatus.PAUSED,
    "ready": DeviceStatus.IDLE,
    "completed": DeviceStatus.IDLE,
    "cancel": DeviceStatus.IDLE,
    "error": DeviceStatus.ERROR,
}
"""The printer's ``status``. A finished or cancelled job reads as idle until the next starts, and anything else, such as heating or loading filament, says nothing about a print."""


def _endpoint(config: dict[str, Any], path: str) -> str:
    return f"http://{str(config['host']).strip()}:{_API_PORT}/{path}"


def _credentials(config: dict[str, Any]) -> dict[str, str]:
    return {"serialNumber": str(config["serial"]).strip(), "checkCode": str(config["check_code"]).strip()}


def _require_code(doing: str, status: int, body: Any) -> dict[str, Any]:
    """Checks a reply is the printer's own and that it took the request.

    Raises:
        RuntimeError: If the printer answered with an error status, not like
            its API, or with a non-zero code, naming LAN mode when that is why.
    """
    require_reply("FlashForge", doing, status, status == 200 and isinstance(body, dict) and "code" in body)
    code = body["code"]
    if code == _LAN_MODE_OFF:
        raise RuntimeError("FlashForge refused the request because LAN mode is off. Turn it on in the printer's network settings")
    if code != 0:
        raise RuntimeError(f"FlashForge rejected {doing}: {body.get('message') or f'code {code}'}")
    return body


def _nozzle(detail: dict[str, Any]) -> Heater | None:
    """The tool with the highest target, or the hottest when none is heating."""
    temps, targets = detail.get("nozzleTemps"), detail.get("nozzleTargetTemps")
    if isinstance(temps, list) and temps:
        targets = targets if isinstance(targets, list) and len(targets) == len(temps) else [0] * len(temps)
        tool = max(range(len(temps)), key=lambda i: (targets[i] or 0, temps[i] or 0))
        return Heater.reported(temps[tool], targets[tool])
    return Heater.reported(detail.get("nozzleTemp"), detail.get("nozzleTargetTemp"))


class FlashForgeAdapter(IntegrationAdapter):
    """Talks to a FlashForge Creator 5 through its LAN-mode HTTP API."""

    id = "flashforge"
    label = "FlashForge Creator 5"
    docs_url = "https://github.com/Parallel-7/flashforge-api-docs"
    setup_hint = (
        "Turn on LAN mode on the printer's screen, which shows its check code, "
        "and copy the serial number from the printer's device information."
    )
    experimental = True
    schema = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "title": "Printer IP", "placeholder": "192.168.1.44"},
            "serial": {"type": "string", "title": "Serial number", "placeholder": "From the printer's device information"},
            "check_code": {
                "type": "string",
                "title": "Check code",
                "secret": True,
                "placeholder": "Shown when LAN mode is on",
            },
        },
        "required": ["host", "serial", "check_code"],
    }

    async def fetch_state(self, http: HttpFn, config: dict[str, Any]) -> DeviceState:
        """Reads the job, temperatures and status from /detail.

        ``printProgress`` is a fraction of the file read and ``estimatedTime``
        the seconds the printer expects the job to take from now.

        Raises:
            RuntimeError: If the printer answers with anything but its own
                reply, or refuses the serial number and check code.
        """
        status, body = await http("POST", _endpoint(config, "detail"), json=_credentials(config))
        detail = _require_code("its state", status, body).get("detail")
        require_reply("FlashForge", "its state", status, isinstance(detail, dict))
        state = _STATUS.get(str(detail.get("status") or "").strip().lower(), DeviceStatus.UNKNOWN)
        active = state in (DeviceStatus.PRINTING, DeviceStatus.PAUSED)
        remaining = detail.get("estimatedTime")
        return DeviceState(
            state,
            float(detail.get("printProgress") or 0) * 100 if active else 0.0,
            (detail.get("printFileName") or None) if active else None,
            remaining_s=int(remaining) if active and isinstance(remaining, int | float) and remaining > 0 else None,
            nozzle=_nozzle(detail),
            bed=Heater.reported(detail.get("platTemp"), detail.get("platTargetTemp")),
        )

    async def send(self, http: HttpFn, config: dict[str, Any], action: DeviceAction) -> None:
        """Pauses, continues or cancels the job through /control's ``jobCtl_cmd``.

        Raises:
            RuntimeError: If the printer refuses the command.
        """
        status, body = await http(
            "POST",
            _endpoint(config, "control"),
            json={**_credentials(config), "payload": {"cmd": "jobCtl_cmd", "args": {"jobID": "", "action": _ACTIONS[action]}}},
            timeout=_CONTROL_TIMEOUT_S,
        )
        _require_code(action.value, status, body)

    async def cameras(self, http: HttpFn, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Exposes the built-in camera's MJPEG stream on port 8080."""
        url = f"http://{str(config['host']).strip()}:{_CAMERA_PORT}/?action=stream"
        return [{"key": "camera", "name": "FlashForge camera", "source": {"kind": "url", "url": url}}]

"""Solve image CAPTCHAs through the OmoCaptcha API.

WHY a separate module: a handler should ask one function "what does this
picture say?" and get back a string. Everything about talking to OmoCaptcha --
the two-step create/poll protocol, the JSON shapes, the error codes, keeping
the key out of the logs -- lives here, mirroring ``kiotproxy.py``.

The client key is a paid secret. It is sent only to OmoCaptcha's ``BASE_URL``
and is never written to a log in full (use ``mask_key``). The base64 image is
large and uninteresting, so it is never logged either.

Vendor doc: https://docs.omocaptcha.com/vi/api/image-to-text-task
    POST {BASE_URL}/createTask      {clientKey, task:{type, imageBase64, module?}}
                                    -> {errorId:0, taskId} | {errorId:1, errorCode, errorDescription}
    POST {BASE_URL}/getTaskResult   {clientKey, taskId}
                                    -> {errorId:0, status:"ready"|"processing", solution:{text}}
    POST {BASE_URL}/getBalance      {clientKey} -> {errorId:0, balance, voucherBalance}

Every response is HTTP 200; ``errorId`` distinguishes success from a business
error, so the body -- not the HTTP status -- is what this module inspects.

The calls here block (HTTP plus 2-second polling). Everything in the framework
runs on one asyncio event loop, so a handler must call ``solve`` through
``asyncio.to_thread`` to avoid freezing the other workers.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BASE_URL = "https://api.omocaptcha.com/v2"

# Default location of the client key, one key on its own line. Gitignored like
# ``keys.txt``. ``load_client_key`` and ``solve`` read it when no key is passed.
KEY_FILE = Path("omocaptcha.txt")

# Error codes the vendor marks as transient (service busy / maintenance /
# infrastructure). Only these are worth retrying; everything else (bad key,
# empty balance, unsolvable) is terminal and retrying just wastes money/time.
_RETRYABLE_CODES = frozenset({
    "ERROR_TASK_IS_MAINTENANCE",
    "ERROR_SERVICE_UNAVAILABLE",
    "ERROR_REDIS_UNAVAILABLE",
    "ERROR_REDIS_JOB_DATA",
})


class OmoCaptchaError(Exception):
    """An OmoCaptcha call failed: transport, bad body, or ``errorId: 1``.

    ``code`` carries the API's ``errorCode`` string when the failure came from
    a structured error body; it is ``None`` for transport/parse failures.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def mask_key(key: str) -> str:
    """Return a log-safe form of a key: first 4 + '…' + last 4, or all '*'.

    Short keys (<= 8 chars) are fully masked since first/last 4 would reveal
    the whole thing.
    """
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def load_client_key(path: Path | None = None) -> str:
    """Read the single client key from ``path`` (blank lines and ``#`` ignored).

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError`` if it
    holds no key. Only the first key is used -- OmoCaptcha has one account key.
    ``path`` defaults to ``KEY_FILE``, read at call time so a test (or caller)
    that reassigns ``KEY_FILE`` is honoured.
    """
    if path is None:
        path = KEY_FILE
    if not path.exists():
        raise FileNotFoundError(f"key file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    raise ValueError(f"no key found in {path}")


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a request body safe to log: key masked, image summarised.

    The ``clientKey`` is replaced by its masked form and any ``imageBase64`` is
    replaced by its length, so a log line shows the shape of the request
    without leaking the secret or dumping tens of kilobytes of base64.
    """
    redacted = dict(payload)
    if "clientKey" in redacted:
        redacted["clientKey"] = mask_key(str(redacted["clientKey"]))
    task = redacted.get("task")
    if isinstance(task, dict) and "imageBase64" in task:
        task = dict(task)
        task["imageBase64"] = f"<{len(str(task['imageBase64']))} base64 chars>"
        redacted["task"] = task
    return redacted


def _post_json(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """POST ``payload`` as JSON to ``url`` and parse a JSON object.

    Raises ``OmoCaptchaError`` on a transport error, a non-JSON body, or a
    non-object body. The request (key masked, image summarised) and the
    response body are logged at INFO. On an HTTP error the error body is logged
    before raising, so a 4xx/5xx is never silent.
    """
    endpoint = url.rsplit("/", 1)[-1]
    # ensure_ascii=False keeps the '…' in the masked key literal instead of
    # escaping it to …, so the log line stays readable.
    log.info(
        "OmoCaptcha request: POST %s %s",
        endpoint,
        json.dumps(_redact_payload(payload), ensure_ascii=False),
    )
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
            status = getattr(response, "status", None)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001 - body may be unreadable; the status still helps
            pass
        log.info("OmoCaptcha response (HTTP %s): %s", exc.code, body)
        raise OmoCaptchaError(f"request to OmoCaptcha failed: {exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise OmoCaptchaError(f"request to OmoCaptcha failed: {exc}") from exc
    log.info("OmoCaptcha response (HTTP %s): %s", status, raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OmoCaptchaError(f"OmoCaptcha returned non-JSON body: {raw[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise OmoCaptchaError(f"OmoCaptcha returned a non-object body: {parsed!r}")
    return parsed


def _raise_for_error(body: dict[str, Any]) -> None:
    """Raise ``OmoCaptchaError`` (carrying ``errorCode``) when ``errorId`` is set."""
    if body.get("errorId"):
        raise OmoCaptchaError(
            body.get("errorDescription") or "OmoCaptcha returned an error",
            code=body.get("errorCode"),
        )


def create_task(
    image: bytes,
    *,
    client_key: str,
    module: str | None = None,
    base_url: str | None = None,
    timeout_s: float = 30.0,
) -> str:
    """Submit ``image`` (raw PNG/JPEG bytes) for solving and return its ``taskId``.

    Raises ``OmoCaptchaError`` on an API error (e.g. bad key, no balance).
    """
    resolved = base_url or BASE_URL
    task: dict[str, Any] = {
        "type": "ImageToTextTask",
        "imageBase64": base64.b64encode(image).decode("ascii"),
    }
    if module:
        task["module"] = module
    body = _post_json(f"{resolved}/createTask", {"clientKey": client_key, "task": task}, timeout_s)
    _raise_for_error(body)
    task_id = body.get("taskId")
    if not task_id:
        raise OmoCaptchaError(f"createTask succeeded but returned no taskId: {body!r}")
    return str(task_id)


def get_task_result(
    task_id: str,
    *,
    client_key: str,
    base_url: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Fetch the current result for ``task_id``.

    Returns the parsed body (``status`` is ``"ready"`` or ``"processing"``).
    Raises ``OmoCaptchaError`` when the API reports an error (e.g. the solver
    failed, ``ERROR_JOB_STATUS``).
    """
    resolved = base_url or BASE_URL
    body = _post_json(
        f"{resolved}/getTaskResult", {"clientKey": client_key, "taskId": task_id}, timeout_s
    )
    _raise_for_error(body)
    return body


def get_balance(
    *, client_key: str | None = None, base_url: str | None = None, timeout_s: float = 30.0
) -> dict[str, Any]:
    """Return the account balance body (``balance``, ``voucherBalance``).

    Raises ``OmoCaptchaError`` on an API error. ``client_key`` defaults to the
    key read from ``KEY_FILE``.
    """
    key = client_key or load_client_key()
    resolved = base_url or BASE_URL
    body = _post_json(f"{resolved}/getBalance", {"clientKey": key}, timeout_s)
    _raise_for_error(body)
    return body


def solve(
    image: bytes,
    *,
    client_key: str | None = None,
    module: str | None = None,
    base_url: str | None = None,
    poll_interval_s: float = 2.0,
    max_wait_s: float = 120.0,
    timeout_s: float = 30.0,
) -> str:
    """Solve one image CAPTCHA and return the recognised text.

    Submits the image, then polls ``getTaskResult`` every ``poll_interval_s``
    seconds until the solver is ``ready`` (returning ``solution.text``) or
    ``max_wait_s`` elapses. ``client_key`` defaults to the key read from
    ``KEY_FILE``. Blocks; call via ``asyncio.to_thread`` from a handler.

    Raises ``OmoCaptchaError`` on an API error or when the solver does not
    finish within ``max_wait_s``.
    """
    key = client_key or load_client_key()
    task_id = create_task(
        image, client_key=key, module=module, base_url=base_url, timeout_s=timeout_s
    )

    deadline = time.monotonic() + max_wait_s
    while True:
        result = get_task_result(task_id, client_key=key, base_url=base_url, timeout_s=timeout_s)
        if result.get("status") == "ready":
            text = (result.get("solution") or {}).get("text")
            if not text:
                raise OmoCaptchaError(f"ready result has no solution text: {result!r}")
            return str(text)
        if time.monotonic() >= deadline:
            raise OmoCaptchaError(
                f"OmoCaptcha did not solve task {task_id} within {max_wait_s:g}s"
            )
        time.sleep(poll_interval_s)

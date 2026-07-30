from __future__ import annotations

import base64
import json
import random
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


SDK_VERSION = "20260219f9f6"
SCRIPT_SRC = f"https://sentinel.openai.com/sentinel/{SDK_VERSION}/sdk.js"
SENTINEL_URL = "https://sentinel.openai.com/backend-api/sentinel/req"


class ProofGenerator:
    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, user_agent: str) -> None:
        self.device_id = device_id
        self.user_agent = user_agent
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _hash(value: str) -> str:
        result = 2166136261
        for char in value:
            result ^= ord(char)
            result = (result * 16777619) & 0xFFFFFFFF
        result ^= result >> 16
        result = (result * 2246822507) & 0xFFFFFFFF
        result ^= result >> 13
        result = (result * 3266489909) & 0xFFFFFFFF
        result ^= result >> 16
        return format(result & 0xFFFFFFFF, "08x")

    @staticmethod
    def _encode(value: Any) -> str:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _config(self) -> list[Any]:
        performance_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            SCRIPT_SRC,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            random.choice(("vendorSub-undefined", "plugins-undefined", "hardwareConcurrency-undefined")),
            random.choice(("location", "implementation", "documentURI", "compatMode")),
            random.choice(("Object", "Function", "Array", "Number", "undefined")),
            performance_now,
            self.sid,
            "",
            random.choice((4, 8, 12, 16)),
            time.time() * 1000 - performance_now,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
        ]

    def requirements(self) -> str:
        data = self._config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._encode(data)

    def proof(self, seed: str, difficulty: str) -> str:
        started = time.time()
        data = self._config()
        target = str(difficulty or "0")
        for attempt in range(self.MAX_ATTEMPTS):
            data[3] = attempt
            data[9] = round((time.time() - started) * 1000)
            payload = self._encode(data)
            if self._hash(seed + payload)[: len(target)] <= target:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._encode(str(None))


def _headers(user_agent: str, sec_ch_ua: str) -> dict[str, str]:
    return {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SDK_VERSION}",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def _request_challenge(session: Any, generator: ProofGenerator, device_id: str, flow: str, timeout_ms: int) -> tuple[str, dict[str, Any]]:
    proof = generator.requirements()
    response = session.post(
        SENTINEL_URL,
        data=json.dumps({"p": proof, "id": device_id, "flow": flow}, separators=(",", ":")),
        headers=_headers(generator.user_agent, _sec_ch_ua(generator.user_agent)),
        timeout=max(20, int(timeout_ms / 1000)),
        verify=False,
    )
    try:
        data = response.json() if response.text else {}
    except Exception as exc:
        raise RuntimeError(f"Sentinel 响应不是 JSON: HTTP {response.status_code}") from exc
    token = str(data.get("token") or "").strip()
    if response.status_code != 200 or not token:
        raise RuntimeError(f"Sentinel 请求失败: HTTP {response.status_code}: {response.text[:240]}")
    return proof, data


def _sec_ch_ua(user_agent: str) -> str:
    import re

    match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent)
    major = match.group(1) if match else "145"
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not/A)Brand";v="99"'


def _run_vm(kind: str, challenge: dict[str, Any], proof: str, device_id: str, flow: str, user_agent: str, settings: dict[str, Any]) -> str:
    root = Path(__file__).resolve().parents[2]
    script = root / "tools" / "openai_so_vm.mjs"
    if not script.exists():
        raise RuntimeError(f"Sentinel VM 文件不存在: {script}")
    payload: dict[str, Any] = {
        "p": proof,
        "device_id": device_id,
        "flow": flow,
        "user_agent": user_agent,
        "href": "https://auth.openai.com/log-in-or-create-account",
    }
    if kind == "so":
        payload.update({"collector_dx": challenge.get("collector_dx"), "snapshot_dx": challenge.get("snapshot_dx")})
    else:
        payload["turnstile_dx"] = challenge.get("dx")
    timeout_ms = max(30_000, int(settings.get("timeout_ms") or 75_000))
    completed = subprocess.run(
        [str(settings.get("node") or "node"), str(script)],
        input=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=int(timeout_ms / 1000) + 10,
        cwd=str(root),
        check=False,
    )
    stdout = (completed.stdout or "").strip()
    if completed.returncode != 0 or not stdout:
        detail = (completed.stderr or stdout or f"exit={completed.returncode}")[-600:]
        raise RuntimeError(f"Sentinel VM 执行失败: {detail}")
    try:
        data = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Sentinel VM 输出格式错误: {stdout[-500:]}") from exc
    key = "so" if kind == "so" else "turnstile"
    value = str(data.get(key) or "").strip()
    if not data.get("ok") or not value:
        raise RuntimeError(f"Sentinel VM 未返回 {key}: {str(data)[:500]}")
    return value


def build_standard_token(session: Any, device_id: str, flow: str, user_agent: str, settings: dict[str, Any]) -> tuple[str, str]:
    generator = ProofGenerator(device_id, user_agent)
    proof, data = _request_challenge(session, generator, device_id, flow, int(settings.get("timeout_ms") or 75_000))
    work = data.get("proofofwork") if isinstance(data.get("proofofwork"), dict) else {}
    proof_value = (
        generator.proof(str(work.get("seed") or ""), str(work.get("difficulty") or "0"))
        if work.get("required") and work.get("seed")
        else generator.requirements()
    )
    turnstile = data.get("turnstile") if isinstance(data.get("turnstile"), dict) else {}
    turnstile_value = ""
    if turnstile.get("required") and turnstile.get("dx"):
        turnstile_value = _run_vm("turnstile", turnstile, proof, device_id, flow, user_agent, settings)
    token = str(data.get("token") or "")
    value = json.dumps(
        {"p": proof_value, "t": turnstile_value, "c": token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    return value, "0" + token


def build_so_token(session: Any, device_id: str, flow: str, user_agent: str, settings: dict[str, Any]) -> tuple[str, str]:
    generator = ProofGenerator(device_id, user_agent)
    proof, data = _request_challenge(session, generator, device_id, flow, int(settings.get("timeout_ms") or 75_000))
    token = str(data.get("token") or "")
    challenge = data.get("so") if isinstance(data.get("so"), dict) else {}
    if not (challenge.get("required") is True and challenge.get("collector_dx") and challenge.get("snapshot_dx")):
        return "", "0" + token
    so_value = _run_vm("so", challenge, proof, device_id, flow, user_agent, settings)
    value = json.dumps({"so": so_value, "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))
    return value, "0" + token


from __future__ import annotations

import base64
import json
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    extension: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class GenerationResponse:
    images: list[GeneratedImage]
    request_id: str | None
    model: str | None
    raw_usage: dict[str, Any] | None


class ArkApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
        self.retryable = retryable


def detect_extension(content: bytes) -> str:
    # 不依赖远端返回的文件名，而是读取文件头（magic bytes）判断真实格式。
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def image_as_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError(f"不支持的图片格式：{path.name}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class ArkImageClient:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        size: str,
        response_format: str,
        watermark: bool,
        timeout_seconds: int,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.size = size
        self.response_format = response_format
        self.watermark = watermark
        self.timeout_seconds = timeout_seconds

    def build_payload(
        self, source_path: Path, prompt: str, mode: str, max_images: int
    ) -> dict[str, Any]:
        # 方舟接口同时支持单图和组图；两种模式只在 sequential 参数上有区别。
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "image": [image_as_data_url(source_path)],
            "size": self.size,
            "sequential_image_generation": "auto" if mode == "group" else "disabled",
            "response_format": self.response_format,
            "watermark": self.watermark,
        }
        if mode == "group":
            payload["sequential_image_generation_options"] = {"max_images": max_images}
        return payload

    def generate(
        self, source_path: Path, prompt: str, mode: str, max_images: int
    ) -> GenerationResponse:
        # 这里刻意把“构造请求”和“解析响应”分开，便于测试，也便于替换供应商。
        payload = self.build_payload(source_path, prompt, mode, max_images)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                response_body = response.read()
                response_headers = response.headers
        except urllib.error.HTTPError as exc:
            # HTTPError 同时是异常和响应对象，因此仍能读取服务端返回的 JSON 错误详情。
            response_body = exc.read()
            raise self._http_error(exc.code, response_body, dict(exc.headers)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ArkApiError(f"方舟网络请求失败：{exc}", retryable=True) from exc

        try:
            data = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArkApiError("方舟返回了无法解析的响应", retryable=False) from exc
        return self._parse_response(data, dict(response_headers))

    def _parse_response(
        self, payload: dict[str, Any], headers: dict[str, str]
    ) -> GenerationResponse:
        # API 可能返回 Base64、Data URL 或普通下载 URL，统一转换成 bytes 给流水线保存。
        entries = payload.get("data")
        if not isinstance(entries, list):
            error_value = payload.get("error")
            error: dict[str, Any] = error_value if isinstance(error_value, dict) else {}
            message = str(
                error.get("message")
                or payload.get("message")
                or "方舟响应缺少 data 数组"
            )
            raise ArkApiError(
                message,
                error_code=str(error.get("code") or "invalid_response"),
                request_id=self._request_id(payload, headers),
            )

        images: list[GeneratedImage] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content: bytes | None = None
            b64_value = entry.get("b64_json")
            url_value = entry.get("url")
            if isinstance(b64_value, str) and b64_value:
                try:
                    content = base64.b64decode(b64_value, validate=True)
                except ValueError as exc:
                    raise ArkApiError("方舟返回的 Base64 图片无效") from exc
            elif isinstance(url_value, str) and url_value.startswith("data:image/"):
                try:
                    content = base64.b64decode(
                        url_value.split(",", 1)[1], validate=True
                    )
                except (IndexError, ValueError) as exc:
                    raise ArkApiError("方舟返回的 Data URL 图片无效") from exc
            elif isinstance(url_value, str) and url_value:
                content = self._download(url_value)
            if content:
                images.append(
                    GeneratedImage(
                        content=content,
                        extension=detect_extension(content),
                        width=_optional_int(entry.get("width")),
                        height=_optional_int(entry.get("height")),
                    )
                )
        if not images:
            raise ArkApiError(
                "方舟响应中没有可保存的图片",
                request_id=self._request_id(payload, headers),
            )
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        return GenerationResponse(
            images=images,
            request_id=self._request_id(payload, headers),
            model=str(payload.get("model")) if payload.get("model") else None,
            raw_usage=usage,
        )

    def _download(self, url: str) -> bytes:
        # response_format 不是 b64_json 时，生成接口可能只返回短期有效的图片地址。
        request = urllib.request.Request(url, headers={"Accept": "image/*"})
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise ArkApiError(f"生成图片下载失败：{exc}", retryable=True) from exc

    @staticmethod
    def _request_id(payload: dict[str, Any], headers: dict[str, str]) -> str | None:
        for key in ("request_id", "id"):
            value = payload.get(key)
            if value:
                return str(value)
        lowered = {key.lower(): value for key, value in headers.items()}
        value = lowered.get("x-request-id") or lowered.get("x-tt-logid")
        return str(value) if value else None

    @staticmethod
    def _http_error(status: int, body: bytes, headers: dict[str, str]) -> ArkApiError:
        payload: dict[str, Any] = {}
        try:
            decoded = json.loads(body.decode("utf-8"))
            if isinstance(decoded, dict):
                payload = decoded
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        error_value = payload.get("error")
        error: dict[str, Any] = error_value if isinstance(error_value, dict) else {}
        code = str(error.get("code") or payload.get("code") or f"http_{status}")
        message = str(
            error.get("message")
            or payload.get("message")
            or f"方舟请求失败，HTTP {status}"
        )
        request_id = ArkImageClient._request_id(payload, headers)
        retryable = status == 429 or status >= 500
        return ArkApiError(
            message,
            status_code=status,
            error_code=code,
            request_id=request_id,
            retryable=retryable,
        )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

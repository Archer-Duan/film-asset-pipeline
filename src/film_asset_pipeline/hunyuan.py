from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Submitted3DJob:
    job_id: str
    request_id: str | None
    status: str


@dataclass(frozen=True)
class Remote3DFile:
    file_type: str
    url: str
    preview_url: str | None


@dataclass(frozen=True)
class Queried3DJob:
    job_id: str
    request_id: str | None
    status: str
    files: list[Remote3DFile]
    credits_consumed: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class HunyuanApiError(RuntimeError):
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


class Hunyuan3DClient:
    def __init__(
        self,
        *,
        api_style: str,
        submit_endpoint: str,
        query_endpoint: str,
        api_key: str,
        secret_id: str = "",
        secret_key: str = "",
        cloud_endpoint: str = "ai3d.tencentcloudapi.com",
        cloud_version: str = "2025-05-13",
        cloud_region: str = "ap-guangzhou",
        model: str,
        minimal_request: bool = False,
        enable_pbr: bool,
        face_count: int,
        generate_type: str,
        timeout_seconds: int,
    ) -> None:
        self.api_style = api_style
        self.submit_endpoint = submit_endpoint
        self.query_endpoint = query_endpoint
        self.api_key = api_key
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.cloud_endpoint = cloud_endpoint
        self.cloud_version = cloud_version
        self.cloud_region = cloud_region
        self.model = model
        self.minimal_request = minimal_request
        self.enable_pbr = enable_pbr
        self.face_count = face_count
        self.generate_type = generate_type
        self.timeout_seconds = timeout_seconds

    def build_submit_payload(self, source_path: Path) -> dict[str, Any]:
        if source_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError(f"3D 输入图片超过 8MB：{source_path.name}")
        image_base64 = base64.b64encode(source_path.read_bytes()).decode("ascii")
        if self.api_style == "tencentcloud_sdk":
            payload = {
                "Model": self.model.removeprefix("hy-3d-"),
                "ImageBase64": image_base64,
            }
            if not self.minimal_request:
                payload.update(
                    {
                        "EnablePBR": self.enable_pbr,
                        "FaceCount": self.face_count,
                        "GenerateType": self.generate_type,
                    }
                )
            return payload
        if self.api_style == "ai3d_openai":
            mime = "image/png" if source_path.suffix.lower() == ".png" else "image/jpeg"
            payload: dict[str, Any] = {
                "Model": self.model.removeprefix("hy-3d-"),
                "ImageUrl": {"Url": f"data:{mime};base64,{image_base64}"},
            }
            if not self.minimal_request:
                payload.update(
                    {
                        "EnablePBR": self.enable_pbr,
                        "FaceCount": self.face_count,
                        "GenerateType": self.generate_type,
                    }
                )
            return payload
        return {
            "model": self.model,
            "image_base64": image_base64,
            "enable_pbr": self.enable_pbr,
            "face_count": self.face_count,
            "generate_type": self.generate_type,
        }

    def submit(self, source_path: Path) -> Submitted3DJob:
        submit_payload = self.build_submit_payload(source_path)
        payload = (
            self._cloud_call("SubmitHunyuanTo3DProJob", submit_payload)
            if self.api_style == "tencentcloud_sdk"
            else self._post(self.submit_endpoint, submit_payload)
        )
        payload = self._unwrap_response(payload)
        job_id = payload.get("id") or payload.get("job_id") or payload.get("JobId")
        if not job_id:
            raise self._response_error(payload, "混元提交响应缺少任务 ID")
        return Submitted3DJob(
            job_id=str(job_id),
            request_id=_optional_string(payload.get("request_id") or payload.get("RequestId")),
            status=str(payload.get("status") or "queued").lower(),
        )

    def query(self, job_id: str) -> Queried3DJob:
        query_payload = (
            {"JobId": job_id}
            if self.api_style in {"tencentcloud_sdk", "ai3d_openai"}
            else {"model": self.model, "id": job_id}
        )
        payload = self._unwrap_response(
            self._cloud_call("QueryHunyuanTo3DProJob", query_payload)
            if self.api_style == "tencentcloud_sdk"
            else self._post(self.query_endpoint, query_payload)
        )
        status = str(payload.get("status") or payload.get("Status") or "unknown").lower()
        files: list[Remote3DFile] = []
        entries = payload.get("data") or payload.get("result_file_3ds") or payload.get("ResultFile3Ds") or []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                url = entry.get("url") or entry.get("Url")
                file_type = entry.get("type") or entry.get("Type")
                if url and file_type:
                    files.append(
                        Remote3DFile(
                            file_type=str(file_type).lower(),
                            url=str(url),
                            preview_url=_optional_string(
                                entry.get("preview_image_url") or entry.get("PreviewImageUrl")
                            ),
                        )
                    )
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        return Queried3DJob(
            job_id=job_id,
            request_id=_optional_string(payload.get("request_id") or payload.get("RequestId")),
            status=status,
            files=files,
            credits_consumed=_optional_float(
                payload.get("result_credit_consumed") or payload.get("ResultCreditConsumed")
            ),
            error_code=_optional_string(
                error.get("code") or payload.get("error_code") or payload.get("ErrorCode")
            ),
            error_message=_optional_string(
                error.get("message") or payload.get("error_message") or payload.get("ErrorMessage")
            ),
        )

    def download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"Accept": "*/*"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise HunyuanApiError(f"混元结果下载失败：{exc}", retryable=True) from exc

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        authorization = self.api_key if self.api_style == "ai3d_openai" else f"Bearer {self.api_key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise self._http_error(exc.code, raw) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HunyuanApiError(f"混元网络请求失败：{exc}", retryable=True) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HunyuanApiError("混元返回了无法解析的响应") from exc
        if not isinstance(decoded, dict):
            raise HunyuanApiError("混元响应不是 JSON 对象")
        if isinstance(decoded.get("error"), dict):
            raise self._response_error(decoded, "混元返回错误")
        return decoded

    def _cloud_call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.common_client import CommonClient
            from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
                TencentCloudSDKException,
            )
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
        except ModuleNotFoundError as exc:
            raise HunyuanApiError(
                "缺少腾讯云 SDK；请执行 pip install -e . 安装项目依赖"
            ) from exc

        try:
            cred = credential.Credential(self.secret_id, self.secret_key)
            http_profile = HttpProfile()
            http_profile.endpoint = self.cloud_endpoint
            http_profile.reqTimeout = self.timeout_seconds
            client_profile = ClientProfile()
            client_profile.httpProfile = http_profile
            client = CommonClient(
                "ai3d",
                self.cloud_version,
                cred,
                self.cloud_region,
                profile=client_profile,
            )
            response = client.call_json(action, payload)
        except TencentCloudSDKException as exc:
            raise HunyuanApiError(
                str(exc),
                error_code=_optional_string(getattr(exc, "code", None)),
                request_id=_optional_string(getattr(exc, "requestId", None)),
                retryable=_optional_string(getattr(exc, "code", None))
                in {"InternalError", "RequestLimitExceeded", "ServiceUnavailable"},
            ) from exc
        if not isinstance(response, dict):
            raise HunyuanApiError("腾讯云 SDK 响应不是 JSON 对象")
        return response

    @staticmethod
    def _unwrap_response(payload: dict[str, Any]) -> dict[str, Any]:
        response = payload.get("Response")
        if isinstance(response, dict):
            error = response.get("Error")
            if isinstance(error, dict):
                raise HunyuanApiError(
                    str(error.get("Message") or "混元返回错误"),
                    error_code=_optional_string(error.get("Code")),
                    request_id=_optional_string(response.get("RequestId")),
                )
            return response
        return payload

    @staticmethod
    def _response_error(payload: dict[str, Any], fallback: str) -> HunyuanApiError:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = _optional_string(error.get("code") or payload.get("error_code"))
        message = str(error.get("message") or payload.get("error_message") or fallback)
        request_id = _optional_string(payload.get("request_id") or payload.get("RequestId"))
        return HunyuanApiError(message, error_code=code, request_id=request_id)

    @staticmethod
    def _http_error(status: int, raw: bytes) -> HunyuanApiError:
        payload: dict[str, Any] = {}
        raw_text = ""
        try:
            raw_text = raw.decode("utf-8", errors="replace").strip()
            decoded = json.loads(raw_text)
            if isinstance(decoded, dict):
                payload = decoded
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        response = payload.get("Response") if isinstance(payload.get("Response"), dict) else {}
        legacy_error = response.get("Error") if isinstance(response.get("Error"), dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        fallback_detail = ""
        if raw_text and len(raw_text) <= 1000 and "base64" not in raw_text.lower():
            fallback_detail = raw_text
        message = str(
            legacy_error.get("Message")
            or error.get("message")
            or payload.get("message")
            or payload.get("Msg")
            or payload.get("Desc")
            or fallback_detail
            or f"混元请求失败，HTTP {status}"
        )
        code = _optional_string(
            legacy_error.get("Code")
            or error.get("code")
            or payload.get("code")
            or payload.get("Code")
            or f"http_{status}"
        )
        request_id = _optional_string(
            response.get("RequestId") or payload.get("request_id") or payload.get("RequestId")
        )
        return HunyuanApiError(
            message,
            status_code=status,
            error_code=code,
            request_id=request_id,
            retryable=status == 429 or status >= 500,
        )


def _optional_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# 保留旧名称，避免早期本地脚本导入失败。
TokenHub3DClient = Hunyuan3DClient

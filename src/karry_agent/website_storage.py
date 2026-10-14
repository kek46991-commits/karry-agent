"""Private S3-compatible storage for immutable published HTML (R2 or AWS S3)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

FILENAME_RE = re.compile(r"[A-Za-z0-9_-]{1,100}_[a-f0-9]{32}\.html")
MAX_HTML_BYTES = 524_288


class StorageError(ValueError):
    def __init__(self, message: str, code: str = "storage_unavailable", *, diagnostic_code: str | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostic_code = diagnostic_code


DIAGNOSTIC_HINTS = {
    "storage_bucket_missing": "指定したR2/S3バケットが見つかりません。バケット名とアカウントを確認してください。",
    "storage_permission": "保存先の読み取り権限が拒否されました。対象バケットのObject Read & Write権限を確認してください。",
    "storage_credentials": "保存先のS3アクセスキーが無効または期限切れです。R2/S3署名用のキーを秘密設定へ登録してください。",
    "storage_signature": "S3署名が一致しません。アクセスキーとシークレットの組、endpoint・regionを確認してください。",
    "storage_endpoint": "保存先のendpoint・regionが一致しません。対象アカウントのS3エンドポイントを確認してください。",
    "storage_network": "保存先とのネットワーク接続に失敗しました。endpointとサービス状態を確認してください。",
    "storage_rate_limit": "保存先がリクエストを制限しています。時間を置いて再接続してください。",
    "storage_unsupported": "保存先が必要なS3操作に対応していません。R2またはAWS S3を使用してください。",
    "storage_unknown": "永続保存へ接続できません。管理者がバケット・署名キー・権限・endpointを確認してください。",
}


def connection_diagnostic(exc: Exception) -> str:
    """Only allowlisted categories, never provider messages, keys or request URLs."""
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return {
            "NoSuchBucket": "storage_bucket_missing",
            "AccessDenied": "storage_permission", "AllAccessDisabled": "storage_permission", "403": "storage_permission",
            "InvalidAccessKeyId": "storage_credentials", "InvalidToken": "storage_credentials", "ExpiredToken": "storage_credentials",
            "SignatureDoesNotMatch": "storage_signature",
            "PermanentRedirect": "storage_endpoint", "AuthorizationHeaderMalformed": "storage_endpoint",
            "SlowDown": "storage_rate_limit", "Throttling": "storage_rate_limit",
            "NotImplemented": "storage_unsupported",
        }.get(code, "storage_unknown")
    if isinstance(exc, (BotoCoreError, OSError)):
        return "storage_network"
    return "storage_unknown"


@dataclass(frozen=True)
class StorageConfig:
    bucket: str
    prefix: str
    endpoint: str | None
    region: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)

    @property
    def identity(self) -> str:
        # Key rotations do not change the actual storage destination.
        data = [self.bucket, self.prefix, self.endpoint, self.region]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    def key(self, filename: str) -> str:
        if not FILENAME_RE.fullmatch(filename):
            raise StorageError("公開ファイル名の形式が不正です。", "invalid_filename")
        return self.prefix + filename


def storage_mode() -> str:
    mode = os.getenv("KARRY_WEBSITE_STORAGE", "s3").strip().lower()
    if mode not in {"s3", "local"}:
        raise StorageError("KARRY_WEBSITE_STORAGEはs3またはlocalを指定してください。", "storage_configuration")
    return mode


def load_storage_config() -> StorageConfig:
    bucket = os.getenv("KARRY_WEBSITE_S3_BUCKET", "").strip()
    endpoint = os.getenv("KARRY_WEBSITE_S3_ENDPOINT", "").strip() or None
    prefix = os.getenv("KARRY_WEBSITE_S3_PREFIX", "karry-websites/").strip().rstrip("/") + "/"
    access = os.getenv("KARRY_WEBSITE_S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID") or ""
    secret = os.getenv("KARRY_WEBSITE_S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY") or ""
    session = os.getenv("KARRY_WEBSITE_S3_SESSION_TOKEN") or os.getenv("AWS_SESSION_TOKEN") or None
    region = os.getenv("KARRY_WEBSITE_S3_REGION", "auto" if endpoint and "r2.cloudflarestorage.com" in endpoint else "us-east-1").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or ".." in bucket:
        raise StorageError("永続保存用バケットをKARRY_WEBSITE_S3_BUCKETへ設定してください。", "storage_configuration")
    if not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)+", prefix) or len(prefix) > 150:
        raise StorageError("ストレージのprefix設定が不正です。", "storage_configuration")
    if not access or not secret or any(c in access + secret + (session or "") for c in "\r\n"):
        raise StorageError("永続保存用のS3アクセスキーをサーバーの秘密設定へ登録してください。", "storage_configuration")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", region):
        raise StorageError("S3リージョン設定が不正です。", "storage_configuration")
    if endpoint:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        trusted = re.fullmatch(r"[a-f0-9]{32}(?:\.(?:eu|us))?\.r2\.cloudflarestorage\.com", host) or re.fullmatch(
            r"s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?", host)
        try:
            valid_port = parsed.port in {None, 443}
        except ValueError:
            valid_port = False
        if not trusted or parsed.scheme != "https" or not valid_port or parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise StorageError("保存先endpointはHTTPSのR2またはAWS S3エンドポイントを指定してください。", "storage_configuration")
    return StorageConfig(bucket, prefix, endpoint, region, access, secret, session)


@lru_cache(maxsize=4)
def _client(config: StorageConfig):
    return boto3.client("s3", endpoint_url=config.endpoint, region_name=config.region,
        aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
        aws_session_token=config.session_token,
        config=Config(signature_version="s3v4", connect_timeout=3, read_timeout=10,
                      retries={"mode": "standard", "total_max_attempts": 2},
                      s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"))


def _translate(exc: Exception) -> StorageError:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"NoSuchKey", "NotFound", "404"}:
            return StorageError("公開成果物が見つかりません。", "not_found")
        if code in {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}:
            return StorageError("同じ公開キーが既に存在するか更新が競合しました。上書きしません。", "already_exists")
    # Never return provider exception text: it may contain signing/request details.
    return StorageError("永続ストレージへの接続・保存・読み取りに失敗しました。", "storage_unavailable",
                        diagnostic_code=connection_diagnostic(exc))


class S3WebsiteStore:
    def __init__(self, config: StorageConfig, client: Any = None):
        self.config = config
        self.client = client if client is not None else _client(config)

    def put(self, filename: str, body: bytes, *, max_files: int = 128, max_total_bytes: int = 32 * 1024 * 1024) -> str:
        key = self.config.key(filename)
        if not body or len(body) > MAX_HTML_BYTES:
            raise StorageError("HTML容量は512 KiB以内にしてください。", "invalid_content")
        sha = hashlib.sha256(body).hexdigest()
        try:
            # One bounded listing is sufficient for the deliberately small store.
            listing = self.client.list_objects_v2(Bucket=self.config.bucket, Prefix=self.config.prefix, MaxKeys=max_files + 1)
            entries = listing.get("Contents", [])
            if listing.get("IsTruncated") or len(entries) >= max_files or sum(int(e.get("Size", 0)) for e in entries) + len(body) > max_total_bytes:
                raise StorageError("永続公開領域の容量上限に達しています。", "storage_capacity")
            self.client.put_object(Bucket=self.config.bucket, Key=key, Body=body,
                ContentType="text/html; charset=utf-8", CacheControl="no-store", IfNoneMatch="*",
                ContentMD5=base64.b64encode(hashlib.md5(body).digest()).decode(), Metadata={"sha256": sha})
            head = self.client.head_object(Bucket=self.config.bucket, Key=key)
            if head.get("ContentLength") != len(body) or head.get("Metadata", {}).get("sha256") != sha or not str(head.get("ContentType", "")).startswith("text/html"):
                raise StorageError("永続ストレージへの保存検証に失敗しました。成功として返しません。", "storage_integrity")
            return sha
        except StorageError:
            raise
        except (BotoCoreError, ClientError, OSError) as exc:
            raise _translate(exc) from None

    def get(self, filename: str) -> bytes:
        key = self.config.key(filename)
        response = None
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            length = response.get("ContentLength", -1)
            if not isinstance(length, int) or not 0 < length <= MAX_HTML_BYTES or str(response.get("ContentType", "")).split(";", 1)[0].strip() != "text/html":
                raise StorageError("保存済み成果物の容量・形式が不正です。", "storage_integrity")
            body = response["Body"].read(MAX_HTML_BYTES + 1)
            if len(body) != length or hashlib.sha256(body).hexdigest() != response.get("Metadata", {}).get("sha256"):
                raise StorageError("保存済み成果物の内容検証に失敗しました。", "storage_integrity")
            body.decode("utf-8")
            return body
        except StorageError:
            raise
        except (BotoCoreError, ClientError, OSError, UnicodeError) as exc:
            raise _translate(exc) from None
        finally:
            if response is not None and response.get("Body") is not None:
                response["Body"].close()


def configured_store() -> S3WebsiteStore:
    return S3WebsiteStore(load_storage_config())


def storage_identity() -> str:
    return "s3:" + load_storage_config().identity if storage_mode() == "s3" else "local"

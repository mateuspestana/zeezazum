from __future__ import annotations

from zeezazum.storage.base import Storage

try:
    import boto3
except ImportError:  # extra "s3" não instalado
    boto3 = None


class S3Storage(Storage):
    def __init__(self, bucket: str, prefix: str = "", region: str = "us-east-1"):
        if boto3 is None:
            raise RuntimeError(
                "boto3 não está instalado. Instale com `uv pip install -e '.[s3]'` "
                "para usar storage.backend: s3."
            )
        if not bucket:
            raise ValueError("storage.s3.bucket precisa estar configurado.")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3", region_name=region)

    def _key(self, relative_path: str) -> str:
        relative_path = relative_path.lstrip("/")
        return f"{self.prefix}/{relative_path}" if self.prefix else relative_path

    def write_bytes(self, relative_path: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._key(relative_path), Body=data)

    def read_bytes(self, relative_path: str) -> bytes:
        obj = self._client.get_object(Bucket=self.bucket, Key=self._key(relative_path))
        return obj["Body"].read()

    def exists(self, relative_path: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(relative_path))
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise

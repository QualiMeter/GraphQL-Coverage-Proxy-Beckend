import hashlib
import json


def operation_key(query_hash: str, operation_name: str | None, operation_type: str) -> str:
    # Preserve the original algorithm, including ASCII escaping and null names.
    identity = json.dumps(
        [query_hash, operation_name, operation_type], ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()

"""Validate the complete JSON document on the same checked file descriptor."""
import hashlib
import io
import json
import os
from pathlib import Path

from genesisai.core.capabilities.contracts import schema_validator
from genesisai.core.capabilities.files import reject_link
from genesisai.shared.security import ToolError


def checked_parents(path):
    path = Path(path).absolute()
    identities = []
    for part in (path, *path.parents):
        reject_link(part)
        stat = part.stat()
        identities.append((str(part), stat.st_dev, stat.st_ino))
    return identities


def validate_result(path, schema, maximum):
    chain = checked_parents(path)
    with Path(path).open('rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > maximum:
            raise ToolError('result_limit', '结果超过配置的资产导入字节配额')
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise ToolError('result_limit', '结果增长超过配置的资产导入字节配额')
            digest.update(chunk)
        identity = {'sha256': digest.hexdigest(), 'size': size}
        stream.seek(0)
        def constant(value):
            raise ValueError('不接受非有限 JSON 数值：' + value)
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError('JSON 对象不接受重复键：' + key)
                result[key] = value
            return result
        reader = io.TextIOWrapper(stream, encoding='utf-8')
        try:
            value = json.load(reader, parse_constant=constant, object_pairs_hook=pairs)
        finally:
            reader.detach()
        json.dumps(value, allow_nan=False)  # Also rejects numeric exponent overflow to infinity.
        error = next(schema_validator(schema).iter_errors(value), None)
        if error:
            raise ToolError('invalid_result', f'输出不满足约定：{error.json_path}: {error.message}')
        stream.seek(0)
        after_hash = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            after_hash.update(chunk)
        after = os.fstat(stream.fileno())
        if (after_hash.hexdigest() != identity['sha256'] or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns or checked_parents(path) != chain):
            raise ToolError('source_changed', 'JSON 校验期间文件或父目录发生变化')
    return value, identity

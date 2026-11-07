"""Validate the current typed session boundary."""

import re
from genesisai.shared.messages import validate_content, image_parts


def validate_record(data, expected_id=None):
    """验证会话持久化数据的最小稳定结构。"""
    if not isinstance(data, dict):
        raise ValueError("会话 JSON 根节点必须是对象")
    field_types = {
        "id": str,
        "workspace": str,
        "status": str,
        "messages": list,
        "calls": dict,
        "job_receipts": dict,
        "authorizations": list,
        "sources": dict,
        "artifacts": dict,
        "pending": list,
        "grants": list,
        "output": str,
        "usage": dict,
        "history": list,
        "tool_runtime": dict,
        "run_runtime": dict,
        "changes": list,
    }
    missing = [name for name in field_types if name not in data]
    missing.extend(
        name
        for name in ("run_id", "rounds", "tool_count", "failures", "deadline")
        if name not in data
    )
    if missing:
        raise ValueError("会话缺少字段：" + ", ".join(sorted(set(missing))))
    for name, expected in field_types.items():
        if not isinstance(data[name], expected):
            raise ValueError(f"会话字段 {name} 类型无效")
    if expected_id is not None and data["id"] != expected_id:
        raise ValueError("会话 ID 与文件名不一致")
    from genesisai.core.tools.authorization import validate_records
    validate_records(data['authorizations'], data['id'])
    if any(not re.fullmatch(r'job_[a-zA-Z0-9_-]+', key) or not isinstance(value, str) or not value
           for key, value in data['job_receipts'].items()):
        raise ValueError('作业收取记录无效')
    if not data["workspace"]:
        raise ValueError("会话字段 workspace 无效")
    for ref, source in data["sources"].items():
        if source.get("kind") == "image":
            if not re.fullmatch(r"[a-f0-9]{64}", source.get("sha256", "")):
                raise ValueError("图片快照哈希无效")
            if (not re.fullmatch(r'[a-f0-9]{64}', source.get('original_sha256', ''))
                    or source.get('original_mime_type') not in {'image/png', 'image/jpeg', 'image/webp'}
                    or any(type(source.get(key)) is not int or source[key] < 1 for key in
                           ('original_width', 'original_height', 'original_byte_size', 'max_side'))):
                raise ValueError('图片原图快照元数据无效；需要重新导入原图')
            region = source.get('region')
            if region is not None and (not isinstance(region, list) or len(region) != 4
                    or any(type(v) is not int for v in region) or min(region[:2]) < 0 or min(region[2:]) < 1
                    or region[0] + region[2] > source['original_width'] or region[1] + region[3] > source['original_height']):
                raise ValueError('图片裁剪范围无效')
            validate_content(
                [
                    {
                        "type": "image",
                        "image_ref": ref,
                        **{
                            k: source.get(k)
                            for k in (
                                "name",
                                "mime_type",
                                "width",
                                "height",
                                "byte_size",
                            )
                        },
                    }
                ]
            )

    def validate_messages(messages):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {
                "user",
                "assistant",
                "system",
                "tool",
            }:
                raise ValueError("会话消息结构无效")
            validate_content(message.get("content"))
            projection = (message.get('metadata') or {}).get('image_projection', [])
            if not isinstance(projection, list):
                raise ValueError('图片投影槽位无效')
            indices = set()
            for slot in projection:
                if (not isinstance(slot, dict) or set(slot) != {'index', 'image_ref'}
                        or type(slot['index']) is not int or not isinstance(message.get('content'), list)
                        or not 0 <= slot['index'] < len(message['content']) or slot['index'] in indices
                        or not isinstance(slot['image_ref'], str)
                        or data['sources'].get(slot['image_ref'], {}).get('kind') != 'image'
                        or message['content'][slot['index']].get('type') != 'text'):
                    raise ValueError('图片投影槽位或引用无效')
                indices.add(slot['index'])
            if message.get("reasoning") is not None and (message["role"] != "assistant" or not isinstance(message["reasoning"], str)):
                raise ValueError("模型推理协议字段无效")
            for part in image_parts(message.get("content")):
                source = data["sources"].get(part["image_ref"], {})
                if (
                    message["role"] not in {"user", "tool"}
                    or source.get("kind") != "image"
                    or any(
                        source.get(k) != part[k]
                        for k in ("name", "mime_type", "width", "height", "byte_size")
                    )
                ):
                    raise ValueError("会话图片引用无效或元数据不一致")

    validate_messages(data["messages"])
    checkpoint = data.get("context_checkpoint")
    if checkpoint is not None:
        if (
            not isinstance(checkpoint, dict)
            or type(checkpoint.get("source_count")) is not int
            or checkpoint["source_count"] < 0
            or not isinstance(checkpoint.get("source_hash"), str)
            or not isinstance(checkpoint.get("messages"), list)
        ):
            raise ValueError("会话 context_checkpoint 无效")
        validate_messages(checkpoint["messages"])
    factor = data.get("context_estimate_factor", 1.0)
    if type(factor) not in (int, float) or not 1 <= factor <= 4:
        raise ValueError("会话 context_estimate_factor 无效")
    if data.get("permission_mode", "user") not in ("user", "auto"):
        raise ValueError("会话字段 permission_mode 无效")
    if data["run_id"] is not None and not isinstance(data["run_id"], str):
        raise ValueError("会话字段 run_id 类型无效")
    for name in ("rounds", "tool_count", "failures"):
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"会话字段 {name} 类型无效")
    deadline = data["deadline"]
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or deadline < 0
    ):
        raise ValueError("会话字段 deadline 类型无效")
    tool_runtime = data["tool_runtime"]
    if set(tool_runtime) != {"active_skills"} or not isinstance(tool_runtime['active_skills'], dict):
        raise ValueError("会话字段 tool_runtime 无效")
    if any(not isinstance(name, str) or not name or not isinstance(stage, str) or not stage
           for name, stage in tool_runtime['active_skills'].items()):
        raise ValueError("会话字段 active_skills 无效")
    run_runtime = data["run_runtime"]
    required_runtime = {
        "profile",
        "phase",
        "budget",
        "usage",
        "candidates",
        "candidate_count",
        "fetched_count",
        "content_hashes",
        "evidence",
        "evidence_refs",
        "failed_resources",
        "recovery_counts",
        "counted_calls",
        "completed_queries",
        "stop_reason",
        "partial_response",
        "last_tool_name",
        "last_query_relevant",
        "reuse_evidence",
        "allow_new_network",
        "prompt_names",
        "prompt_hashes",
        "started_at",
        "lifecycle",
        "state_history",
        "protocols",
        "context_summary",
        "context_report",
    }
    missing_runtime = required_runtime - set(run_runtime)
    if missing_runtime:
        raise ValueError(
            "会话字段 run_runtime 缺少：" + ", ".join(sorted(missing_runtime))
        )
    if run_runtime["profile"] not in {
        "direct_answer",
        "web_quick",
        "web_normal",
        "web_deep",
        "local_files",
        "creative",
    }:
        raise ValueError("会话字段 run_runtime.profile 无效")
    if run_runtime["phase"] not in {"explore", "answer", "recover", "done"}:
        raise ValueError("会话字段 run_runtime.phase 无效")
    from genesisai.shared.run_states import RUN_STATES

    if run_runtime["lifecycle"] not in RUN_STATES:
        raise ValueError("会话字段 run_runtime.lifecycle 无效")
    for name in (
        "budget",
        "usage",
        "candidates",
        "evidence",
        "failed_resources",
        "recovery_counts",
        "prompt_hashes",
        "context_report",
    ):
        if not isinstance(run_runtime[name], dict):
            raise ValueError(f"会话字段 run_runtime.{name} 类型无效")
    for name in (
        "content_hashes",
        "evidence_refs",
        "counted_calls",
        "completed_queries",
        "prompt_names",
        "state_history",
        "protocols",
    ):
        if not isinstance(run_runtime[name], list):
            raise ValueError(f"会话字段 run_runtime.{name} 类型无效")
    for name in ("candidate_count", "fetched_count"):
        value = run_runtime[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"会话字段 run_runtime.{name} 类型无效")
    if run_runtime["stop_reason"] is not None and not isinstance(
        run_runtime["stop_reason"], str
    ):
        raise ValueError("会话字段 run_runtime.stop_reason 类型无效")
    if not isinstance(run_runtime["partial_response"], str):
        raise ValueError("会话字段 run_runtime.partial_response 类型无效")
    if run_runtime["last_tool_name"] is not None and not isinstance(
        run_runtime["last_tool_name"], str
    ):
        raise ValueError("会话字段 run_runtime.last_tool_name 类型无效")
    if not isinstance(run_runtime["last_query_relevant"], bool):
        raise ValueError("会话字段 run_runtime.last_query_relevant 类型无效")
    for name in ("reuse_evidence", "allow_new_network"):
        if not isinstance(run_runtime[name], bool):
            raise ValueError(f"会话字段 run_runtime.{name} 类型无效")
    if run_runtime["context_summary"] is not None and not isinstance(
        run_runtime["context_summary"], dict
    ):
        raise ValueError("会话字段 run_runtime.context_summary 无效")
    for name in ("capability_routes", "discovered_tools", "pending_verification_jobs"):
        if name in run_runtime and (not isinstance(run_runtime[name], list) or any(not isinstance(item, str) for item in run_runtime[name])):
            raise ValueError(f"会话字段 run_runtime.{name} 无效")
    if "constraints" in run_runtime:
        constraints = run_runtime["constraints"]
        if (not isinstance(constraints, dict) or set(constraints) - {"read_only", "network_forbidden"}
                or any(type(value) is not bool for value in constraints.values())):
            raise ValueError("会话字段 run_runtime.constraints 无效")
    for key in ("model_calls", "tools", "total_tokens"):
        value = run_runtime["budget"][key]
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("无效的显式任务预算: " + key)

    if "progress" in run_runtime:
        progress = run_runtime["progress"]
        if (not isinstance(progress, dict) or set(progress) != {"epoch", "observed", "attempts"}
                or type(progress["epoch"]) is not int or progress["epoch"] < 0):
            raise ValueError("会话进展检查点无效")
        for field in ("observed", "attempts"):
            values = progress[field]
            if not isinstance(values, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[a-f0-9]{64}", k)
                   or type(v) is not int or v < 1 for k, v in values.items()):
                raise ValueError("会话动作指纹无效")
    limits = data.get("limits", {})
    if limits and (not isinstance(limits, dict) or set(limits) != {"rounds", "tools", "total_tokens"}
                   or any(v is not None and (type(v) is not int or v < 1) for v in limits.values())):
        raise ValueError("会话显式预算无效")

"""运行日志系统：完整的 core 层实现，包括配置、存储、查询和分析。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class LogLevel(Enum):
    """日志级别枚举。"""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogCategory(Enum):
    """日志分类枚举。"""
    MODEL_INPUT = "model_input"
    MODEL_OUTPUT = "model_output"
    TOOL_EXECUTION = "tool_execution"
    RUN_SUMMARY = "run_summary"
    SYSTEM = "system"


@dataclass
class LogRecord:
    """日志记录数据结构。"""
    timestamp: str
    level: str
    category: str
    round_num: int | None
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        """转换为字典格式。"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> LogRecord:
        """从字典创建日志记录。"""
        return cls(**data)


class LogConfig:
    """日志配置管理器。"""
    
    def __init__(
        self,
        log_dir: Path,
        level: LogLevel = LogLevel.INFO,
        max_file_size_mb: int = 10,
        backup_count: int = 5,
        enable_console: bool = False,
        console_level: LogLevel = LogLevel.WARNING,
    ):
        """初始化日志配置。
        
        Args:
            log_dir: 日志目录路径
            level: 日志级别
            max_file_size_mb: 单个日志文件最大大小（MB）
            backup_count: 保留的日志文件数量
            enable_console: 是否启用控制台输出
            console_level: 控制台日志级别
        """
        self.log_dir = log_dir
        self.level = level
        self.max_file_size_mb = max_file_size_mb
        self.backup_count = backup_count
        self.enable_console = enable_console
        self.console_level = console_level
        
        # 确保日志目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)
    
    def get_log_file_path(self, session_id: str) -> Path:
        """获取日志文件路径。
        
        Args:
            session_id: 会话 ID
            
        Returns:
            日志文件路径
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return self.log_dir / f'run_{session_id}_{timestamp}.log'
    
    def get_metadata_file_path(self, session_id: str) -> Path:
        """获取日志元数据文件路径。
        
        Args:
            session_id: 会话 ID
            
        Returns:
            元数据文件路径
        """
        return self.log_dir / f'run_{session_id}_metadata.json'


class LogStore:
    """日志存储和查询管理器。"""
    
    def __init__(self, config: LogConfig):
        """初始化日志存储。
        
        Args:
            config: 日志配置
        """
        self.config = config
        self.records: list[LogRecord] = []
        self._current_file: Path | None = None
        self._file_handler: logging.FileHandler | None = None
    
    def add_record(self, record: LogRecord) -> None:
        """添加日志记录。
        
        Args:
            record: 日志记录
        """
        self.records.append(record)
    
    def get_records(
        self,
        category: LogCategory | None = None,
        round_num: int | None = None,
        level: LogLevel | None = None,
    ) -> list[LogRecord]:
        """查询日志记录。
        
        Args:
            category: 日志分类
            round_num: 轮次号
            level: 日志级别
            
        Returns:
            匹配的日志记录列表
        """
        results = self.records
        
        if category is not None:
            results = [r for r in results if r.category == category.value]
        
        if round_num is not None:
            results = [r for r in results if r.round_num == round_num]
        
        if level is not None:
            results = [r for r in results if r.level == level.value]
        
        return results
    
    def get_tool_executions(self) -> list[LogRecord]:
        """获取所有工具执行记录。"""
        return self.get_records(category=LogCategory.TOOL_EXECUTION)
    
    def get_model_interactions(self) -> list[LogRecord]:
        """获取所有模型交互记录。"""
        return self.get_records(category=LogCategory.MODEL_INPUT) + \
               self.get_records(category=LogCategory.MODEL_OUTPUT)
    
    def get_round_summary(self, round_num: int) -> list[LogRecord]:
        """获取指定轮次的完整日志。"""
        return self.get_records(round_num=round_num)
    
    def export_to_json(self, output_path: Path) -> None:
        """导出日志为 JSON 格式。
        
        Args:
            output_path: 输出文件路径
        """
        data = [r.to_dict() for r in self.records]
        output_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )
    
    def get_statistics(self) -> dict[str, Any]:
        """获取日志统计信息。"""
        stats = {
            'total_records': len(self.records),
            'by_category': {},
            'by_level': {},
            'by_round': {},
        }
        
        for record in self.records:
            # 按分类统计
            stats['by_category'][record.category] = \
                stats['by_category'].get(record.category, 0) + 1
            
            # 按级别统计
            stats['by_level'][record.level] = \
                stats['by_level'].get(record.level, 0) + 1
            
            # 按轮次统计
            if record.round_num is not None:
                round_key = f'round_{record.round_num}'
                stats['by_round'][round_key] = \
                    stats['by_round'].get(round_key, 0) + 1
        
        return stats


class LogAnalyzer:
    """日志分析器。"""
    
    def __init__(self, store: LogStore):
        """初始化日志分析器。
        
        Args:
            store: 日志存储
        """
        self.store = store
    
    def analyze_tool_performance(self) -> dict[str, Any]:
        """分析工具执行性能。
        
        Returns:
            工具性能分析结果
        """
        executions = self.store.get_tool_executions()
        
        tool_stats = {}
        for record in executions:
            tool_name = record.metadata.get('tool_name', 'unknown')
            elapsed_ms = record.metadata.get('elapsed_ms', 0)
            success = record.metadata.get('success', False)
            
            if tool_name not in tool_stats:
                tool_stats[tool_name] = {
                    'count': 0,
                    'total_elapsed_ms': 0,
                    'success_count': 0,
                    'fail_count': 0,
                }
            
            tool_stats[tool_name]['count'] += 1
            tool_stats[tool_name]['total_elapsed_ms'] += elapsed_ms
            
            if success:
                tool_stats[tool_name]['success_count'] += 1
            else:
                tool_stats[tool_name]['fail_count'] += 1
        
        # 计算平均值
        for tool_name, stats in tool_stats.items():
            if stats['count'] > 0:
                stats['avg_elapsed_ms'] = stats['total_elapsed_ms'] / stats['count']
            else:
                stats['avg_elapsed_ms'] = 0
        
        return tool_stats
    
    def analyze_model_usage(self) -> dict[str, Any]:
        """分析模型使用情况。
        
        Returns:
            模型使用分析结果
        """
        outputs = self.store.get_records(category=LogCategory.MODEL_OUTPUT)
        
        total_tokens = 0
        total_elapsed_ms = 0
        round_count = 0
        
        for record in outputs:
            usage = record.metadata.get('usage', {})
            total_tokens += usage.get('total_tokens', 0)
            total_elapsed_ms += record.metadata.get('elapsed_ms', 0)
            round_count += 1
        
        return {
            'total_rounds': round_count,
            'total_tokens': total_tokens,
            'total_elapsed_ms': total_elapsed_ms,
            'avg_tokens_per_round': total_tokens / round_count if round_count > 0 else 0,
            'avg_elapsed_ms_per_round': total_elapsed_ms / round_count if round_count > 0 else 0,
        }
    
    def generate_report(self) -> str:
        """生成日志分析报告。
        
        Returns:
            分析报告文本
        """
        stats = self.store.get_statistics()
        tool_perf = self.analyze_tool_performance()
        model_usage = self.analyze_model_usage()
        
        report_lines = [
            "=" * 80,
            "日志分析报告",
            "=" * 80,
            "",
            "## 总体统计",
            f"总记录数: {stats['total_records']}",
            "",
            "## 按分类统计",
        ]
        
        for category, count in stats['by_category'].items():
            report_lines.append(f"  {category}: {count}")
        
        report_lines.extend([
            "",
            "## 按级别统计",
        ])
        
        for level, count in stats['by_level'].items():
            report_lines.append(f"  {level}: {count}")
        
        report_lines.extend([
            "",
            "## 模型使用分析",
            f"总轮次: {model_usage['total_rounds']}",
            f"总 Token: {model_usage['total_tokens']}",
            f"总耗时: {model_usage['total_elapsed_ms']}ms",
            f"平均每轮 Token: {model_usage['avg_tokens_per_round']:.0f}",
            f"平均每轮耗时: {model_usage['avg_elapsed_ms_per_round']:.0f}ms",
            "",
            "## 工具性能分析",
        ])
        
        for tool_name, perf in tool_perf.items():
            report_lines.extend([
                f"  {tool_name}:",
                f"    调用次数: {perf['count']}",
                f"    成功次数: {perf['success_count']}",
                f"    失败次数: {perf['fail_count']}",
                f"    平均耗时: {perf['avg_elapsed_ms']:.0f}ms",
            ])
        
        report_lines.append("=" * 80)
        
        return "\n".join(report_lines)


class RunLogger:
    """Agent 运行日志记录器，整合配置、存储和分析功能。"""
    
    def __init__(self, store, session_id: str = "unknown"):
        """初始化日志记录器。
        
        Args:
            store: 会话存储，用于获取日志目录和运行信息
            session_id: 会话 ID
        """
        self.store = store
        self.session_id = session_id
        
        # 创建日志配置
        log_dir = store.root / 'logs'
        self.config = LogConfig(log_dir=log_dir)
        
        # 创建日志存储
        self.log_store = LogStore(self.config)
        
        # 创建日志分析器
        self.analyzer = LogAnalyzer(self.log_store)
        
        # 创建 Python logger
        self.logger = logging.getLogger(f'genesisai.run.{session_id}')
        self._setup_logger()
    
    def _setup_logger(self):
        """配置 Python logger。"""
        if self.logger.handlers:
            return
        
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        
        # 文件处理器
        log_file = self.config.get_log_file_path(self.session_id)
        self._current_file = log_file
        
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)
        
        # 控制台处理器（可选）
        if self.config.enable_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(self.config.console_level.value)
            console_formatter = logging.Formatter('%(levelname)s: %(message)s')
            console_handler.setFormatter(console_formatter)
            self.logger.addHandler(console_handler)
    
    def _add_record(self, category: LogCategory, round_num: int | None, message: str, metadata: dict | None = None):
        """添加日志记录到存储。
        
        Args:
            category: 日志分类
            round_num: 轮次号
            message: 日志消息
            metadata: 元数据
        """
        record = LogRecord(
            timestamp=datetime.now().isoformat(),
            level=LogLevel.INFO.value,
            category=category.value,
            round_num=round_num,
            message=message,
            metadata=metadata or {},
        )
        self.log_store.add_record(record)
    
    def log_model_input(self, round_num: int, messages: list, tools: list[dict] | None):
        """记录模型接收的输入信息。
        
        Args:
            round_num: 当前轮次
            messages: 发送给模型的消息列表（Message 对象或字典）
            tools: 提供给模型的工具定义
        """
        self.logger.info(f"{'='*80}")
        self.logger.info(f"[轮次 {round_num}] 模型输入")
        self.logger.info(f"{'='*80}")
        
        # 记录消息数量
        self.logger.info(f"消息数量: {len(messages)}")
        
        # 记录每条消息的摘要
        for i, msg in enumerate(messages, 1):
            # 支持 Message 对象和字典两种格式
            if hasattr(msg, 'role'):
                role = msg.role
                content = msg.content or ''
                tool_calls = msg.tool_calls
                tool_call_id = msg.tool_call_id
            else:
                role = msg.get('role', 'unknown')
                content = msg.get('content', '')
                tool_calls = msg.get('tool_calls')
                tool_call_id = msg.get('tool_call_id')
            
            # 截断过长的内容
            content_preview = content[:200] + '...' if len(content) > 200 else content
            content_preview = content_preview.replace('\n', ' ').strip()
            
            self.logger.info(f"  消息 {i} [{role}]: {content_preview}")
            
            # 记录工具调用信息
            if tool_calls:
                self.logger.info(f"    包含 {len(tool_calls)} 个工具调用")
                for tc in tool_calls:
                    if hasattr(tc, 'name'):
                        self.logger.info(f"      - {tc.name}(id={tc.id})")
                    else:
                        self.logger.info(f"      - {tc.get('name', 'unknown')}(id={tc.get('id', 'N/A')})")
            
            # 记录工具响应
            if role == 'tool':
                self.logger.info(f"    工具响应 ID: {tool_call_id or 'N/A'}")
        
        # 记录工具定义数量
        if tools is not None:
            self.logger.info(f"工具定义数量: {len(tools)}")
            if tools:
                tool_names = [t.get('function', {}).get('name', 'unknown') for t in tools[:5]]
                self.logger.info(f"  工具列表（前5个）: {', '.join(tool_names)}")
        
        # 添加到日志存储
        self._add_record(
            LogCategory.MODEL_INPUT,
            round_num,
            f"模型输入 - {len(messages)} 条消息",
            {'message_count': len(messages), 'tool_count': len(tools) if tools else 0}
        )
    
    def log_model_output(self, round_num: int, response, elapsed_ms: int):
        """记录模型的输出响应。
        
        Args:
            round_num: 当前轮次
            response: 模型返回的 Response 对象
            elapsed_ms: 模型调用耗时（毫秒）
        """
        self.logger.info(f"{'='*80}")
        self.logger.info(f"[轮次 {round_num}] 模型输出")
        self.logger.info(f"{'='*80}")
        
        # 基本信息
        self.logger.info(f"耗时: {elapsed_ms}ms")
        self.logger.info(f"完成原因: {response.finish_reason or 'N/A'}")
        
        # Token 使用情况
        usage = response.usage or {}
        if usage:
            self.logger.info(
                f"Token 使用: prompt={usage.get('prompt_tokens', 0)}, "
                f"completion={usage.get('completion_tokens', 0)}, "
                f"total={usage.get('total_tokens', 0)}"
            )
        
        # 模型内容
        content = response.content or ''
        if content:
            content_preview = content[:500] + '...' if len(content) > 500 else content
            self.logger.info(f"模型内容预览:\n{content_preview}")
            self.logger.info(f"内容长度: {len(content)} 字符")
        else:
            self.logger.info("模型内容: (空)")
        
        # 工具调用
        tool_calls = response.tool_calls or []
        if tool_calls:
            self.logger.info(f"工具调用数量: {len(tool_calls)}")
            for i, call in enumerate(tool_calls, 1):
                self.logger.info(f"  调用 {i}:")
                self.logger.info(f"    ID: {call.id}")
                self.logger.info(f"    名称: {call.name}")
                args = call.arguments or '{}'
                args_preview = args[:200] + '...' if len(args) > 200 else args
                self.logger.info(f"    参数: {args_preview}")
        else:
            self.logger.info("工具调用: 无")
        
        # 添加到日志存储
        self._add_record(
            LogCategory.MODEL_OUTPUT,
            round_num,
            f"模型输出 - {elapsed_ms}ms, {usage.get('total_tokens', 0)} tokens",
            {
                'elapsed_ms': elapsed_ms,
                'finish_reason': response.finish_reason,
                'usage': usage,
                'tool_call_count': len(tool_calls),
            }
        )
    
    def log_tool_execution_start(self, call_id: str, tool_name: str, arguments: str):
        """记录工具执行开始。
        
        Args:
            call_id: 调用 ID
            tool_name: 工具名称
            arguments: 工具参数（JSON 字符串）
        """
        self.logger.info(f"{'-'*80}")
        self.logger.info(f"[工具调用] 开始执行")
        self.logger.info(f"{'-'*80}")
        self.logger.info(f"调用 ID: {call_id}")
        self.logger.info(f"工具名称: {tool_name}")
        
        # 格式化参数
        try:
            args_dict = json.loads(arguments)
            args_formatted = json.dumps(args_dict, indent=2, ensure_ascii=False)
            self.logger.info(f"参数:\n{args_formatted}")
        except (json.JSONDecodeError, TypeError):
            self.logger.info(f"参数（原始）: {arguments}")
        
        # 添加到日志存储
        self._add_record(
            LogCategory.TOOL_EXECUTION,
            None,
            f"工具执行开始 - {tool_name}",
            {
                'call_id': call_id,
                'tool_name': tool_name,
                'phase': 'start',
            }
        )
    
    def log_tool_execution_end(self, call_id: str, tool_name: str, result: dict, elapsed_ms: int):
        """记录工具执行完成。
        
        Args:
            call_id: 调用 ID
            tool_name: 工具名称
            result: 工具返回结果
            elapsed_ms: 执行耗时（毫秒）
        """
        self.logger.info(f"{'-'*80}")
        self.logger.info(f"[工具调用] 执行完成")
        self.logger.info(f"{'-'*80}")
        self.logger.info(f"调用 ID: {call_id}")
        self.logger.info(f"工具名称: {tool_name}")
        self.logger.info(f"执行耗时: {elapsed_ms}ms")
        self.logger.info(f"执行结果: {'成功' if result.get('ok') else '失败'}")
        
        # 错误信息
        if result.get('error'):
            error = result['error']
            self.logger.info(f"错误代码: {error.get('code', 'N/A')}")
            self.logger.info(f"错误消息: {error.get('message', 'N/A')}")
        
        # 结果数据预览
        data = result.get('data')
        if data:
            data_str = json.dumps(data, ensure_ascii=False)
            data_preview = data_str[:500] + '...' if len(data_str) > 500 else data_str
            self.logger.info(f"结果数据预览: {data_preview}")
        
        # 引用信息
        source_refs = result.get('source_refs', [])
        artifact_refs = result.get('artifact_refs', [])
        if source_refs:
            self.logger.info(f"来源引用: {', '.join(source_refs)}")
        if artifact_refs:
            self.logger.info(f"产物引用: {', '.join(artifact_refs)}")
        
        # 添加到日志存储
        self._add_record(
            LogCategory.TOOL_EXECUTION,
            None,
            f"工具执行完成 - {tool_name} - {'成功' if result.get('ok') else '失败'}",
            {
                'call_id': call_id,
                'tool_name': tool_name,
                'elapsed_ms': elapsed_ms,
                'success': result.get('ok', False),
                'phase': 'end',
            }
        )
    
    def log_round_summary(self, round_num: int, status: str, answer: str | None = None):
        """记录轮次摘要。
        
        Args:
            round_num: 轮次
            status: 运行状态
            answer: 最终回答（如果有）
        """
        self.logger.info(f"{'='*80}")
        self.logger.info(f"[轮次 {round_num}] 运行摘要")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"最终状态: {status}")
        
        if answer:
            answer_preview = answer[:300] + '...' if len(answer) > 300 else answer
            self.logger.info(f"最终回答:\n{answer_preview}")
            self.logger.info(f"回答长度: {len(answer)} 字符")
        
        # 使用统计
        usage = self.store.data.get('usage', {})
        if usage:
            self.logger.info(
                f"累计 Token 使用: prompt={usage.get('prompt_tokens', 0)}, "
                f"completion={usage.get('completion_tokens', 0)}, "
                f"total={usage.get('total_tokens', 0)}"
            )
        
        self.logger.info(f"{'='*80}\n")
        
        # 添加到日志存储
        self._add_record(
            LogCategory.RUN_SUMMARY,
            round_num,
            f"运行摘要 - {status}",
            {
                'status': status,
                'answer_length': len(answer) if answer else 0,
                'usage': usage,
            }
        )
    
    def export_logs(self, output_path: Path | None = None):
        """导出日志。
        
        Args:
            output_path: 输出路径（可选，默认使用配置的路径）
        """
        if output_path is None:
            output_path = self.config.log_dir / f'run_{self.session_id}_export.json'
        
        self.log_store.export_to_json(output_path)
    
    def get_analysis_report(self) -> str:
        """获取分析报告。
        
        Returns:
            分析报告文本
        """
        return self.analyzer.generate_report()
    
    def get_statistics(self) -> dict:
        """获取日志统计信息。
        
        Returns:
            统计信息字典
        """
        return self.log_store.get_statistics()

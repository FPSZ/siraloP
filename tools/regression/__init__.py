"""回归测试工具包（ctfy-agent）。

本包只含一个可执行脚本 ``run.py``；它用非侵入的计数/计时包装器（Counting*）
把 agent 的各端口（PlatformPort / LLMPort / ExecutionPort / ClockPort）包起来，
在不改动 domain/ports 冻结契约的前提下采集《回归测试字段表》要求的字段，
产出一条 JSON 记录到 ``regression/runs/<run_id>.json``。

详见 docs/testing/回归测试使用说明.md。
"""

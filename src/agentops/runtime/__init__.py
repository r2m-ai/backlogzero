"""runtime 适配层：同一套流水线接不同的 agent CLI。阶段 B 只有一次性的结构化调用（judge / PM），
worker 会话的启动、结果解析与退回重做在阶段 C、D 接入。后端按仓库配置选：`[repos.<x>] runtime`。"""

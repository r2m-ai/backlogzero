class AgentopsError(Exception):
    """面向用户的错误。CLI 捕获后打印到 stderr 并以退出码 2 结束。"""

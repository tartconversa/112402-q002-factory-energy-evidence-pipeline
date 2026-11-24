"""异常类型。"""


class LedgerError(Exception):
    """可预期的业务错误，命令行以非零退出码报告。"""


class ValidationError(LedgerError):
    """输入数据不符合约定。"""


class ConflictError(LedgerError):
    """同一来源版本出现不同内容，或存在互相矛盾的映射。"""


class NotReadyError(LedgerError):
    """指标或报告尚未达到可发布条件。"""


class FetchInterrupted(LedgerError):
    """采集在分页途中中断，可用批次号续传。"""

    def __init__(self, message: str, batch_id: int):
        super().__init__(message)
        self.batch_id = batch_id


class IngestFailure(FetchInterrupted):
    """某一页写入失败；该页已回滚，批次保留可续传的页号。"""

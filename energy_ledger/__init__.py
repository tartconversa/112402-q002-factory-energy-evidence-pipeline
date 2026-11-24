"""工厂能耗证据采集、修订留痕与月度指标核定工具。

该包只依赖 Python 标准库，使用 SQLite 保存不可变的观测版本、人工确认、
发布快照和修订差异。命令行入口见 :mod:`energy_ledger.cli`。
"""

__all__ = ["Ledger", "LedgerError"]

__version__ = "1.0.0"


def __getattr__(name: str):
    # 延迟导入，避免 ``python -m energy_ledger`` 时出现循环导入。
    if name in {"Ledger", "LedgerError"}:
        from .ledger import Ledger, LedgerError

        return {"Ledger": Ledger, "LedgerError": LedgerError}[name]
    raise AttributeError(name)

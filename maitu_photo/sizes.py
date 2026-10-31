"""公开配置使用 KB，文件与协议边界使用整数 B。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

BYTES_PER_KB = 1024
MAX_REFERENCE_KB = 500


def kb_to_bytes(value: Any) -> int:
    """按 1 KB = 1024 B 向下取整，拒绝布尔、非有限数和不足一字节的值。"""

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("图片大小必须使用 KB 数值")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < Decimal(1) / BYTES_PER_KB:
        raise ValueError("图片大小必须为有限正数，且至少为 1 B（1/1024 KB）")
    return int(amount * BYTES_PER_KB)

"""适配器共用的网络环境工具（单一实现，2026-11-17 `reports/134` S5）。

为什么存在
----------
`_domestic_direct()`（临时清空 HTTP(S)_PROXY，让国内源直连、退出时恢复）此前在
**7 个适配器里各抄了一份**：cninfo_funding / eastmoney_repurchase / hk_dividend /
hk_price / index_valuation / ths / ah_hk_mapping。7 份代码逐字相同，但任何一处要改
（比如换成按请求设置 `trust_env=False`）都得改 7 遍，而且"哪一份才是权威"没人说得清
——这是本仓库最典型的"同一事实多处实现"（`reports/133` R1）。

已知风险（不要在本模块里"顺手加锁"）
----------------------------------
本实现**修改进程级** `os.environ`，因此在多线程服务里，一个线程进入本上下文时，
其它线程发出的请求也会失去代理（反之亦然）。历史上没有出过事故，是因为写库/抓取
都是串行子进程；但这是**设计缺陷，不是安全特性**。
正解是让每个适配器用 `httpx.Client(trust_env=False)` 走自己的直连客户端，然后删掉
本模块——见 `reports/134` §5 S5。在那之前，请保持"一个实现"，不要各自打补丁。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@contextlib.contextmanager
def domestic_direct() -> Iterator[None]:
    """临时禁用 HTTP(S) 代理（国内源直连），退出时恢复原值。

    注意：作用域是**整个进程**，不是当前请求（见模块 docstring 的已知风险）。
    """
    saved = {key: os.environ.get(key) for key in _PROXY_KEYS}
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

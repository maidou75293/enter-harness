"""版本号唯一来源。

`pyproject.toml` 用 `[tool.setuptools.dynamic] version = {attr = ...}` 从这里读，
`enter_harness.__version__` 也从这里导入，因此源码与发行元数据不会漂移。

发版时只改这一处。
"""

__version__ = "1.4.0"

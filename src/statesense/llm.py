"""远端 LLM 的 .env 装载。密钥只住在这里与 os.environ，绝不进 Config / 日志。

三条规则：

  · **查找顺序**：先 `CWD/.env`，再配置文件同目录/.env，先找到的文件整体生效。
    CWD 优先是因为常规用法（仓库根起进程）；配置目录兜底是任务计划程序那种
    工作目录不可信的场景。已存在的环境变量优先于文件——十二要素的老规矩，
    也让 CI / 临时覆盖不用改文件。
  · **fail closed**：`shadow.provider = "http"` 而配置缺失时，启动即报错并
    指名缺哪几项。「以为在收影子数据、实际一次都没调」比报错严重得多
    （与 gate 名拼错同一类缺陷）。
  · **密钥不出现在错误信息里**。报错只给变量名；URL 不是秘密（.env.example
    里就有），密钥是。

只支持 `.env` 的一个小子集：`KEY=VALUE`、整行 `#` 注释、可选的 `export `
前缀、成对的首尾引号。够用且可测，不值得为它引入 python-dotenv。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from statesense.config import ConfigError

#: 远端 LLM 的环境变量名。前缀把「本项目的 LLM 配置」与机器上其他 .env 变量隔开。
ENV_URL = "STATESENSE_LLM_URL"
ENV_MODEL = "STATESENSE_LLM_MODEL"
ENV_API_KEY = "STATESENSE_LLM_API_KEY"

_LLM_KEYS: tuple[str, ...] = (ENV_URL, ENV_MODEL, ENV_API_KEY)


def parse_env_text(text: str) -> dict[str, str]:
    """解析 `.env` 文本。只认 `KEY=VALUE`；解析不了的行跳过而不是报错——
    .env 里可能混着别的工具的变量与注释，为一个不认识的行炸掉启动不值。"""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def read_env_files(config_dir: Path) -> dict[str, str]:
    """按查找顺序读 `.env`，先找到的文件整体生效。返回文件里的键值
    （**不含** os.environ——优先级在调用方合成）。"""
    for path in (Path.cwd() / ".env", config_dir / ".env"):
        if path.is_file():
            return parse_env_text(path.read_text(encoding="utf-8"))
    return {}


@dataclass(frozen=True)
class LlmEnv:
    """远端 LLM 的三项连接配置。`api_key` 只存在这里，用完即弃。"""

    url: str
    model: str
    api_key: str


def load_llm_env(config_dir: Path) -> LlmEnv:
    """合成 os.environ 与 .env 文件，缺项即 `ConfigError`（fail closed）。

    报错只指名变量，绝不回显任何已取得的值——错误信息会进日志与运行事件。
    """
    from_file = read_env_files(config_dir)

    def get(name: str) -> str:
        return (os.environ.get(name) or from_file.get(name) or "").strip()

    url = get(ENV_URL)
    model = get(ENV_MODEL)
    api_key = get(ENV_API_KEY)

    missing = [name for name, value in zip(_LLM_KEYS, (url, model, api_key)) if not value]
    if missing:
        raise ConfigError(
            f"远端 LLM 配置缺失：{missing}。"
            "请把 .env.example 复制为 .env 并填入真实值"
            "（查找顺序：CWD/.env → 配置文件同目录/.env；环境变量优先）。"
        )
    if not url.startswith("https://"):
        # 白名单载荷虽然不含私密字段，但带着 API key 的请求头只许走 https。
        raise ConfigError(f"{ENV_URL} 必须是 https:// 开头（当前值不是）。")
    return LlmEnv(url=url, model=model, api_key=api_key)

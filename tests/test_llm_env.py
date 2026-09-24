"""`.env` 装载的测试：解析子集、查找顺序、优先级、fail closed。

安全红线在这里钉死：配置缺失的报错只指名变量，**绝不回显任何值**——
错误信息会进日志与运行事件，密钥漏进那里等于写进了库。
"""

from __future__ import annotations

import pytest

from statesense.config import ConfigError
from statesense.llm import load_llm_env, parse_env_text

_KEYS = ("STATESENSE_LLM_URL", "STATESENSE_LLM_MODEL", "STATESENSE_LLM_API_KEY")


@pytest.fixture(autouse=True)
def _isolate_llm_env(tmp_path, monkeypatch):
    """机器环境里不许有真实配置混进测试——三条键一律清空，CWD 一律切走。

    仓库根就有一份真实 `.env`（gitignored），而 `load_llm_env` 的查找顺序是
    CWD 优先：不切走的话，测试会读到开发机的真实端点与模型名（实测踩过：
    model 读回了 glm-5.3-flash 而不是桩值）。
    """
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def test_parse_env_text_handles_comments_quotes_and_export():
    text = (
        "# 整行注释\n"
        "\n"
        'export STATESENSE_LLM_URL="https://example.test/v1/chat/completions"\n'
        "STATESENSE_LLM_MODEL=glm-5.3-flash\n"
        "STATESENSE_LLM_API_KEY='sk-abc'\n"
        "SPACED =  前后空格要剥掉  \n"
        "这行没有等号\n"
    )
    assert parse_env_text(text) == {
        "STATESENSE_LLM_URL": "https://example.test/v1/chat/completions",
        "STATESENSE_LLM_MODEL": "glm-5.3-flash",
        "STATESENSE_LLM_API_KEY": "sk-abc",
        "SPACED": "前后空格要剥掉",
    }


def test_missing_entries_fail_closed_naming_keys_not_values(tmp_path, monkeypatch):
    """三处都没有配置 → 指名缺哪些键。报错里不许出现任何值。"""
    monkeypatch.chdir(tmp_path)  # CWD 无 .env
    with pytest.raises(ConfigError) as excinfo:
        load_llm_env(tmp_path)  # 配置目录也无 .env
    message = str(excinfo.value)
    for key in _KEYS:
        assert key in message
    assert "sk-" not in message
    assert "https://" not in message


def test_env_file_in_config_dir_is_found_when_cwd_has_none(tmp_path, monkeypatch):
    """CWD 没有 .env 时用配置目录那份——任务计划程序的工作目录不可信，
    这个兜底就是为它准备的。"""
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "STATESENSE_LLM_URL=https://from-config-dir.test/v1/chat/completions\n"
        "STATESENSE_LLM_MODEL=from-file\n"
        "STATESENSE_LLM_API_KEY=sk-from-file\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)  # CWD（tmp_path）下没有 .env

    env = load_llm_env(config_dir)

    assert env.url == "https://from-config-dir.test/v1/chat/completions"
    assert env.model == "from-file"
    assert env.api_key == "sk-from-file"


def test_cwd_env_file_wins_over_config_dir(tmp_path, monkeypatch):
    """两处都有时 CWD 优先——查找顺序必须确定，否则哪份生效全凭运气。"""
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "STATESENSE_LLM_URL=https://config-dir.test/v1\n"
        "STATESENSE_LLM_MODEL=m-conf\n"
        "STATESENSE_LLM_API_KEY=sk-conf\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "STATESENSE_LLM_URL=https://cwd.test/v1\n"
        "STATESENSE_LLM_MODEL=m-cwd\n"
        "STATESENSE_LLM_API_KEY=sk-cwd\n",
        encoding="utf-8",
    )

    env = load_llm_env(config_dir)

    assert env.url == "https://cwd.test/v1"
    assert env.model == "m-cwd"
    assert env.api_key == "sk-cwd"


def test_os_environ_overrides_the_file(tmp_path, monkeypatch):
    """已存在的环境变量优先于文件——CI 与临时覆盖不用改文件。"""
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "STATESENSE_LLM_URL=https://from-file.test/v1/chat/completions\n"
        "STATESENSE_LLM_MODEL=from-file\n"
        "STATESENSE_LLM_API_KEY=sk-from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STATESENSE_LLM_API_KEY", "sk-from-os")

    env = load_llm_env(config_dir)

    assert env.api_key == "sk-from-os"
    assert env.model == "from-file"  # 文件继续供没被环境变量覆盖的键


def test_plain_http_url_is_rejected(tmp_path, monkeypatch):
    """带 API key 的请求头只许走 https——载荷虽是白名单，鉴权头不是。"""
    monkeypatch.setenv("STATESENSE_LLM_URL", "http://insecure.test/v1/chat/completions")
    monkeypatch.setenv("STATESENSE_LLM_MODEL", "m")
    monkeypatch.setenv("STATESENSE_LLM_API_KEY", "sk-x")

    with pytest.raises(ConfigError, match="https"):
        load_llm_env(tmp_path)

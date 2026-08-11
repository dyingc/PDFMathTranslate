"""Smoke test for the three places this app is coupled to pdf2zh internals.

Run after syncing upstream:  .venv/bin/python -m webapp.smoke_test

It does not translate anything (no API key, no network). It only checks that
the contracts the app relies on still hold — an upstream refactor can leave the
rebase conflict-free and still break these silently.
"""

import inspect
import sys
from pathlib import Path

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("high_level.translate() 仍接受我们传入的全部参数")
def _translate_signature():
    from pdf2zh.high_level import translate
    params = inspect.signature(translate).parameters
    required = {"files", "output", "pages", "lang_in", "lang_out", "service",
                "thread", "callback", "model", "envs"}
    missing = required - set(params)
    assert not missing, f"缺少参数: {sorted(missing)}"


@check("DeepseekTranslator 的服务名与 envs 键名未变")
def _deepseek_contract():
    from pdf2zh.translator import DeepseekTranslator
    assert DeepseekTranslator.name == "deepseek", DeepseekTranslator.name
    assert set(DeepseekTranslator.envs) == {"DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"}, \
        DeepseekTranslator.envs
    src = inspect.getsource(DeepseekTranslator)
    assert "api.deepseek.com" in src, "base_url 不再指向 api.deepseek.com"


@check("输出文件名仍是 <stem>-mono.pdf / <stem>-dual.pdf")
def _output_naming():
    from pdf2zh import high_level
    src = inspect.getsource(high_level.translate)
    for suffix in ("-mono.pdf", "-dual.pdf"):
        assert suffix in src, f"找不到 {suffix} 的命名约定"


@check("API Key 不会被写入磁盘")
def _key_never_persisted():
    import webapp.app  # noqa: F401 - importing installs the no-save patch
    from pdf2zh.config import ConfigManager
    from pdf2zh.translator import DeepseekTranslator

    cfg = Path.home() / ".config" / "PDFMathTranslate" / "config.json"
    before = cfg.read_bytes() if cfg.exists() else b""
    secret = "sk-smoke-test-must-not-be-written"

    # This is the call path that used to persist the key.
    DeepseekTranslator("en", "zh", "deepseek-v4-flash",
                       envs={"DEEPSEEK_API_KEY": secret,
                             "DEEPSEEK_MODEL": "deepseek-v4-flash"})
    ConfigManager.set_translator_by_name("deepseek", {"DEEPSEEK_API_KEY": secret})

    after = cfg.read_bytes() if cfg.exists() else b""
    assert secret.encode() not in after, f"API Key 被写入了 {cfg}"
    assert before == after, f"{cfg} 被改动了"


@check("版面模型入口仍在")
def _layout_model():
    from pdf2zh.doclayout import ModelInstance, OnnxModel
    assert hasattr(ModelInstance, "value")
    assert hasattr(OnnxModel, "load_available")


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
        else:
            print(f"  ok    {name}")
    print()
    if failed:
        print(f"{failed}/{len(CHECKS)} 项失败——上游改动可能已经破坏本应用。")
    else:
        print(f"全部 {len(CHECKS)} 项通过。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

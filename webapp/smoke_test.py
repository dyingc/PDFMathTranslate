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


@check("界面翻译覆盖所有语言且无缺漏")
def _i18n_complete():
    import re
    from webapp.app import LANGUAGES, MODELS, OUTPUTS

    src = (Path(__file__).parent / "static" / "i18n.js").read_text()
    # Crude but dependency-free: one block per language, keys are bare idents.
    blocks = re.findall(r'"([\w-]+)":\s*\{(.*?)\n  \}', src, re.S)
    table = {lang: set(re.findall(r"^\s{4}(\w+):", body, re.M))
             for lang, body in blocks}

    missing_langs = set(LANGUAGES) - set(table)
    assert not missing_langs, f"缺少界面语言: {sorted(missing_langs)}"

    values = {lang: dict(re.findall(r'^\s{4}(\w+): "((?:[^"\\]|\\.)*)"', body, re.M))
              for lang, body in blocks}
    reference = table["zh"]
    # Guard against the regex silently matching nothing, which would make every
    # (empty) set compare equal and turn this check into a no-op.
    assert len(reference) > 20, f"只解析到 {len(reference)} 个键，i18n.js 结构可能已改变"
    for lang, keys in table.items():
        assert keys == reference, \
            f"{lang} 缺少 {sorted(reference - keys)}，多出 {sorted(keys - reference)}"

    # A translation that drops a placeholder silently loses information at
    # runtime, and key-presence alone would not catch it.
    for key, text in values["zh"].items():
        want = set(re.findall(r"\{(\w+)\}", text))
        for lang, texts in values.items():
            got = set(re.findall(r"\{(\w+)\}", texts.get(key, "")))
            assert got == want, f"{lang}.{key} 占位符不匹配：期望 {want or '无'}，实际 {got or '无'}"
        assert all(texts.get(key, "").strip() for texts in values.values()), \
            f"{key} 存在空翻译"

    # Keys the server hands to the client must exist on the client side.
    for key in list(OUTPUTS.values()) + [m["hint"] for m in MODELS.values()]:
        assert key in reference, f"i18n 缺少服务端引用的键: {key}"


@check("pdf2zh 仍会解析到我们带计量的 translator")
def _translator_installed():
    import webapp.app  # noqa: F401 - installs the subclass
    from pdf2zh import converter
    from webapp.translator import MeteredDeepseekTranslator

    assert converter.DeepseekTranslator is MeteredDeepseekTranslator
    assert MeteredDeepseekTranslator.name == "deepseek"
    # The class list in TranslateConverter.__init__ is what actually picks it.
    src = inspect.getsource(converter.TranslateConverter.__init__)
    assert "DeepseekTranslator" in src, "converter 不再按模块全局名解析 translator"


@check("价格表覆盖所有模型且档位有序")
def _pricing_table():
    from webapp.app import MODELS
    from webapp.pricing import TABLE

    assert TABLE.regimes, "价格表为空"
    froms = [r["_from"] for r in TABLE.regimes]
    assert froms == sorted(froms), "regimes 未按生效时间排序"
    for regime in TABLE.regimes:
        for model in MODELS:
            rates = regime["rates"].get(model)
            assert rates, f"{regime['effective_from']} 缺少 {model} 的价格"
            assert "off_peak" in rates, f"{model} 缺少 off_peak 价格"
            for period in rates.values():
                assert set(period) == {"cache_hit", "cache_miss", "output"}, period


@check("重影修复所依赖的 PyMuPDF 能力仍在")
def _deghost_capabilities():
    import pymupdf
    for const in ("PDF_REDACT_TEXT_REMOVE", "PDF_REDACT_TEXT_NONE",
                  "PDF_REDACT_IMAGE_PIXELS", "PDF_REDACT_IMAGE_NONE",
                  "PDF_REDACT_LINE_ART_NONE"):
        assert hasattr(pymupdf, const), f"PyMuPDF 缺少 {const}"
    sig = inspect.signature(pymupdf.Page.apply_redactions).parameters
    assert {"images", "graphics", "text"} <= set(sig), sorted(sig)
    # get_texttrace must still expose draw order and render mode; without those
    # we cannot tell a hidden layer from a visible one.
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "probe")
    span = page.get_texttrace()[0]
    for key in ("seqno", "type", "font", "size", "chars", "bbox"):
        assert key in span, f"get_texttrace 不再提供 {key}"
    doc.close()


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

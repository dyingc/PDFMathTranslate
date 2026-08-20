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


@check("VFONT 仍覆盖 pdf2zh 内置的公式字体规则")
def _vfont_superset():
    import re
    from pdf2zh import converter
    from webapp.app import VFONT

    src = inspect.getsource(converter.TranslateConverter.receive_layout)
    builtin = re.search(r'r"\((CM\[\^R\][^"]+)\)"', src)
    assert builtin, "找不到 pdf2zh 内置的公式字体正则，规则可能已改变"
    # Passing vfont switches the built-in rules off, so ours has to contain them
    # or formulas would quietly start being translated as prose.
    missing = [alt for alt in builtin.group(1).split("|") if alt not in VFONT]
    assert not missing, f"VFONT 缺少内置规则: {missing}"


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


@check("版面区域注入所依赖的 pdf2zh 约定仍成立")
def _verbatim_injection():
    from pdf2zh import high_level
    from pdf2zh.doclayout import YoloBox

    src = inspect.getsource(high_level.translate_patch)
    # One predict call per page, in page order — this is what lets us match a
    # page to its precomputed blocks by position.
    assert src.count("model.predict(") == 1, "translate_patch 调用 predict 的次数变了"
    assert "isolate_formula" in src, "vcls 里不再有 isolate_formula"
    assert "box[y0:y1, x0:x1] = 0" in src, "保留区域不再被painted 为 0"
    # YoloBox must still accept a flat [x0, y0, x1, y1, conf, cls] row.
    box = YoloBox(data=[1, 2, 3, 4, 0.9, 7])
    assert list(box.xyxy) == [1, 2, 3, 4] and box.cls == 7, "YoloBox 结构已改变"


@check("被区域切断的文本行会被修复为单一段落")
def _line_healing():
    import numpy as np
    import pymupdf
    from pdf2zh.doclayout import YoloBox
    from webapp.verbatim import heal_cuts

    class Result:
        names = {0: "plain text", 1: "figure"}

        def __init__(self, boxes):
            self.boxes = boxes

    line = pymupdf.Rect(161, 261, 484, 271)
    # The real case from SPA.pdf: a 0.47-confidence region covering the middle
    # of a line, which split it into "More ex" / ... / "n of TIP.".
    cutter = YoloBox(data=np.array([195.0, 257.0, 442.0, 270.0, 0.47, 0.0]))
    result = Result([cutter])
    assert heal_cuts(result, [line]) == 1
    x0, _, x1, _ = [float(v) for v in np.array(result.boxes[0].xyxy).squeeze()]
    assert x0 <= line.x0 and x1 >= line.x1, (x0, x1)

    # A region that already holds the whole line is left alone, as is one that
    # merely grazes it, and preserved regions are never widened.
    for box in (YoloBox(data=np.array([150.0, 255.0, 500.0, 275.0, 0.9, 0.0])),
                YoloBox(data=np.array([195.0, 269.5, 442.0, 300.0, 0.9, 0.0])),
                YoloBox(data=np.array([195.0, 257.0, 442.0, 270.0, 0.9, 1.0]))):
        assert heal_cuts(Result([box]), [line]) == 0, box.xyxy

    # A neighbouring column must stay out of reach. This is what makes widening
    # safe: a line never spans two columns, so a region sitting in one of them
    # can only ever grow to that column's width.
    left = pymupdf.Rect(60, 261, 280, 271)
    right = pymupdf.Rect(320, 261, 540, 271)
    narrow = YoloBox(data=np.array([100.0, 257.0, 200.0, 270.0, 0.5, 0.0]))
    result = Result([narrow])
    heal_cuts(result, [left, right])
    x0, _, x1, _ = [float(v) for v in np.array(result.boxes[0].xyxy).squeeze()]
    assert (x0, x1) == (left.x0, left.x1), f"expected {left.x0}-{left.x1}, got {x0}-{x1}"


@check("链接重建所依赖的 PyMuPDF 能力仍在")
def _link_capabilities():
    import pymupdf
    for const in ("LINK_GOTO", "LINK_NAMED", "LINK_URI"):
        assert hasattr(pymupdf, const), f"PyMuPDF 缺少 {const}"
    for method in ("get_links", "insert_link", "delete_link", "search_for",
                   "get_textbox"):
        assert hasattr(pymupdf.Page, method), f"Page 缺少 {method}"


@check("上下文预翻译写入的缓存能被正式那一遍命中")
def _context_cache_key():
    """This is the whole mechanism: prepare() writes translations into pdf2zh's
    cache and the real pass reads them back. The two instances are built in
    different places, so if their cache parameters ever diverge the app would
    quietly translate everything twice, at full price."""
    import webapp.app  # noqa: F401 - installs the subclass
    from pdf2zh import converter
    from webapp.translator import MeteredDeepseekTranslator

    envs = {"DEEPSEEK_API_KEY": "sk-smoke", "DEEPSEEK_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_EFFORT": "high", "DEEPSEEK_JOB_ID": "smoke",
            "DEEPSEEK_COLLECT": "", "DEEPSEEK_DOC": "abc123"}
    ours = MeteredDeepseekTranslator("en", "zh", "deepseek-v4-flash", envs=envs)
    # Exactly how TranslateConverter builds it.
    theirs = converter.DeepseekTranslator("en", "zh", "deepseek-v4-flash",
                                          envs=envs, prompt=None,
                                          ignore_cache=False)
    assert ours.cache.translate_engine == theirs.cache.translate_engine
    assert ours.cache.translate_engine_params == theirs.cache.translate_engine_params, (
        f"缓存键不一致:\n  ours   {ours.cache.translate_engine_params}\n"
        f"  theirs {theirs.cache.translate_engine_params}")

    # The document is part of the key: a translation belongs to the document it
    # was made for, under that document's description and glossary.
    other = MeteredDeepseekTranslator("en", "zh", "deepseek-v4-flash",
                                      envs={**envs, "DEEPSEEK_DOC": "def456"})
    assert other.cache.translate_engine_params != ours.cache.translate_engine_params, \
        "文档标识没有进入缓存键"


@check("采集模式与琐碎段落判定行为正确")
def _context_rules():
    from webapp import context

    for text in ("n", "12", "1.", "  ", "{v3}", "{v1} {v2}", "—"):
        assert context.is_trivial(text), f"{text!r} 不应被送去翻译"
    for text in ("Abstract", "The cat sat.", "{v0} is a set"):
        assert not context.is_trivial(text), f"{text!r} 应被翻译"

    # Chunking must never split a paragraph, or the cache key would not match
    # the text pdf2zh later looks up.
    items = [(i, "x" * 700) for i in range(5)]
    groups = context.chunks(items, limit=2000)
    assert [i for g in groups for i, _ in g] == list(range(5)), groups
    assert all(sum(len(t) for _, t in g) <= 2000 or len(g) == 1 for g in groups)

    # A paragraph longer than the limit still travels whole.
    big = context.chunks([(0, "y" * 5000)], limit=2000)
    assert big == [[(0, "y" * 5000)]]


@check("短段落的缓存键会被上下文加宽")
def _context_keys():
    from webapp import context

    long_a = "a" * 300
    paras = ["2.1 Overview", "b" * 150, long_a, "Conclusion"]
    keys = context.build_keys(paras)
    # Long enough to stand alone: keyed by itself, so it is reusable elsewhere.
    assert keys[long_a] == long_a
    # Too short: keyed together with what follows, until it is distinctive.
    assert keys["2.1 Overview"].startswith("2.1 Overview\n")
    assert len(keys["2.1 Overview"]) >= context.MIN_KEY_CHARS
    # A tail too short to widen still gets whatever context exists.
    assert keys["Conclusion"] == "Conclusion"

    context.use_keys("smoke-keys", paras)
    try:
        assert context.cache_key("smoke-keys", long_a) == long_a
        assert context.cache_key("smoke-keys", "2.1 Overview") == keys["2.1 Overview"]
        # No map means no honest key for a short paragraph — it must not be
        # cached, or it would leak into unrelated documents.
        assert context.cache_key("no-such-job", "2.1 Overview") is None
        assert context.cache_key("no-such-job", long_a) == long_a
    finally:
        context.forget_keys("smoke-keys")
    assert context.cache_key("smoke-keys", "2.1 Overview") is None


@check("术语表先到先得，冲突改写有护栏")
def _glossary():
    from webapp.context import Glossary, apply_fixups

    g = Glossary({"NSA": "NSA"})
    # An entry translating to itself is the do-not-translate list.
    assert g.terms()["NSA"] == "NSA"

    g.add([("program slicing", "程序切片"), ("lattice", "格")])
    # First writer wins, permanently: consistency is the point.
    g.add([("program slicing", "程序分片")])
    assert g.terms()["program slicing"] == "程序切片"
    assert g.fixups() == {"程序分片": "程序切片"}
    assert apply_fixups("这里用了程序分片。", g.fixups()) == "这里用了程序切片。"

    # Nested renderings must not be rewritten: "切片" -> "程序切片" applied to a
    # text already saying "程序切片" would yield "程序程序切片".
    n = Glossary()
    n.add([("slicing", "程序切片")])
    n.add([("slicing", "切片")])
    assert n.fixups() == {}, n.fixups()

    # Malformed entries cost the terms, never the translation that came with
    # them, and only terms present in the chunk are injected.
    from webapp.context import _pairs
    assert _pairs({"terms": [{"source": "a", "target": "b", "forms": ["as"]},
                             {"source": "c", "target": "d", "forms": "bad"},
                             {"oops": 1}, None]}) == [("a", "b", ["as"]),
                                                      ("c", "d", ())]
    assert dict(g.matching("we discuss program slicing here")) == \
        {"program slicing": "程序切片"}
    assert g.matching("nothing relevant") == []

    # Extraction names a term "Fixed point"; the body writes "fixed point".
    # On SPA.pdf that mismatch meant the agreed 不动点 was never injected.
    c = Glossary({"Fixed point": "不动点"})
    assert dict(c.matching("the least fixed point of f")) == {"Fixed point": "不动点"}
    # ...and the two spellings must not become two contradictory entries.
    c.add([("fixed point", "定点")])
    assert c.terms() == {"Fixed point": "不动点"}, c.terms()
    assert c.fixups() == {"定点": "不动点"}

    # An acronym is only itself: case is all that separates TIP from tip.
    a = Glossary({"TIP": "TIP"})
    assert a.matching("just a tip for you") == []

    # A term appears in several grammatical forms. Pinning one and leaving the
    # others free is worse than having no glossary: on SPA.pdf "Soundness" was
    # pinned to 可靠性 while the adjective "sound" drifted to 健全, and
    # consistency fell from 83% to 64%.
    # The forms are the ones the model reported seeing, not ones derived here:
    # suffix rules would be English-only and would still miss analysis/analyses.
    s = Glossary({"Soundness": "可靠性"},
                 {"Soundness": ["sound", "unsound", "soundly"]})
    for text in ("the analysis is sound", "an unsound type system",
                 "we argue soundness", "it behaves soundly"):
        assert s.matching(text), f"{text!r} 没有匹配到 Soundness"
    assert s.matching("the sky is blue") == []
    # A later chunk reporting a known form as if it were its own term must join
    # the term it belongs to, and its rendering must lose — this is exactly the
    # split that made the glossary harmful: 可靠性 for the noun, 健全 for the
    # adjective, living side by side as two entries.
    s.add([("sound", "健全")])
    assert s.terms() == {"Soundness": "可靠性"}, s.terms()
    assert s.fixups() == {"健全": "可靠性"}
    # 71% of the terms extracted from a real book are more than one word, and
    # English writes a compound with a hyphen in the adjective position and a
    # space in the noun position. Either spelling has to find the other.
    m = Glossary({"Context sensitivity": "上下文敏感",
                  "fixed-point algorithms": "不动点算法"},
                 {"Context sensitivity": ["context-sensitive"]})
    for text in ("a context-sensitive analysis",
                 "the context sensitivity of it",
                 "using fixed point algorithms",
                 "using fixed-point algorithms"):
        assert m.matching(text), f"{text!r} 没有匹配到术语"


@check("静态资源不会被浏览器缓存住")
def _no_stale_assets():
    """i18n.js once had no Cache-Control while index.html had no-store, so the
    page was current and its translations were not: a newly added message
    rendered as its own key."""
    from fastapi.testclient import TestClient
    import webapp.app as app

    with TestClient(app.app) as client:
        for path in ("/", "/i18n.js", "/api/config"):
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.status_code)
            assert resp.headers.get("cache-control") == "no-store", \
                f"{path} 缺少 no-store: {resp.headers.get('cache-control')!r}"


@check("两条文档事实随每个提示词走，术语种子只在并发高时买")
def _brief_everywhere_seed_when_concurrent():
    """The field and topic are cheap and ride in front of every prompt. The
    larger term extraction is cold-start insurance, and a cold start is
    proportional to concurrency: conflicts needing repair went 31 -> 15 -> 4 as
    threads went 8 -> 2, and two runs whose extraction silently failed matched
    the best results ever measured on the same book."""
    import webapp.app as app
    from webapp import context

    src = inspect.getsource(app._with_context)
    assert "context.brief(" in src, "开场的两条事实不再无条件获取"
    assert "context.use_brief(job_id, profile)" in src, "两条事实没有注册给逐段路径"
    assert "LLM_THREADS > SEED_ABOVE_THREADS" in src, "术语种子不再受并发条件控制"
    assert app.SEED_ABOVE_THREADS >= 2

    # The block is what actually reaches a prompt, so it has to survive a
    # missing or partial brief rather than raise or print "None".
    assert context.document_block({}) == ""
    got = context.document_block({"field": "Computer Science", "topic": "T"})
    assert "Computer Science" in got and "T" in got, got

    # And the opening excerpt has to exist even for a one-paragraph document.
    assert context.opening([]) == ""
    assert context.opening(["x" * 40]) == "x" * 40


@check("已是目标语言的文档会被认出来")
def _already_translated():
    """Uploading a previous translation instead of the original costs double.
    Counted by page, not by character: Chinese is far denser than English, so a
    half-Chinese dual document measures 19% by character — under any sensible
    bar — and 44% by page, which is what it actually is."""
    import pymupdf
    from webapp.langcheck import already_translated

    english = "The analysis is sound and terminates on every input program. " * 3
    chinese = "该分析是可靠的，并且在每个输入程序上都会终止。" * 4

    import tempfile
    folder = Path(tempfile.mkdtemp(prefix="smoke-lang-"))

    def build(name, pages):
        doc = pymupdf.open()
        for text, font in pages:
            page = doc.new_page()
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 700), text,
                                fontname=font, fontsize=11)
        path = folder / name
        doc.save(str(path))
        doc.close()
        return path

    original = build("original.pdf", [(english, "helv")] * 4)
    dual = build("dual.pdf", [(english, "helv"), (chinese, "china-s")] * 2)
    try:
        assert not already_translated(original, "zh")["hit"], "原文被误判"
        got = already_translated(dual, "zh")
        assert got["hit"], f"对照版没被认出，占比 {got['ratio']:.0%}"
        # Latin-script targets share an alphabet, so the check stays quiet
        # rather than guessing.
        assert not already_translated(dual, "fr")["hit"]
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


@check("术语必须在摘录里露过面才被采纳")
def _grounded_terms():
    """The frequency list names bare words with no context. A translation
    guessed from one gets pinned for the whole document by first-writer-wins,
    which is a systematic error — worse than the drift the glossary cures."""
    from webapp.context import _grounded

    excerpt = ("A sound analysis over-approximates behaviour. "
               "We use context-sensitive call strings.")
    triples = [
        ("soundness", "可靠性", ["sound"]),          # visible via a form
        ("Context sensitivity", "上下文敏感", ["context-sensitive"]),
        ("call string", "调用串", []),               # visible, plural in text
        ("widening", "加宽", []),                    # frequency list only
        ("Galois connection", "伽罗瓦连接", ["galois"]),
    ]
    kept = [s for s, _, _ in _grounded(triples, excerpt)]
    assert kept == ["soundness", "Context sensitivity", "call string"], kept


@check("文档描述按提取所用文本缓存")
def _profile_reuse():
    from webapp import context

    paras = [f"Paragraph number {i} with enough text to be sampled. " * 4
             for i in range(30)]
    excerpt = context.sample(paras)
    assert excerpt, "采样为空"
    key = context.profile_key(excerpt)
    # Same text, same key — regardless of which file it came from.
    assert context.profile_key(context.sample(list(paras))) == key
    # Different text, different key.
    assert context.profile_key(context.sample(paras[:5])) != key

    path = context._profile_path(key)
    existed = path.exists()
    backup = path.read_bytes() if existed else None
    try:
        context.save_profile(key, {"field": "Machine Learning"})
        assert context.load_profile(key) == {"field": "Machine Learning"}
        # A failed description must not be remembered as final.
        context.save_profile(key, {})
        assert context.load_profile(key) == {"field": "Machine Learning"}
        assert context.load_profile("0" * 32) is None
    finally:
        if existed:
            path.write_bytes(backup)
        else:
            path.unlink(missing_ok=True)


@check("公式占位符能安全穿过 JSON 编解码")
def _context_placeholders():
    import json
    text = 'A {v0} and <b3>x</b3> "quoted" \\ backslash {v12}'
    assert json.loads(json.dumps({"segments": [{"id": 0, "text": text}]},
                                 ensure_ascii=False))["segments"][0]["text"] == text


@check("版面模型入口仍在")
def _layout_model():
    from pdf2zh.doclayout import ModelInstance, OnnxModel
    assert hasattr(ModelInstance, "value")
    assert hasattr(OnnxModel, "load_available")


@check("被后画的填充盖住的文字会被认出来，可见的那一份留下")
def _hidden_text():
    import tempfile
    import pymupdf
    from pdf2zh import converter
    from webapp.verbatim import hidden_spans

    doc = pymupdf.open()
    page = doc.new_page()
    # The shape that hides it must be drawn *after* the buried text and
    # *before* the visible one, which is how a figure with doubly-drawn
    # labels is put together.
    page.insert_text((100, 100), "COND", fontsize=10)
    page.draw_rect(pymupdf.Rect(90, 85, 160, 105), color=None,
                   fill=(1, 1, 1))
    page.insert_text((100, 100), "PRED", fontsize=10)
    page.insert_text((100, 300), "visible", fontsize=10)
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        doc.save(fh.name)
        found = hidden_spans(Path(fh.name))

    boxes = found.get(0, [])
    assert len(boxes) == 1, f"预期只藏了一处，实得 {boxes}"
    x0, y0, x1, y1, count = boxes[0]
    assert count == 4, f"字符数应为 COND 的 4，实得 {count}"
    # Flipped into pdfminer's bottom-up space: near the top of an 842pt page.
    assert y0 > page.rect.height / 2, f"坐标未翻转：{y0}"
    # Untouched text far below must not be caught.
    assert not any(b[1] < page.rect.height / 2 for b in boxes)

    # The converter reads them per thread, and only for the thread that set them.
    assert converter.hidden_boxes() == {}
    converter.hide(found)
    assert converter.hidden_boxes() == found
    converter.hide({})
    assert converter.hidden_boxes() == {}


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

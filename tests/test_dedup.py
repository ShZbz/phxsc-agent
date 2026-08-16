"""dedup 引擎测试：shingle + SimHash + DedupIndex + build_index + detect。

全部离线：DedupIndex/MemoryStore 均指到 tmp_path，不触碰真实
workspace/memory.db/dedup_index.db；embed_check 路径 monkeypatch
embedder 与 cache 单例，禁止真实 embedding API 调用。
"""

import pymupdf
import pytest

from phxsc.memory.store import MemoryStore
from phxsc.tools import dedup as dedup_tools


def _make_pdf(path, page_paras):
    """page_paras: list[list[str]]，每页一个段落列表；段落间用大垂直间距分隔。"""
    doc = pymupdf.open()
    for paras in page_paras:
        page = doc.new_page()
        y = 72
        for para in paras:
            page.insert_text((72, y), para, fontsize=11)
            y += 100
    doc.save(path)
    doc.close()


# 长文本（SimHash 稳定性测试用）：中英混合重复段落，gram 量足够大，
# 单个字符改动对 64 位符号位几乎无影响（实验验证距离为 0）。
_LONG_TEXT = (
    "量子纠缠态在多体系统中的演化行为揭示了非平衡统计物理的深层规律。"
    "The simulation results demonstrate a clear phase transition across the critical regime. "
    "我们进一步分析了纠缠熵的时间演化曲线及其与温度的关系。"
    "Numerical evidence supports the scaling hypothesis in the thermodynamic limit. "
    "实验数据与理论预测在误差范围内高度吻合，验证了模型的可靠性。"
    "The finite-size effects vanish rapidly as the system size increases beyond threshold. "
) * 30

# 完全无关的主题文本（距离应远大于 DUP_DISTANCE）。
_UNRELATED_TEXT = (
    "超导材料在极低温条件下的电子配对机制一直是凝聚态物理的核心难题。"
    "The economic impact of the new policy remains uncertain across different regions. "
    "神经网络的梯度消失问题严重制约了深层模型的训练效率与收敛速度。"
    "Ancient civilizations developed sophisticated irrigation systems along major rivers. "
) * 30


class TestSplitSentences:
    def test_chinese_sentences(self):
        text = "第一句话内容。第二句话内容！第三句话内容？第四句话内容；第五句话内容。"
        assert dedup_tools._split_sentences(text) == [
            "第一句话内容",
            "第二句话内容",
            "第三句话内容",
            "第四句话内容",
            "第五句话内容",
        ]

    def test_english_sentences(self):
        text = "First sentence here. Second sentence here! Third sentence here?"
        assert dedup_tools._split_sentences(text) == [
            "First sentence here",
            "Second sentence here",
            "Third sentence here",
        ]

    def test_newline_separates(self):
        text = "第一行句子内容。\n第二行句子内容。"
        assert dedup_tools._split_sentences(text) == [
            "第一行句子内容",
            "第二行句子内容",
        ]

    def test_empty_text(self):
        assert dedup_tools._split_sentences("") == []
        assert dedup_tools._split_sentences("   \n\n  ") == []

    def test_short_sentences_dropped(self):
        text = "短。这是足够长的句子内容。"
        assert dedup_tools._split_sentences(text) == ["这是足够长的句子内容"]


class TestMakeShingles:
    def test_three_sentences_grouped(self):
        text = "句子一号内容。句子二号内容。句子三号内容。句子四号内容。句子五号内容。句子六号内容。"
        assert dedup_tools._make_shingles(text) == [
            "句子一号内容 句子二号内容 句子三号内容",
            "句子二号内容 句子三号内容 句子四号内容",
            "句子三号内容 句子四号内容 句子五号内容",
            "句子四号内容 句子五号内容 句子六号内容",
        ]

    def test_fewer_than_three_dropped(self):
        assert dedup_tools._make_shingles("句子一号内容。句子二号内容。") == []
        assert len(dedup_tools._make_shingles("句子一号内容。句子二号内容。句子三号内容。")) == 1

    def test_whitespace_normalized(self):
        text = "句子  一号内容。\t句子 二号内容。句子三号 内容。"
        assert dedup_tools._make_shingles(text) == [
            "句子 一号内容 句子 二号内容 句子三号 内容"
        ]

    def test_empty_text(self):
        assert dedup_tools._make_shingles("") == []


class TestSimhash:
    def test_stable_hash_same_text(self):
        h1 = dedup_tools._simhash(_LONG_TEXT)
        h2 = dedup_tools._simhash(_LONG_TEXT)
        assert h1 == h2
        assert isinstance(h1, int)
        assert 0 <= h1 < 2**dedup_tools.SIMHASH_BITS

    def test_slight_change_small_distance(self):
        h0 = dedup_tools._simhash(_LONG_TEXT)
        changed = _LONG_TEXT[:750] + "X" + _LONG_TEXT[751:]
        assert dedup_tools._hamming(h0, dedup_tools._simhash(changed)) <= dedup_tools.DUP_DISTANCE

    def test_unrelated_text_large_distance(self):
        h0 = dedup_tools._simhash(_LONG_TEXT)
        h1 = dedup_tools._simhash(_UNRELATED_TEXT)
        assert dedup_tools._hamming(h0, h1) > dedup_tools.DUP_DISTANCE

    def test_short_text_still_works(self):
        assert dedup_tools._simhash("") == 0
        assert isinstance(dedup_tools._simhash("ab"), int)


class TestDedupIndex:
    @pytest.fixture
    def idx(self, tmp_path):
        db = dedup_tools.DedupIndex(str(tmp_path / "dedup.db"))
        yield db
        db.close()

    def test_add_idempotent(self, idx):
        assert idx.add("s1", 1, 12345, "snippet one") is True
        assert idx.add("s1", 1, 12345, "snippet one") is False
        assert idx.add("s1", 1, 12346, "snippet two") is True
        assert idx.add("s2", 1, 12345, "snippet other") is True
        assert idx.count() == 3

    def test_search_hamming_threshold(self, idx):
        base = 0
        near = base ^ 0b11
        far = base ^ (0b11111111 << 56)
        idx.add("near", 2, near, "close snippet")
        idx.add("far", 3, far, "distant snippet")
        hits = idx.search(base, max_distance=dedup_tools.DUP_DISTANCE)
        assert len(hits) == 1
        hit = hits[0]
        assert hit["source_id"] == "near"
        assert hit["page"] == 2
        assert hit["snippet"] == "close snippet"
        assert hit["shingle_hash"] == near
        assert hit["distance"] == 2

    def test_search_empty_db(self, idx):
        assert idx.search(12345) == []
        assert idx.count() == 0

    def test_has_source_incremental(self, idx):
        assert idx.has_source("x") is False
        idx.add("x", 0, 999, "s")
        assert idx.has_source("x") is True

    def test_default_db_path_under_workdir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHXSC_WORKDIR", str(tmp_path))
        db = dedup_tools.DedupIndex()
        try:
            assert (tmp_path / "dedup_index.db").exists()
        finally:
            db.close()

    def test_default_db_path_constant(self):
        from pathlib import Path

        p = Path(dedup_tools.DEFAULT_DB_PATH)
        assert p.name == "dedup_index.db"
        assert p.parent.name == "workspace"


class TestBuildIndex:
    @pytest.fixture
    def env(self, tmp_path):
        papers = tmp_path / "papers"
        papers.mkdir()
        store = MemoryStore(str(tmp_path / "memory.db"))
        db = dedup_tools.DedupIndex(str(tmp_path / "dedup.db"))
        yield store, db, papers
        db.close()
        store.close()

    def _make_good_pdf(self, papers):
        pdf = papers / "2401.00001.pdf"
        _make_pdf(pdf, [
            ["First sentence one. Second sentence two. Third sentence three. Fourth sentence four."],
            ["Fifth sentence five. Sixth sentence six. Seventh sentence seven. Eighth sentence eight."],
        ])
        return pdf

    def test_build_from_pdf_evidence_summary(self, env):
        store, db, papers = env
        self._make_good_pdf(papers)
        (papers / "bad.pdf").write_bytes(b"this is not a real pdf")
        store.add_evidence(
            "evsrc", 3,
            "Evidence sentence alpha. Evidence sentence beta. "
            "Evidence sentence gamma. Evidence sentence delta.",
        )
        store.add_paper(
            "psrc", "title",
            "Summary sentence one. Summary sentence two. "
            "Summary sentence three. Summary sentence four.",
            "/tmp/x.pdf",
        )

        stats = dedup_tools.build_index(db, papers, store)
        assert stats["files_indexed"] == 1
        assert stats["files_skipped"] == 1
        assert stats["shingles_added"] == 8
        assert db.count() == 8

        rows = db._conn.execute(
            "SELECT source_id, page, shingle_hash, snippet FROM dedup_index ORDER BY id"
        ).fetchall()
        by_source = {}
        for r in rows:
            by_source.setdefault(r["source_id"], []).append(r["page"])
        assert sorted(by_source["2401.00001"]) == [1, 1, 2, 2]
        assert sorted(by_source["evsrc"]) == [3, 3]
        assert sorted(by_source["psrc"]) == [0, 0]

    def test_second_build_skips_indexed_files(self, env):
        store, db, papers = env
        self._make_good_pdf(papers)

        first = dedup_tools.build_index(db, papers, store)
        assert first["files_indexed"] == 1
        assert first["files_skipped"] == 0

        second = dedup_tools.build_index(db, papers, store)
        assert second["files_indexed"] == 0
        assert second["files_skipped"] == 1
        assert second["shingles_added"] == 0
        assert db.count() == first["shingles_added"]

    def test_no_pdfs_returns_zeros(self, env):
        store, db, papers = env
        stats = dedup_tools.build_index(db, papers, store)
        assert stats == {"files_indexed": 0, "files_skipped": 0, "shingles_added": 0}


REF_SENTENCES = [
    f"Reference sentence number {i}." for i in range(1, 11)
]
NEW_SENTENCES = [
    "New sentence number one.", "New sentence number two.",
    "New sentence number three.", "New sentence number four.",
]
REF_TEXT = " ".join(REF_SENTENCES) + " "


class TestDetect:
    @pytest.fixture
    def idx(self, tmp_path):
        db = dedup_tools.DedupIndex(str(tmp_path / "dedup.db"))
        yield db
        db.close()

    def test_detect_with_duplicates(self, idx):
        ref_shingles = dedup_tools._make_shingles(REF_TEXT)
        assert len(ref_shingles) == 8
        for sh in ref_shingles:
            idx.add("refsrc", 1, dedup_tools._simhash(sh), sh)

        query = REF_TEXT + " ".join(NEW_SENTENCES) + " "
        result = dedup_tools.detect(idx, query)
        assert set(result) == {"dup_rate", "total_shingles", "dup_shingles", "matches"}
        assert result["total_shingles"] == 12
        assert result["dup_shingles"] == 8
        assert result["dup_rate"] == pytest.approx(8 / 12)
        assert len(result["matches"]) == 8
        for m in result["matches"]:
            assert m["source_id"] == "refsrc"
            assert m["page"] == 1
            assert m["distance"] == 0
            assert set(m) == {"snippet", "source_id", "page", "distance"}

    def test_detect_no_duplicates(self, idx):
        idx.add("refsrc", 1, dedup_tools._simhash("some unrelated reference shingle text"), "snippet")
        unrelated = " ".join(
            f"Completely different topic sentence number {i}." for i in range(1, 7)
        ) + " "
        result = dedup_tools.detect(idx, unrelated)
        assert result["total_shingles"] == 4
        assert result["dup_shingles"] == 0
        assert result["dup_rate"] == 0.0
        assert result["matches"] == []

    def test_detect_empty_text(self, idx):
        result = dedup_tools.detect(idx, "")
        assert result["total_shingles"] == 0
        assert result["dup_shingles"] == 0
        assert result["dup_rate"] == 0.0
        assert result["matches"] == []

    def test_detect_embed_check(self, idx, monkeypatch):
        import numpy as np

        class FakeEmbedder:
            def encode(self, texts):
                vecs = []
                for t in texts:
                    v = np.zeros(4, dtype=np.float32)
                    for ch in t[:100]:
                        v[ord(ch) % 4] += 1.0
                    norm = float(np.linalg.norm(v))
                    vecs.append(v / norm if norm > 0 else v)
                return np.stack(vecs)

        class FakeCache:
            def __init__(self):
                self._d = {}

            def get_or_compute(self, query, compute_fn):
                if query not in self._d:
                    self._d[query] = compute_fn()
                return self._d[query]

        sh = dedup_tools._make_shingles(REF_TEXT)[0]
        idx.add("refsrc", 1, dedup_tools._simhash(sh), sh)
        monkeypatch.setattr(dedup_tools, "_get_embedder", lambda: FakeEmbedder())
        monkeypatch.setattr(dedup_tools, "_get_embed_cache", lambda: FakeCache())

        query = " ".join(REF_SENTENCES[:3]) + " " + " ".join(NEW_SENTENCES[:2]) + " "
        result = dedup_tools.detect(idx, query, embed_check=True)
        assert result["dup_shingles"] >= 1
        for m in result["matches"]:
            assert set(m) == {"snippet", "source_id", "page", "distance", "embed_sim"}
            assert isinstance(m["embed_sim"], float)
            assert -1.0 <= m["embed_sim"] <= 1.0

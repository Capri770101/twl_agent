"""Dynamic proven retrieval keeps vector scores aligned with the loaded records."""

import pytest

from agent.knowledge import store


@pytest.fixture
def proven_records(monkeypatch):
    records = [{"id": "first", "name": "温暖阳光"}]
    loads = []

    def load():
        loads.append(1)
        return records

    monkeypatch.setattr(store, "_load_proven", load)
    monkeypatch.setattr(store, "_dynamic_index_cache", None)
    monkeypatch.setattr(store.settings, "EMBEDDING_PROVIDER", "")
    return records, loads


def _ids(query):
    return [hit["id"] for hit in store.query_knowledge("proven", query)["results"]]


def test_proven_added_after_first_vector_query(proven_records):
    records, loads = proven_records
    assert _ids("星河花束") == []
    records.append({"id": "second", "name": "星河璀璨"})
    assert _ids("星河花束") == ["second"]
    assert len(loads) == 2  # one coherent load per query, not one for scoring and another for indexing


def test_proven_removed_after_first_vector_query(proven_records):
    records, loads = proven_records
    records.append({"id": "second", "name": "星河璀璨"})
    assert _ids("星河花束") == ["second"]
    records.pop()
    assert _ids("星河花束") == []
    assert len(loads) == 2


def test_proven_same_length_reorder_keeps_scores_on_correct_records(proven_records):
    records, loads = proven_records
    records.append({"id": "second", "name": "星河璀璨"})
    assert _ids("星河花束") == ["second"]
    records.reverse()
    assert _ids("星河花束") == ["second"]
    assert len(loads) == 2


def test_proven_same_length_content_change_invalidates_index(proven_records):
    records, loads = proven_records
    assert _ids("星河花束") == []
    records[0]["name"] = "星河璀璨"
    assert _ids("星河花束") == ["first"]
    assert len(loads) == 2


def test_proven_embedding_uses_retrieval_snapshot_and_reuses_cache(monkeypatch, tmp_path):
    """A second DB load can reorder proven; embeddings must follow the first load."""
    from agent.knowledge import semantic
    from backend import embedding

    records = [
        {"id": "target", "name": "orbit"},
        {"id": "decoy", "name": "sunset"},
    ]
    loads = []

    def load():
        loads.append(1)
        # If semantic reloads while the first query is being scored, it sees
        # the opposite order and attaches the target's vector to the decoy.
        if len(loads) == 2:
            records.reverse()
        return list(records)

    calls = []

    def embed_texts(texts, mode):
        calls.append((mode, list(texts)))
        if mode == "query":
            return [[1.0, 0.0]]
        return [[1.0, 0.0] if "orbit" in text else [0.0, 1.0] for text in texts]

    monkeypatch.setattr(store, "_load_proven", load)
    monkeypatch.setattr(store, "_dynamic_index_cache", None)
    monkeypatch.setattr(store.settings, "EMBEDDING_PROVIDER", "mock")
    monkeypatch.setattr(semantic, "_cache_file", lambda: tmp_path / "embed_cache.json")
    monkeypatch.setattr(embedding, "embed_texts", embed_texts)

    assert _ids("zzznotokenzzz") == ["target"]
    assert len(loads) == 1
    assert calls[0] == ("document", ["target orbit", "decoy sunset"])

    # The next query gets a new, reordered snapshot and must re-key the cache.
    assert _ids("zzznotokenzzz") == ["target"]
    assert len(loads) == 2
    assert calls[2] == ("document", ["decoy sunset", "target orbit"])

    # Same snapshot after that: document embeddings are read from cache.
    assert _ids("zzznotokenzzz") == ["target"]
    assert len(loads) == 3
    assert [mode for mode, _ in calls] == ["document", "query", "document", "query", "query"]

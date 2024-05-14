import uuid

import pytest
import logging
import hypothesis.strategies as st
from hypothesis import given, settings
from typing import Dict, Set, cast, Union, DefaultDict, Any, List
from dataclasses import dataclass
import random

from chromadb.api.fastapi import FastAPI
from chromadb.api.types import ID, Include, IDs, validate_embeddings
import chromadb.errors as errors
from chromadb.api import ServerAPI
from chromadb.api.models.Collection import Collection
import chromadb.test.property.strategies as strategies
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    MultipleResults,
    rule,
    initialize,
    precondition,
    consumes,
    run_state_machine_as_test,
    multiple,
    invariant,
)
from collections import defaultdict
import chromadb.test.property.invariants as invariants
import numpy as np

traces: DefaultDict[str, int] = defaultdict(lambda: 0)


def trace(key: str) -> None:
    global traces
    traces[key] += 1


def print_traces() -> None:
    global traces
    for key, value in traces.items():
        print(f"{key}: {value}")


dtype_shared_st: st.SearchStrategy[
    Union[np.float16, np.float32, np.float64]
] = st.shared(st.sampled_from(strategies.float_types), key="dtype")

dimension_shared_st: st.SearchStrategy[int] = st.shared(
    st.integers(min_value=2, max_value=2048), key="dimension"
)


@dataclass
class EmbeddingStateMachineStates:
    initialize = "initialize"
    add_embeddings = "add_embeddings"
    delete_by_ids = "delete_by_ids"
    update_embeddings = "update_embeddings"
    upsert_embeddings = "upsert_embeddings"


collection_st = st.shared(
    strategies.collections(with_hnsw_params=True, with_persistent_hnsw_params=True),
    key="coll",
)


class EmbeddingStateMachine(RuleBasedStateMachine):
    collection: Collection
    embedding_ids: Bundle[ID] = Bundle("embedding_ids")

    def __init__(self, api: ServerAPI):
        super().__init__()
        self.api = api
        self._rules_strategy = strategies.DeterministicRuleStrategy(self)  # type: ignore

    @initialize(collection=collection_st)  # type: ignore
    def initialize(self, collection: strategies.Collection):
        self.api.reset()
        try:
            self.collection = self.api.create_collection(
                name=collection.name,
                metadata=collection.metadata,
                embedding_function=collection.embedding_function,
            )
        except Exception as e:
            self.api.reset()
            if "hnsw:batch_size" in str(e):
                del collection.metadata["hnsw:batch_size"]
                del collection.metadata["hnsw:sync_threshold"]
                try:
                    self.collection = self.api.create_collection(
                        name=collection.name,
                        metadata=collection.metadata,
                        embedding_function=collection.embedding_function,
                    )
                except Exception as e:
                    raise e
            else:
                raise e
        self.embedding_function = collection.embedding_function
        trace("init")
        self._metadata = collection.metadata
        self.on_state_change(EmbeddingStateMachineStates.initialize)

        self.record_set_state = strategies.StateMachineRecordSet(
            ids=[], metadatas=[], documents=[], embeddings=[]
        )

    @rule(target=embedding_ids, record_set=strategies.recordsets(collection_st))
    def add_embeddings(self, record_set: strategies.RecordSet) -> MultipleResults[ID]:
        trace("add_embeddings")
        self.on_state_change(EmbeddingStateMachineStates.add_embeddings)

        normalized_record_set: strategies.NormalizedRecordSet = invariants.wrap_all(
            record_set
        )

        if len(normalized_record_set["ids"]) > 0:
            trace("add_more_embeddings")

        if not invariants.is_metadata_valid(normalized_record_set):
            with pytest.raises(Exception):
                self.collection.add(**normalized_record_set)
            return multiple()

        intersection = set(normalized_record_set["ids"]).intersection(
            self.record_set_state["ids"]
        )
        if len(intersection) > 0:
            # Partially apply the non-duplicative records to the state
            new_ids = list(set(normalized_record_set["ids"]).difference(intersection))
            indices = [normalized_record_set["ids"].index(id) for id in new_ids]
            filtered_record_set: strategies.NormalizedRecordSet = {
                "ids": [normalized_record_set["ids"][i] for i in indices],
                "metadatas": [normalized_record_set["metadatas"][i] for i in indices]
                if normalized_record_set["metadatas"]
                else None,
                "documents": [normalized_record_set["documents"][i] for i in indices]
                if normalized_record_set["documents"]
                else None,
                "embeddings": [normalized_record_set["embeddings"][i] for i in indices]
                if normalized_record_set["embeddings"]
                else None,
            }
            self.collection.add(**normalized_record_set)
            self._upsert_embeddings(cast(strategies.RecordSet, filtered_record_set))
            return multiple(*filtered_record_set["ids"])

        else:
            self.collection.add(**normalized_record_set)
            self._upsert_embeddings(cast(strategies.RecordSet, normalized_record_set))
            return multiple(*normalized_record_set["ids"])

    @precondition(lambda self: len(self.record_set_state["ids"]) > 20)
    @rule(ids=st.lists(consumes(embedding_ids), min_size=1, max_size=20))
    def delete_by_ids(self, ids: IDs) -> None:
        trace("remove embeddings")
        self.on_state_change(EmbeddingStateMachineStates.delete_by_ids)
        indices_to_remove = [self.record_set_state["ids"].index(id) for id in ids]

        self.collection.delete(ids=ids)
        self._remove_embeddings(set(indices_to_remove))

    # Removing the precondition causes the tests to frequently fail as "unsatisfiable"
    # Using a value < 5 causes retries and lowers the number of valid samples
    @precondition(lambda self: len(self.record_set_state["ids"]) >= 5)
    @rule(
        record_set=strategies.recordsets(
            collection_strategy=collection_st,
            id_strategy=embedding_ids,
            min_size=1,
            max_size=5,
        )
    )
    def update_embeddings(self, record_set: strategies.RecordSet) -> None:
        trace("update embeddings")
        self.on_state_change(EmbeddingStateMachineStates.update_embeddings)

        normalized_record_set: strategies.NormalizedRecordSet = invariants.wrap_all(
            record_set
        )
        if not invariants.is_metadata_valid(normalized_record_set):
            with pytest.raises(Exception):
                self.collection.update(**normalized_record_set)
            return

        self.collection.update(**record_set)
        self._upsert_embeddings(record_set)

    # Using a value < 3 causes more retries and lowers the number of valid samples
    @precondition(lambda self: len(self.record_set_state["ids"]) >= 3)
    @rule(
        record_set=strategies.recordsets(
            collection_strategy=collection_st,
            id_strategy=st.one_of(embedding_ids, strategies.safe_text),
            min_size=1,
            max_size=5,
        )
    )
    def upsert_embeddings(self, record_set: strategies.RecordSet) -> None:
        trace("upsert embeddings")
        self.on_state_change(EmbeddingStateMachineStates.upsert_embeddings)

        normalized_record_set: strategies.NormalizedRecordSet = invariants.wrap_all(
            record_set
        )
        if not invariants.is_metadata_valid(normalized_record_set):
            with pytest.raises(Exception):
                self.collection.upsert(**normalized_record_set)
            return

        self.collection.upsert(**record_set)
        self._upsert_embeddings(record_set)

    @precondition(
        lambda self: "hnsw:batch_size" in self._metadata
        and len(self.record_set_state["ids"]) >= self._metadata["hnsw:batch_size"]
    )
    @rule()
    def swap_embeddings(self) -> None:
        trace("swap embeddings")
        docs = self.collection.get(include=["embeddings", "documents", "metadatas"])
        ids_to_swap = random.sample(docs["ids"], min(5, len(docs["ids"])))
        indices_to_swap = [docs["ids"].index(id) for id in ids_to_swap]
        record_set = {
            "ids": [docs["ids"][i] for i in indices_to_swap],
            "metadatas": [docs["metadatas"][i] for i in indices_to_swap],
            "documents": [docs["documents"][i] for i in indices_to_swap],
            "embeddings": [docs["embeddings"][i] for i in indices_to_swap],
        }
        self.collection.delete(ids=ids_to_swap)
        self.collection.add(**record_set)
        self._upsert_embeddings(record_set)

    @invariant()
    def count(self) -> None:
        invariants.count(
            self.collection, cast(strategies.RecordSet, self.record_set_state)
        )

    @invariant()
    def no_duplicates(self) -> None:
        invariants.no_duplicates(self.collection)

    @invariant()
    def ann_accuracy(self) -> None:
        invariants.ann_accuracy(
            collection=self.collection,
            record_set=cast(strategies.RecordSet, self.record_set_state),
            min_recall=0.95,
            embedding_function=self.embedding_function,
        )

    @invariant()
    def fields_match(self) -> None:
        self.record_set_state = cast(strategies.RecordSet, self.record_set_state)
        invariants.embeddings_match(self.collection, self.record_set_state)
        invariants.metadatas_match(self.collection, self.record_set_state)
        invariants.documents_match(self.collection, self.record_set_state)

    def _upsert_embeddings(self, record_set: strategies.RecordSet) -> None:
        normalized_record_set: strategies.NormalizedRecordSet = invariants.wrap_all(
            record_set
        )
        for idx, id in enumerate(normalized_record_set["ids"]):
            # Update path
            if id in self.record_set_state["ids"]:
                target_idx = self.record_set_state["ids"].index(id)
                if normalized_record_set["embeddings"] is not None:
                    self.record_set_state["embeddings"][
                        target_idx
                    ] = normalized_record_set["embeddings"][idx]
                else:
                    assert normalized_record_set["documents"] is not None
                    assert self.embedding_function is not None
                    self.record_set_state["embeddings"][
                        target_idx
                    ] = self.embedding_function(
                        [normalized_record_set["documents"][idx]]
                    )[
                        0
                    ]
                if normalized_record_set["metadatas"] is not None:
                    # Sqlite merges the metadata, as opposed to old
                    # implementations which overwrites it
                    record_set_state = self.record_set_state["metadatas"][target_idx]
                    if record_set_state is not None:
                        record_set_state = cast(
                            Dict[str, Union[str, int, float]], record_set_state
                        )
                        record_set_state.update(normalized_record_set["metadatas"][idx])
                if normalized_record_set["documents"] is not None:
                    self.record_set_state["documents"][
                        target_idx
                    ] = normalized_record_set["documents"][idx]
            else:
                # Add path
                self.record_set_state["ids"].append(id)
                if normalized_record_set["embeddings"] is not None:
                    self.record_set_state["embeddings"].append(
                        normalized_record_set["embeddings"][idx]
                    )
                else:
                    assert self.embedding_function is not None
                    assert normalized_record_set["documents"] is not None
                    self.record_set_state["embeddings"].append(
                        self.embedding_function(
                            [normalized_record_set["documents"][idx]]
                        )[0]
                    )
                if normalized_record_set["metadatas"] is not None:
                    self.record_set_state["metadatas"].append(
                        normalized_record_set["metadatas"][idx]
                    )
                else:
                    self.record_set_state["metadatas"].append(None)
                if normalized_record_set["documents"] is not None:
                    self.record_set_state["documents"].append(
                        normalized_record_set["documents"][idx]
                    )
                else:
                    self.record_set_state["documents"].append(None)

    def _remove_embeddings(self, indices_to_remove: Set[int]) -> None:
        indices_list = list(indices_to_remove)
        indices_list.sort(reverse=True)

        for i in indices_list:
            del self.record_set_state["ids"][i]
            del self.record_set_state["embeddings"][i]
            del self.record_set_state["metadatas"][i]
            del self.record_set_state["documents"][i]

    def on_state_change(self, new_state: str) -> None:
        pass


def test_embeddings_state(caplog: pytest.LogCaptureFixture, api: ServerAPI) -> None:
    caplog.set_level(logging.ERROR)
    run_state_machine_as_test(lambda: EmbeddingStateMachine(api))  # type: ignore
    print_traces()


def test_multi_add(api: ServerAPI) -> None:
    api.reset()
    coll = api.create_collection(name="foo")
    coll.add(ids=["a"], embeddings=[[0.0]])
    assert coll.count() == 1

    # after the sqlite refactor - add silently ignores duplicates, no exception is raised
    # partial adds are supported - i.e we will add whatever we can in the request
    coll.add(ids=["a"], embeddings=[[0.0]])

    assert coll.count() == 1

    results = coll.get()
    assert results["ids"] == ["a"]

    coll.delete(ids=["a"])
    assert coll.count() == 0


def test_dup_add(api: ServerAPI) -> None:
    api.reset()
    coll = api.create_collection(name="foo")
    with pytest.raises(errors.DuplicateIDError):
        coll.add(ids=["a", "a"], embeddings=[[0.0], [1.1]])
    with pytest.raises(errors.DuplicateIDError):
        coll.upsert(ids=["a", "a"], embeddings=[[0.0], [1.1]])


def test_query_without_add(api: ServerAPI) -> None:
    api.reset()
    coll = api.create_collection(name="foo")
    fields: Include = ["documents", "metadatas", "embeddings", "distances"]
    N = np.random.randint(1, 2000)
    K = np.random.randint(1, 100)
    results = coll.query(
        query_embeddings=np.random.random((N, K)).tolist(), include=fields
    )
    for field in fields:
        field_results = results[field]
        assert field_results is not None
        assert all([len(result) == 0 for result in field_results])


def test_get_non_existent(api: ServerAPI) -> None:
    api.reset()
    coll = api.create_collection(name="foo")
    result = coll.get(ids=["a"], include=["documents", "metadatas", "embeddings"])
    assert len(result["ids"]) == 0
    assert len(result["metadatas"]) == 0
    assert len(result["documents"]) == 0
    assert len(result["embeddings"]) == 0


# TODO: Use SQL escaping correctly internally
@pytest.mark.xfail(reason="We don't properly escape SQL internally, causing problems")
def test_escape_chars_in_ids(api: ServerAPI) -> None:
    api.reset()
    id = "\x1f"
    coll = api.create_collection(name="foo")
    coll.add(ids=[id], embeddings=[[0.0]])
    assert coll.count() == 1
    coll.delete(ids=[id])
    assert coll.count() == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"ids": []},
        {"where": {}},
        {"where_document": {}},
        {"where_document": {}, "where": {}},
    ],
)
def test_delete_empty_fails(api: ServerAPI, kwargs: dict):
    api.reset()
    coll = api.create_collection(name="foo")
    with pytest.raises(Exception) as e:
        coll.delete(**kwargs)
    assert "You must provide either ids, where, or where_document to delete." in str(e)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ids": ["foo"]},
        {"where": {"foo": "bar"}},
        {"where_document": {"$contains": "bar"}},
        {"ids": ["foo"], "where": {"foo": "bar"}},
        {"ids": ["foo"], "where_document": {"$contains": "bar"}},
        {
            "ids": ["foo"],
            "where": {"foo": "bar"},
            "where_document": {"$contains": "bar"},
        },
    ],
)
def test_delete_success(api: ServerAPI, kwargs: dict):
    api.reset()
    coll = api.create_collection(name="foo")
    # Should not raise
    coll.delete(**kwargs)


@given(supported_types=st.sampled_from([np.float32, np.int32, np.int64, int, float]))
def test_autocasting_validate_embeddings_for_compatible_types(
    supported_types: List[Any],
) -> None:
    embds = strategies.create_embeddings(10, 10, supported_types)
    validated_embeddings = validate_embeddings(Collection._normalize_embeddings(embds))
    assert all(
        [
            isinstance(value, list)
            and all(
                [
                    isinstance(vec, (int, float)) and not isinstance(vec, bool)
                    for vec in value
                ]
            )
            for value in validated_embeddings
        ]
    )


@given(supported_types=st.sampled_from([np.float32, np.int32, np.int64, int, float]))
def test_autocasting_validate_embeddings_with_ndarray(
    supported_types: List[Any],
) -> None:
    embds = strategies.create_embeddings_ndarray(10, 10, supported_types)
    validated_embeddings = validate_embeddings(Collection._normalize_embeddings(embds))
    assert all(
        [
            isinstance(value, list)
            and all(
                [
                    isinstance(vec, (int, float)) and not isinstance(vec, bool)
                    for vec in value
                ]
            )
            for value in validated_embeddings
        ]
    )


@given(unsupported_types=st.sampled_from([str, bool]))
def test_autocasting_validate_embeddings_incompatible_types(
    unsupported_types: List[Any],
) -> None:
    embds = strategies.create_embeddings(10, 10, unsupported_types)
    with pytest.raises(ValueError) as e:
        validate_embeddings(Collection._normalize_embeddings(embds))

    assert "Expected each value in the embedding to be a int or float" in str(e)


def test_0dim_embedding_validation() -> None:
    embds = [[]]
    with pytest.raises(ValueError) as e:
        validate_embeddings(embds)
    assert "Expected each embedding in the embeddings to be a non-empty list" in str(e)


@dataclass
class BatchParams:
    batch_size: int
    sync_threshold: int
    item_size: int


@st.composite
def batching_params(draw: st.DrawFn) -> BatchParams:
    batch_size = draw(st.integers(min_value=3, max_value=100))
    sync_threshold = draw(st.integers(min_value=batch_size, max_value=batch_size * 2))
    item_size = draw(
        st.integers(min_value=batch_size + 1, max_value=(batch_size * 2) + 1)
    )
    return BatchParams(
        batch_size=batch_size, sync_threshold=sync_threshold, item_size=item_size
    )


@settings(max_examples=10)
@given(batching_params=batching_params())
def test_get_vector(batching_params: BatchParams, api: ServerAPI) -> None:
    error_distribution = {"IndexError": 0, "TypeError": 0, "NoError": 0}
    rounds = 100
    if isinstance(api, FastAPI) or not api.get_settings().is_persistent:
        pytest.skip("FastAPI does not support this test")
    for _ in range(
        rounds
    ):  # we do a few rounds to ensure that key or lists arrangements (due to UUID randomness) do not affect the test
        api.reset()
        collection = api.get_or_create_collection(
            "test",
            metadata={
                "hnsw:batch_size": batching_params.batch_size,
                "hnsw:sync_threshold": batching_params.sync_threshold,
            },
        )
        items = [
            (f"{uuid.uuid4()}", i, [0.1] * 2) for i in range(batching_params.item_size)
        ]  # we want to exceed the batch size by at least 1
        ids = [item[0] for item in items]
        embeddings = [item[2] for item in items]
        collection.add(ids=ids, embeddings=embeddings)
        collection.delete(ids=[ids[0]])
        collection.add(ids=[ids[0]], embeddings=[[1] * 2])
        try:
            collection.get(include=["embeddings"])
            error_distribution["NoError"] += 1
        except IndexError as e:
            if "list assignment index out of range" in str(e):
                error_distribution["IndexError"] += 1
        except TypeError as e:
            if "'NoneType' object is not subscriptable" in str(e):
                error_distribution["TypeError"] += 1
        invariants.segments_len_match(api, collection)

    assert error_distribution["NoError"] == rounds
    assert error_distribution["IndexError"] == 0
    assert error_distribution["TypeError"] == 0

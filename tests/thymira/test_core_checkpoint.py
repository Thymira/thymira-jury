"""LangGraph checkpoint persistence tests (RA-CORE-07)."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from thymira.core import RuntimeState, StateCheckpointer
from thymira.core.phases import Phase
from thymira.schemas import new_id
from thymira.state import LocalCheckpointRepository

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.runnables import RunnableConfig
    from langgraph.graph.state import CompiledStateGraph


def _runtime_state() -> RuntimeState:
    """Build a valid state for checkpoint tests."""
    return RuntimeState(run_id=new_id("run"), project_id=new_id("project"))


def test_state_checkpointer_round_trips_a_checkpoint(tmp_path: Path) -> None:
    """The public checkpointer API persists and restores a checkpoint tuple."""
    repository = LocalCheckpointRepository(tmp_path)
    saver = StateCheckpointer(repository)
    config: RunnableConfig = {"configurable": {"thread_id": "thread-1"}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"runtime": _runtime_state()}
    checkpoint["id"] = "checkpoint-1"
    metadata: CheckpointMetadata = {"source": "loop", "step": 1}

    saved_config = saver.put(config, checkpoint, metadata, {})
    restored = saver.get_tuple(saved_config)

    assert restored is not None
    assert restored.checkpoint["id"] == "checkpoint-1"
    assert (
        restored.checkpoint["channel_values"]["runtime"] == checkpoint["channel_values"]["runtime"]
    )
    assert restored.metadata == metadata
    assert list(saver.list(config)) == [restored]


def test_state_checkpointer_resumes_after_a_paused_graph(tmp_path: Path) -> None:
    """A graph resumes at the second node after its first checkpoint is persisted."""
    saver = StateCheckpointer(LocalCheckpointRepository(tmp_path))
    calls: list[str] = []

    def thy_node(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("thy")
        return {"phase": Phase.PREPARATION}

    def mira_node(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("mira")
        return {"phase": Phase.MODELING}

    builder = StateGraph(RuntimeState)
    builder.add_node("thy", thy_node)
    builder.add_node("mira", mira_node)
    builder.add_edge(START, "thy")
    builder.add_edge("thy", "mira")
    builder.add_edge("mira", END)
    graph = builder.compile(checkpointer=saver, interrupt_after=["thy"])
    config: RunnableConfig = {"configurable": {"thread_id": "run-thread"}}

    initial = _runtime_state()
    graph.invoke(initial, config)
    graph.invoke(None, config)

    assert calls == ["thy", "mira"]


@pytest.mark.parametrize("write_order", ["root-first", "child-first"])
def test_state_checkpointer_isolates_two_namespaces_sharing_a_thread(
    tmp_path: Path, write_order: str
) -> None:
    """Two namespaces sharing a thread keep independent latest checkpoints, either write order."""
    saver = StateCheckpointer(LocalCheckpointRepository(tmp_path))
    thread_id = "shared-thread"
    root_config: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    child_config: RunnableConfig = {
        "configurable": {"thread_id": thread_id, "checkpoint_ns": "thy:task-1"}
    }
    root_checkpoint = empty_checkpoint()
    root_checkpoint["id"] = "root-checkpoint-1"
    root_checkpoint["channel_values"] = {"runtime": _runtime_state()}
    root_metadata: CheckpointMetadata = {"source": "loop", "step": 1}
    child_checkpoint = empty_checkpoint()
    child_checkpoint["id"] = "child-checkpoint-1"
    child_checkpoint["channel_values"] = {"runtime": _runtime_state()}
    child_metadata: CheckpointMetadata = {"source": "loop", "step": 2}

    def write_root() -> None:
        saver.put(root_config, root_checkpoint, root_metadata, {})

    def write_child() -> None:
        saver.put(child_config, child_checkpoint, child_metadata, {})

    if write_order == "root-first":
        write_root()
        write_child()
    else:
        write_child()
        write_root()

    root_restored = saver.get_tuple(root_config)
    child_restored = saver.get_tuple(child_config)

    assert root_restored is not None
    assert child_restored is not None
    assert root_restored.checkpoint["id"] == "root-checkpoint-1"
    assert child_restored.checkpoint["id"] == "child-checkpoint-1"
    assert (
        root_restored.checkpoint["channel_values"]["runtime"]
        == root_checkpoint["channel_values"]["runtime"]
    )
    assert (
        child_restored.checkpoint["channel_values"]["runtime"]
        == child_checkpoint["channel_values"]["runtime"]
    )
    assert list(saver.list(root_config)) == [root_restored]
    assert list(saver.list(child_config)) == [child_restored]


def test_state_checkpointer_appends_pending_writes_to_one_namespace(tmp_path: Path) -> None:
    """``put_writes`` addresses only the namespace named in its own config."""
    saver = StateCheckpointer(LocalCheckpointRepository(tmp_path))
    thread_id = "shared-thread"
    root_config: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    child_config: RunnableConfig = {
        "configurable": {"thread_id": thread_id, "checkpoint_ns": "thy:task-1"}
    }
    root_checkpoint = empty_checkpoint()
    root_checkpoint["id"] = "root-checkpoint-1"
    child_checkpoint = empty_checkpoint()
    child_checkpoint["id"] = "child-checkpoint-1"
    saver.put(root_config, root_checkpoint, {"source": "loop", "step": 1}, {})
    saver.put(child_config, child_checkpoint, {"source": "loop", "step": 1}, {})

    saver.put_writes(child_config, [("channel", "child-value")], "task-1")

    root_restored = saver.get_tuple(root_config)
    child_restored = saver.get_tuple(child_config)
    assert root_restored is not None
    assert child_restored is not None
    assert root_restored.pending_writes == []
    assert [tuple(write) for write in child_restored.pending_writes or []] == [
        ("task-1", "channel", "child-value")
    ]

    saver.put_writes(root_config, [("channel", "root-value")], "task-2")

    child_restored_again = saver.get_tuple(child_config)
    assert child_restored_again is not None
    assert [tuple(write) for write in child_restored_again.pending_writes or []] == [
        ("task-1", "channel", "child-value")
    ]


def test_state_checkpointer_reads_namespaces_through_a_separate_saver(tmp_path: Path) -> None:
    """A freshly constructed saver over the same root reads both namespaces back."""
    writer = StateCheckpointer(LocalCheckpointRepository(tmp_path))
    thread_id = "shared-thread"
    root_config: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    child_config: RunnableConfig = {
        "configurable": {"thread_id": thread_id, "checkpoint_ns": "thy:task-1"}
    }
    root_checkpoint = empty_checkpoint()
    root_checkpoint["id"] = "root-checkpoint-1"
    child_checkpoint = empty_checkpoint()
    child_checkpoint["id"] = "child-checkpoint-1"

    writer.put(root_config, root_checkpoint, {"source": "loop", "step": 1}, {})
    writer.put(child_config, child_checkpoint, {"source": "loop", "step": 1}, {})

    reader = StateCheckpointer(LocalCheckpointRepository(tmp_path))
    child_restored = reader.get_tuple(child_config)
    root_restored = reader.get_tuple(root_config)

    assert child_restored is not None
    assert root_restored is not None
    assert child_restored.checkpoint["id"] == "child-checkpoint-1"
    assert root_restored.checkpoint["id"] == "root-checkpoint-1"


def _compiled_alpha(calls: list[str]) -> CompiledStateGraph:
    """Build a two-node subgraph that records its own calls under the ``alpha`` node."""

    def a1(state: dict[str, Any]) -> dict[str, Any]:
        del state
        calls.append("a1")
        return {"phase": Phase.PREPARATION}

    def a2(state: dict[str, Any]) -> dict[str, Any]:
        del state
        calls.append("a2")
        return {"phase": Phase.MODELING}

    builder = StateGraph(RuntimeState)
    builder.add_node("a1", a1)
    builder.add_node("a2", a2)
    builder.add_edge(START, "a1")
    builder.add_edge("a1", "a2")
    builder.add_edge("a2", END)
    return builder.compile()


def _compiled_beta(calls: list[str]) -> CompiledStateGraph:
    """Build a two-node subgraph that interrupts after its first node, under the ``beta`` node."""

    def b1(state: dict[str, Any]) -> dict[str, Any]:
        del state
        calls.append("b1")
        return {"phase": Phase.EVALUATION}

    def b2(state: dict[str, Any]) -> dict[str, Any]:
        del state
        calls.append("b2")
        return {"phase": Phase.REPORTING}

    builder = StateGraph(RuntimeState)
    builder.add_node("b1", b1)
    builder.add_node("b2", b2)
    builder.add_edge(START, "b1")
    builder.add_edge("b1", "b2")
    builder.add_edge("b2", END)
    return builder.compile(interrupt_after=["b1"])


def _build_parent(repository: LocalCheckpointRepository, calls: list[str]) -> CompiledStateGraph:
    """Compile a parent graph whose ``alpha``/``beta`` nodes are real compiled subgraphs.

    Neither subgraph is compiled with its own checkpointer, so each inherits the parent's
    :class:`StateCheckpointer` and is assigned a nested ``checkpoint_ns`` by LangGraph itself.
    """
    parent_builder = StateGraph(RuntimeState)
    parent_builder.add_node("alpha", _compiled_alpha(calls))
    parent_builder.add_node("beta", _compiled_beta(calls))
    parent_builder.add_edge(START, "alpha")
    parent_builder.add_edge("alpha", "beta")
    parent_builder.add_edge("beta", END)
    return parent_builder.compile(checkpointer=StateCheckpointer(repository))


def test_two_subgraphs_sharing_a_thread_checkpoint_and_resume_independently(
    tmp_path: Path,
) -> None:
    """Two real subgraphs sharing one thread keep independent checkpoints and resume correctly.

    This is the closure tuple's producer pair -- checkpoint storage plus graph recovery.  The
    independent oracle never uses ``thymira.core.graph.checkpoint``'s own decoder: it lists the
    thread's directory itself, decodes each blob with a separately constructed
    :class:`JsonPlusSerializer`, reads the ``checkpoint_ns`` each stored envelope declares, and
    recomputes that namespace's storage coordinate on its own.
    """
    calls: list[str] = []
    thread_id = "shared-thread"
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    repository = LocalCheckpointRepository(tmp_path)
    parent = _build_parent(repository, calls)
    parent.invoke(_runtime_state(), config, durability="sync")

    assert calls == ["a1", "a2", "b1"]

    thread_dir = tmp_path / "checkpoints" / thread_id
    serializer = JsonPlusSerializer()
    namespaces: set[str] = set()
    for path in thread_dir.iterdir():
        envelope = json.loads(path.read_text(encoding="utf-8"))
        raw = base64.b64decode(envelope["payload"], validate=True)
        stored = serializer.loads_typed((envelope["serde_type"], raw))
        namespace = stored["config"]["configurable"]["checkpoint_ns"]
        namespaces.add(namespace)
        expected_name = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:32] + ".bin"
        assert path.name == expected_name

    assert "" in namespaces
    assert any(namespace.startswith("alpha:") for namespace in namespaces)
    assert any(namespace.startswith("beta:") for namespace in namespaces)
    assert len(namespaces) == 3

    resumed_repository = LocalCheckpointRepository(tmp_path)
    resumed_parent = _build_parent(resumed_repository, calls)
    resumed_parent.invoke(None, config, durability="sync")

    assert calls == ["a1", "a2", "b1", "b2"]

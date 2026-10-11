import pytest

from memory import ConversationMemory


def test_add_and_get_memory():
    mem = ConversationMemory(max_history=5)
    mem.add_message("user", "こんにちは")
    mem.add_message("model", "はい、こんにちは！")

    history = mem.get_history()
    assert len(history) == 2
    assert history[0] == {"role": "user", "content": "こんにちは"}


def test_memory_clear():
    mem = ConversationMemory()
    mem.add_message("user", "テスト")
    mem.clear()
    assert mem.get_history() == []


def test_history_keeps_latest_two_messages_per_turn():
    mem = ConversationMemory(max_history=2)
    for index in range(6):
        mem.add_message("user", f"message-{index}")

    assert len(mem.get_history()) == 4
    assert [message["content"] for message in mem.get_history()] == [
        "message-2", "message-3", "message-4", "message-5"
    ]


def test_history_is_returned_as_a_shallow_copy():
    mem = ConversationMemory()
    mem.add_message("user", "original")
    history = mem.get_history()
    history.clear()
    assert len(mem.get_history()) == 1


def test_invalid_max_history_is_rejected():
    with pytest.raises(ValueError):
        ConversationMemory(max_history=0)

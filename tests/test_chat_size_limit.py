"""Reject oversized chat inputs before DB, memory, or paid LLM calls."""
import pytest
from pydantic import ValidationError

from backend.routers.chat import ChatRequest


@pytest.mark.parametrize('content', ['', 'x' * 8001])
def test_chat_message_size_guard(content):
    with pytest.raises(ValidationError):
        ChatRequest(message=content, user_id='owner')


def test_chat_message_boundary():
    assert len(ChatRequest(message='x' * 8000, user_id='owner').message) == 8000

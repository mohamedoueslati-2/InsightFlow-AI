from __future__ import annotations

import json
import os
from pathlib import Path

from ...schemas.presentation import PresentationConversation, PresentationConversationMessage
from .plan_service import presentation_directory


MAX_MESSAGES = 100


def conversation_path(job_dir: Path) -> Path:
    return presentation_directory(job_dir) / 'presentation_conversation.json'


def load_conversation(job_dir: Path) -> PresentationConversation:
    path = conversation_path(job_dir)
    if not path.is_file():
        return PresentationConversation()
    try:
        return PresentationConversation.model_validate_json(path.read_text(encoding='utf-8'))
    except (ValueError, json.JSONDecodeError):
        return PresentationConversation()


def save_conversation(job_dir: Path, conversation: PresentationConversation) -> Path:
    # Keep a bounded history so a long-running session cannot grow forever.
    if len(conversation.messages) > MAX_MESSAGES:
        conversation.messages = conversation.messages[-MAX_MESSAGES:]
    path = conversation_path(job_dir)
    temp = path.with_suffix('.json.tmp')
    data = conversation.model_dump_json(indent=2)
    with temp.open('w', encoding='utf-8', newline='\n') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)
    return path


def append_message(job_dir: Path, role: str, content: str) -> PresentationConversation:
    conversation = load_conversation(job_dir)
    conversation.messages.append(PresentationConversationMessage(role=role, content=content))
    save_conversation(job_dir, conversation)
    return conversation


def reset_conversation(job_dir: Path, initial_assistant_message: str | None = None) -> PresentationConversation:
    messages = []
    if initial_assistant_message:
        messages.append(PresentationConversationMessage(role='assistant', content=initial_assistant_message))
    conversation = PresentationConversation(messages=messages)
    save_conversation(job_dir, conversation)
    return conversation

from __future__ import annotations

from dataclasses import asdict

from .api import ApiError, request_chat
from .config import AppConfig
from .database import Database, Profile
from .memory import (
    apply_profile_patch,
    build_chat_messages,
    build_memory_extraction_messages,
    heuristic_memories,
    is_key_message,
    parse_memory_json,
)


def prepare_user_turn(
    database: Database,
    conversation_id: int,
    text: str,
    *,
    attachment_path: str = "",
) -> int:
    """Save the new user message and explicit memory facts before the API call."""
    message_id = database.add_message(
        conversation_id,
        "user",
        text,
        is_key=is_key_message(text),
        importance=4 if is_key_message(text) else 0,
        attachment_path=attachment_path,
    )
    for kind, key, value, importance in heuristic_memories(text):
        database.upsert_memory(kind, key, value, importance)
    return message_id


def request_assistant_turn(
    database: Database,
    conversation_id: int,
    config: AppConfig,
) -> str:
    history = database.recent_messages(conversation_id, config.recent_messages)
    last_user = next(
        (item.content for item in reversed(history) if item.role == "user"),
        "",
    )
    profile = database.get_profile()
    memories = database.relevant_memories(last_user, config.memories_in_prompt)
    interactions = database.recent_interactions(conversation_id, limit=20)
    event_scope = f"main:{conversation_id}"
    recent_events = database.recent_event_memories(
        scope=event_scope,
        limit=10,
    )
    related_events = database.relevant_event_memories(
        last_user,
        scope=event_scope,
        limit=6,
    )
    merged_events = {
        event.id: event for event in [*recent_events, *related_events]
    }
    event_summaries = database.relevant_event_summaries(
        last_user,
        scope=event_scope,
        limit=4,
    )
    learned_persona = database.get_pet_persona().prompt_text()
    prompt_messages = build_chat_messages(
        profile,
        memories,
        history,
        interactions,
        event_memories=[
            merged_events[event_id] for event_id in sorted(merged_events)
        ],
        event_summaries=event_summaries,
        learned_persona=learned_persona,
    )
    return request_chat(prompt_messages, config)


def store_assistant_turn(
    database: Database,
    conversation_id: int,
    text: str,
    *,
    attachment_path: str = "",
) -> int:
    return database.add_message(
        conversation_id,
        "assistant",
        text,
        attachment_path=attachment_path,
    )


def should_extract_memory(database: Database, config: AppConfig) -> bool:
    if not config.auto_memory or config.memory_interval < 2:
        return False
    return database.user_message_count() % config.memory_interval == 0


def extract_and_store_memories(
    database: Database,
    config: AppConfig,
) -> int:
    profile = database.get_profile()
    conversation_id = database.latest_conversation(create=False)
    if conversation_id is None:
        return 0
    history = database.recent_messages(conversation_id, min(config.recent_messages, 12))
    interactions = database.recent_interactions(conversation_id, limit=20)
    pet_persona = database.get_pet_persona()
    messages = build_memory_extraction_messages(
        profile,
        history,
        interactions,
        pet_persona.prompt_text(),
    )
    raw = request_chat(
        messages,
        config,
        max_tokens=500,
        temperature=0.1,
        timeout=min(config.request_timeout, 60),
    )
    parsed = parse_memory_json(raw)
    if not parsed:
        return 0

    patch = parsed.get("profile_patch")
    new_profile = apply_profile_patch(profile, patch)
    if asdict(new_profile) != asdict(profile):
        database.save_profile(new_profile)

    stored = 0
    memories = parsed.get("memories")
    if isinstance(memories, list):
        for item in memories[:20]:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            kind = str(item.get("kind", "fact"))
            key = str(item.get("key") or value[:50])
            try:
                importance = int(item.get("importance", 3))
            except (TypeError, ValueError):
                importance = 3
            database.upsert_memory(kind, key, value, importance)
            stored += 1
    persona_learnings = parsed.get("persona_learnings")
    if isinstance(persona_learnings, list):
        stored += database.apply_pet_persona_learnings(
            [
                item
                for item in persona_learnings[:10]
                if isinstance(item, dict)
            ],
            source="conversation_memory_extraction",
        )
    return stored


__all__ = [
    "ApiError",
    "prepare_user_turn",
    "request_assistant_turn",
    "store_assistant_turn",
    "should_extract_memory",
    "extract_and_store_memories",
]

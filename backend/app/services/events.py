"""Redis pub/sub event publishing and subscription."""

import json
import logging
from typing import Any, Dict, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


def get_channel_name(project_id: str) -> str:
    """Get the Redis channel name for a project."""
    return f"events:{project_id}"


async def publish_event(
    redis_client: aioredis.Redis,
    project_id: str,
    event: Dict[str, Any],
) -> bool:
    """
    Publish an event to the project's Redis channel.

    Args:
        redis_client: Redis client
        project_id: Project ID
        event: Event dictionary to publish

    Returns:
        True if published successfully, False otherwise
    """
    try:
        channel = get_channel_name(project_id)
        message = json.dumps(event, default=str)
        await redis_client.publish(channel, message)
        logger.debug(f"Published event to {channel}: {event.get('type', 'unknown')}")
        return True
    except Exception as e:
        logger.error(f"Failed to publish event for project {project_id}: {e}")
        return False


async def subscribe_to_events(
    redis_client: aioredis.Redis,
    project_id: str,
):
    """
    Subscribe to events for a project.

    Args:
        redis_client: Redis client
        project_id: Project ID

    Yields:
        Event dictionaries
    """
    channel = get_channel_name(project_id)
    pubsub = redis_client.pubsub()

    try:
        await pubsub.subscribe(channel)
        logger.info(f"Subscribed to channel: {channel}")

        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    yield data
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode event message: {e}")
            elif message["type"] == "subscribe":
                logger.debug(f"Subscribed to channel: {message['channel']}")
            elif message["type"] == "unsubscribe":
                logger.debug(f"Unsubscribed from channel: {message['channel']}")
                break
    except Exception as e:
        logger.error(f"Error in event subscription for {project_id}: {e}")
        raise
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception:
            pass


async def get_redis_client(redis_url: str) -> aioredis.Redis:
    """
    Create a Redis client from URL.

    Args:
        redis_url: Redis connection URL

    Returns:
        Redis client
    """
    return aioredis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


def create_event(
    event_type: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Helper to create a standardized event dictionary.

    Args:
        event_type: Type of event
        **kwargs: Additional event data

    Returns:
        Event dictionary
    """
    event = {"type": event_type}
    event.update(kwargs)
    return event

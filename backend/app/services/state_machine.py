"""Finite state machine for project and shot status transitions."""

from enum import Enum
from typing import Optional, Set, Dict
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.project import Project, ProjectStatus, ShotStatus, Event
from app.services.events import publish_event


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


# Valid project status transitions
VALID_PROJECT_TRANSITIONS: Dict[ProjectStatus, Set[ProjectStatus]] = {
    ProjectStatus.DRAFT: {ProjectStatus.SCRIPTING},
    ProjectStatus.SCRIPTING: {ProjectStatus.SCRIPT_REVIEW, ProjectStatus.FAILED},
    ProjectStatus.SCRIPT_REVIEW: {
        ProjectStatus.SCRIPTING,
        ProjectStatus.SHOT_GENERATING,
    },
    ProjectStatus.SHOT_GENERATING: {ProjectStatus.SHOT_REVIEW, ProjectStatus.FAILED},
    ProjectStatus.SHOT_REVIEW: {
        ProjectStatus.SHOT_GENERATING,
        ProjectStatus.SCRIPT_REVIEW,
        ProjectStatus.EXPORTING,
    },
    ProjectStatus.EXPORTING: {ProjectStatus.EXPORTED, ProjectStatus.FAILED},
    ProjectStatus.EXPORTED: {
        ProjectStatus.EXPORTING,
        ProjectStatus.SHOT_GENERATING,
        ProjectStatus.SCRIPTING,
    },
    ProjectStatus.FAILED: {ProjectStatus.DRAFT},
}


# Valid shot status transitions
VALID_SHOT_TRANSITIONS: Dict[ShotStatus, Set[ShotStatus]] = {
    ShotStatus.PENDING: {ShotStatus.PROMPT_GENERATING, ShotStatus.FAILED},
    ShotStatus.PROMPT_GENERATING: {ShotStatus.VIDEO_GENERATING, ShotStatus.FAILED},
    ShotStatus.VIDEO_GENERATING: {ShotStatus.COMPLETED, ShotStatus.FAILED},
    ShotStatus.COMPLETED: set(),  # Terminal state
    ShotStatus.FAILED: {ShotStatus.PENDING},  # Can retry
}


async def transition_project_status(
    project: Project,
    target: ProjectStatus,
    actor: str,
    session: AsyncSession,
    redis_client=None,
) -> None:
    """
    Transition project to target status.

    Args:
        project: The project to update
        target: Target status
        actor: Who/what triggered the transition (e.g., 'user:wayne', 'system:worker')
        session: Database session
        redis_client: Optional Redis client for publishing events

    Raises:
        InvalidTransitionError: If the transition is not valid
    """
    current = ProjectStatus(project.status)

    if target not in VALID_PROJECT_TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(
            f"Invalid transition from {current.value} to {target.value}"
        )

    # Update project status
    old_status = project.status
    project.status = target.value
    project.updated_at = datetime.utcnow()

    # Create audit event
    event = Event(
        project_id=project.id,
        actor=actor,
        event_type="state_change",
        payload=f'{{"from": "{old_status}", "to": "{target.value}"}}',
    )
    session.add(event)

    await session.commit()

    # Publish to Redis if available
    if redis_client:
        await publish_event(
            redis_client,
            project.id,
            {
                "type": "state_change",
                "data": {"status": target.value},
            },
        )


async def transition_shot_status(
    shot,
    target: ShotStatus,
    session: AsyncSession,
) -> None:
    """
    Transition shot to target status.

    Args:
        shot: The shot to update
        target: Target status
        session: Database session

    Raises:
        InvalidTransitionError: If the transition is not valid
    """
    current = ShotStatus(shot.status)

    if target not in VALID_SHOT_TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(
            f"Invalid shot transition from {current.value} to {target.value}"
        )

    shot.status = target.value
    shot.updated_at = datetime.utcnow()
    await session.commit()


def is_valid_project_transition(from_status: ProjectStatus, to_status: ProjectStatus) -> bool:
    """Check if a project status transition is valid without executing it."""
    return to_status in VALID_PROJECT_TRANSITIONS.get(from_status, set())


def is_valid_shot_transition(from_status: ShotStatus, to_status: ShotStatus) -> bool:
    """Check if a shot status transition is valid without executing it."""
    return to_status in VALID_SHOT_TRANSITIONS.get(from_status, set())

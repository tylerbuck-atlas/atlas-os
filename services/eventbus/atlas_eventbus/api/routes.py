"""/v1 — events, subscriptions, schemas, liveness."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import SERVICE_NAME, __version__
from ..auth import Identity, require_identity
from ..bus import EventBus, SchemaValidationError
from ..models import (
    AckRequest,
    EventEnvelope,
    PublishRequest,
    PullRequest,
    PullResponse,
    SchemaRegistration,
    Subscription,
    SubscriptionRequest,
    TopicSchema,
)

router = APIRouter(tags=["eventbus"])


def _bus(request: Request) -> EventBus:
    return request.app.state.bus


# -- liveness -----------------------------------------------------------------

@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


# -- events ---------------------------------------------------------------------

@router.post(
    "/v1/events",
    response_model=EventEnvelope,
    status_code=status.HTTP_201_CREATED,
    summary="Publish an event",
)
async def publish(
    body: PublishRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
) -> EventEnvelope:
    try:
        return await _bus(request).publish(body, source=identity.name)
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"payload does not match schema v{exc.version} "
                f"for topic {exc.topic!r}: {exc.detail}"
            ),
        )


# -- subscriptions -----------------------------------------------------------------

@router.post(
    "/v1/subscriptions",
    response_model=Subscription,
    status_code=status.HTTP_201_CREATED,
    summary="Create (or fetch) a named subscription for the calling service",
)
async def subscribe(
    body: SubscriptionRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
) -> Subscription:
    return await _bus(request).subscribe(body, service_name=identity.name)


@router.get(
    "/v1/subscriptions",
    response_model=list[Subscription],
    summary="List the calling service's subscriptions",
)
async def list_subscriptions(
    request: Request, identity: Identity = Depends(require_identity)
) -> list[Subscription]:
    return await request.app.state.store.list_subscriptions(
        service_name=identity.name
    )


async def _owned_subscription(
    subscription_id: str, request: Request, identity: Identity
) -> Subscription:
    subscription = await request.app.state.store.get_subscription(subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="unknown subscription")
    if subscription.service_name != identity.name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="subscription belongs to another service",
        )
    return subscription


@router.post(
    "/v1/subscriptions/{subscription_id}/pull",
    response_model=PullResponse,
    summary="Pull deliveries (long-polls up to wait_seconds)",
)
async def pull(
    subscription_id: str,
    body: PullRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
) -> PullResponse:
    await _owned_subscription(subscription_id, request, identity)
    messages = await _bus(request).pull(
        subscription_id,
        max_messages=body.max_messages,
        wait_seconds=body.wait_seconds,
    )
    return PullResponse(messages=messages)


@router.post(
    "/v1/subscriptions/{subscription_id}/ack",
    summary="Acknowledge processed deliveries",
)
async def ack(
    subscription_id: str,
    body: AckRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
) -> dict:
    await _owned_subscription(subscription_id, request, identity)
    acked = await _bus(request).ack(subscription_id, body.delivery_ids)
    return {"acked": acked}


@router.delete(
    "/v1/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a subscription and its pending deliveries",
)
async def unsubscribe(
    subscription_id: str,
    request: Request,
    identity: Identity = Depends(require_identity),
) -> None:
    await _owned_subscription(subscription_id, request, identity)
    await request.app.state.store.delete_subscription(subscription_id)


# -- schemas -----------------------------------------------------------------------

@router.put(
    "/v1/schemas/{topic}",
    response_model=TopicSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new schema version for a topic",
)
async def register_schema(
    topic: str,
    body: SchemaRegistration,
    request: Request,
    identity: Identity = Depends(require_identity),
) -> TopicSchema:
    import jsonschema as _jsonschema

    try:
        _jsonschema.Draft202012Validator.check_schema(body.json_schema)
    except _jsonschema.SchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"not a valid JSON Schema: {exc.message}",
        )
    return await request.app.state.store.register_schema(topic, body.json_schema)


@router.get(
    "/v1/schemas",
    response_model=list[TopicSchema],
    summary="List latest schema versions for all topics",
)
async def list_schemas(
    request: Request, identity: Identity = Depends(require_identity)
) -> list[TopicSchema]:
    return await request.app.state.store.list_schemas()


@router.get(
    "/v1/schemas/{topic}",
    response_model=TopicSchema,
    summary="Get the latest schema for a topic",
)
async def get_schema(
    topic: str, request: Request, identity: Identity = Depends(require_identity)
) -> TopicSchema:
    schema = await request.app.state.store.latest_schema(topic)
    if schema is None:
        raise HTTPException(status_code=404, detail="no schema registered for topic")
    return schema

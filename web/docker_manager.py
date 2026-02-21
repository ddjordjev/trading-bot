"""Docker container lifecycle manager for dynamic bot spawning.

The hub bot (bot-momentum) uses the Docker socket to start and stop
sibling bot containers at runtime.  New containers inherit the base
.env configuration and get profile-specific overrides on top.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from loguru import logger

from config.bot_profiles import BotProfile

try:
    import docker
    from docker.errors import NotFound

    DOCKER_AVAILABLE = True
except ImportError:
    docker = None  # type: ignore[assignment,unused-ignore]
    NotFound = Exception  # type: ignore[assignment,misc,unused-ignore]
    DOCKER_AVAILABLE = False


_HUB_CONTAINER = "bot-momentum"
_VOLUME_DATA = "trading-bot_bot-data"
_VOLUME_LOGS = "trading-bot_bot-logs"
_NETWORK = "trading-bot_default"
_ENV_PATH = Path("/app/.env")

# Keys we never forward from the hub's env to child containers
_SKIP_KEYS = {"DASHBOARD_PORT", "DASHBOARD_ENABLED", "BOT_ID", "BOT_STRATEGIES", "BOT_STYLE"}


def _get_client() -> Any:
    if not DOCKER_AVAILABLE:
        raise RuntimeError("docker Python SDK not installed — pip install docker")
    return docker.from_env()


def container_name(profile_id: str) -> str:
    return f"bot-{profile_id}"


def _load_base_env() -> dict[str, str]:
    """Load base env vars from the .env file baked into the image."""
    env: dict[str, str] = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in _SKIP_KEYS:
                    env[key] = val.strip()
        return env

    for key, val in os.environ.items():
        if key.isupper() and not key.startswith("_") and key not in _SKIP_KEYS:
            env[key] = val
    return env


def _hub_image() -> str:
    """Resolve the Docker image from the running hub container."""
    client = _get_client()
    try:
        hub = client.containers.get(_HUB_CONTAINER)
        tags = hub.image.tags
        return str(tags[0]) if tags else str(hub.image.id)
    except Exception:
        return "trading-bot-bot-momentum:latest"


def get_container_status(profile_id: str) -> str:
    """Return 'running', 'exited', or 'missing'."""
    try:
        client = _get_client()
        c = client.containers.get(container_name(profile_id))
        return str(c.status)
    except Exception:
        return "missing"


def list_running_profiles() -> set[str]:
    """Return profile IDs whose containers are currently running."""
    try:
        client = _get_client()
        containers = client.containers.list(filters={"name": "bot-"})
        running: set[str] = set()
        for c in containers:
            name: str = c.name
            if name.startswith("bot-") and c.status == "running":
                running.add(name[4:])  # strip "bot-" prefix
        return running
    except Exception:
        return set()


async def start_container(profile: BotProfile) -> tuple[bool, str]:
    """Create and start a Docker container for this profile."""
    name = container_name(profile.id)
    try:
        client = _get_client()
    except Exception as e:
        return False, f"Docker not available: {e}"

    try:
        existing = client.containers.get(name)
        if existing.status == "running":
            return True, f"{name} already running"
        existing.remove(force=True)
    except NotFound:
        pass
    except Exception as e:
        logger.warning("Removing stale container {}: {}", name, e)
        with contextlib.suppress(Exception):
            client.containers.get(name).remove(force=True)

    image = _hub_image()
    hub_url = f"http://{_HUB_CONTAINER}:9035"

    env = _load_base_env()
    env.update(
        {
            "TZ": "UTC",
            "BOT_ID": profile.id,
            "BOT_STYLE": profile.style,
            "BOT_STRATEGIES": ",".join(profile.strategies),
            "DASHBOARD_ENABLED": "false",
            "DASHBOARD_HUB_URL": hub_url,
        }
    )
    env.update(profile.env_overrides)

    def _run() -> Any:
        return client.containers.run(
            image=image,
            name=name,
            command="python bot.py",
            environment=env,
            volumes={
                _VOLUME_DATA: {"bind": "/app/data", "mode": "rw"},
                _VOLUME_LOGS: {"bind": "/app/logs", "mode": "rw"},
            },
            network=_NETWORK,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            labels={"tradeborg.profile": profile.id, "tradeborg.managed": "true"},
        )

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _run)
        logger.info("Started container {} (image={})", name, image)
        return True, f"Started {name}"
    except Exception as e:
        logger.error("Failed to start {}: {}", name, e)
        return False, str(e)


async def stop_container(profile_id: str, timeout: int = 30) -> tuple[bool, str]:
    """Stop and remove a bot container."""
    name = container_name(profile_id)
    try:
        client = _get_client()
    except Exception as e:
        return False, f"Docker not available: {e}"

    try:
        c = client.containers.get(name)
    except NotFound:
        return True, f"{name} not found (already removed)"
    except Exception as e:
        return False, str(e)

    loop = asyncio.get_event_loop()

    try:
        await loop.run_in_executor(None, lambda: c.stop(timeout=timeout))
    except Exception as e:
        logger.warning("Stop {} error (will force-remove): {}", name, e)

    try:
        await loop.run_in_executor(None, lambda: c.remove(force=True))
        logger.info("Stopped and removed {}", name)
        return True, f"Stopped {name}"
    except Exception as e:
        return False, f"Remove failed: {e}"

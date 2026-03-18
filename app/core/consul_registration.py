"""Consul service registration helper for Status Service."""

import logging
import os
import socket
import subprocess
from typing import Optional

import consul

from app.core import settings

logger = logging.getLogger("app_logger")


def is_running_in_docker() -> bool:
    try:
        if os.path.exists("/.dockerenv"):
            return True
        if os.path.exists("/proc/self/cgroup"):
            with open("/proc/self/cgroup", "r") as f:
                contents = f.read()
            return "docker" in contents or "kubepods" in contents
    except Exception as exc:  # pragma: no cover
        logger.debug("Docker detection failed: %s", exc)
    return False


def get_host_ip_from_docker() -> str:
    try:
        host_docker_ip = socket.gethostbyname("host.docker.internal")
        if host_docker_ip and not host_docker_ip.startswith("127."):
            logger.info("Detected host IP via host.docker.internal: %s", host_docker_ip)
            return host_docker_ip
    except Exception:
        pass

    try:
        with open("/proc/net/route", "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gateway_hex = parts[2]
                    if len(gateway_hex) == 8:
                        gateway_ip = ".".join(
                            str(int(gateway_hex[i : i + 2], 16))
                            for i in range(6, -1, -2)
                        )
                        logger.info(
                            "Detected Docker gateway IP from /proc/net/route: %s",
                            gateway_ip,
                        )
                        if not gateway_ip.startswith("172.17.") and not gateway_ip.startswith(
                            "172.18."
                        ):
                            return gateway_ip
    except Exception as e:
        logger.debug("Failed to read /proc/net/route: %s", e)

    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if "via" in parts:
                gateway_ip = parts[parts.index("via") + 1]
                logger.info("Detected Docker gateway IP via ip command: %s", gateway_ip)
                if not gateway_ip.startswith("172.17.") and not gateway_ip.startswith(
                    "172.18."
                ):
                    return gateway_ip
    except Exception as e:
        logger.debug("Failed to use ip command: %s", e)

    return ""


def get_local_ip() -> str:
    if getattr(settings, "CONSUL_SERVICE_EXTERNAL_IP", None):
        logger.info(
            "Using configured CONSUL_SERVICE_EXTERNAL_IP for Consul registration: %s",
            settings.CONSUL_SERVICE_EXTERNAL_IP,
        )
        return settings.CONSUL_SERVICE_EXTERNAL_IP

    if is_running_in_docker():
        host_ip = get_host_ip_from_docker()
        if host_ip:
            logger.info("Auto-detected host IP in Docker: %s", host_ip)
            return host_ip
        logger.warning(
            "Running inside Docker but could not auto-detect host IP. "
            "Set CONSUL_SERVICE_EXTERNAL_IP explicitly if needed."
        )

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        logger.info("Using local IP for Consul registration: %s", ip)
        return ip
    except Exception as e:
        logger.warning("Failed to detect local IP: %s; falling back to 127.0.0.1", e)
        return "127.0.0.1"


class ConsulServiceRegistry:
    def __init__(self) -> None:
        self.consul_client: Optional[consul.Consul] = None
        self.service_id: Optional[str] = None
        self.registered: bool = False

        if getattr(settings, "CONSUL_ENABLED", False):
            try:
                self.consul_client = consul.Consul(
                    host=settings.CONSUL_HOST,
                    port=settings.CONSUL_PORT,
                )
                logger.info("Initialized Consul client: %s:%s", settings.CONSUL_HOST, settings.CONSUL_PORT)
            except Exception as e:
                logger.error("Failed to initialize Consul client: %s", e)
                self.consul_client = None

    def register_service(
        self,
        service_name: Optional[str] = None,
        service_port: Optional[int] = None,
        service_path: Optional[str] = None,
        auth_required: Optional[str] = None,
        health_check_url: Optional[str] = None,
    ) -> bool:
        if not getattr(settings, "CONSUL_ENABLED", False):
            logger.info("Consul registration is disabled")
            return False

        if not self.consul_client:
            logger.warning("Consul client is not initialized, skipping registration")
            return False

        try:
            service_name = service_name or settings.CONSUL_SERVICE_NAME
            default_port = getattr(settings, "CONSUL_SERVICE_PORT", getattr(settings, "APP_PORT", 8115))
            external_port = getattr(settings, "CONSUL_SERVICE_EXTERNAL_PORT", None)
            service_port = service_port or external_port or default_port

            service_path = (service_path or settings.CONSUL_SERVICE_PATH).rstrip("/") or "/"
            auth_required = (auth_required or settings.CONSUL_SERVICE_AUTH or "mixed").lower()

            service_address = get_local_ip()
            health_check_address = (
                "127.0.0.1" if str(settings.CONSUL_HOST) in {"localhost", "127.0.0.1"} else service_address
            )

            self.service_id = f"{service_name}-{service_address}-{service_port}"

            health_check_url = health_check_url or "/health"
            health_check_enabled = getattr(settings, "CONSUL_HEALTH_CHECK_ENABLED", False)
            health_check = None
            if health_check_enabled:
                health_check = consul.Check.http(
                    url=f"http://{health_check_address}:{service_port}{health_check_url}",
                    interval="10s",
                    timeout="5s",
                    deregister="30s",
                )

            tags = [
                "api",
                "fastapi",
                f"path={service_path}",
                f"auth={auth_required}",
            ]

            if auth_required == "mixed":
                no_auth_paths = [
                    f"{service_path}/health",
                    "/health",
                ]
                for path in no_auth_paths:
                    tags.append(f"no_auth_path={path}")

            register_kwargs = dict(
                name=service_name,
                service_id=self.service_id,
                address=service_address,
                port=service_port,
                tags=tags,
            )
            if health_check is not None:
                register_kwargs["check"] = health_check

            self.consul_client.agent.service.register(**register_kwargs)
            self.registered = True

            logger.info(
                "Registered service with Consul: %s (ID=%s, %s:%s, path=%s, auth=%s)",
                service_name,
                self.service_id,
                service_address,
                service_port,
                service_path,
                auth_required,
            )
            return True
        except Exception as e:
            logger.error("Failed to register service with Consul: %s", e, exc_info=True)
            return False

    def deregister_service(self) -> bool:
        if not getattr(settings, "CONSUL_ENABLED", False) or not self.consul_client:
            return False
        if not self.registered or not self.service_id:
            logger.warning("Service not registered with Consul, skipping deregistration")
            return False

        try:
            self.consul_client.agent.service.deregister(self.service_id)
            self.registered = False
            logger.info("Deregistered service from Consul: %s", self.service_id)
            return True
        except Exception as e:
            logger.error("Failed to deregister service from Consul: %s", e, exc_info=True)
            return False


consul_registry = ConsulServiceRegistry()

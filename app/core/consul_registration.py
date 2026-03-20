"""
Consul service registration and health check management.
"""

import logging
import os
import socket
import subprocess
from typing import Optional

import consul

from app.core import settings
from typing import Iterable

logger = logging.getLogger("app_logger")


def is_running_in_kubernetes() -> bool:
    """
    Best-effort check for Kubernetes runtime.

    We prefer Kubernetes-specific IP detection (pod IP) over Docker-host heuristics,
    otherwise Consul may be given an unroutable/incorrect address.
    """
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return True
    if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount"):
        return True
    return False


def is_running_in_docker() -> bool:
    """
    Check if the application is running inside a Docker container.

    Returns:
        bool: True if running in Docker, False otherwise.
    """
    try:
        if os.path.exists("/.dockerenv"):
            return True
        if os.path.exists("/proc/self/cgroup"):
            with open("/proc/self/cgroup", "r") as f:
                contents = f.read()
            return "docker" in contents or "kubepods" in contents
    except Exception as exc:  # pragma: no cover - best-effort detection
        logger.debug("Error while detecting Docker environment: %s", exc)
    return False


def get_host_ip_from_docker() -> str:
    """
    Get the Docker host's external IP address when running inside a container.

    Tries multiple methods to detect the host's IP that's accessible from outside.

    Returns:
        str: Host IP address, or empty string if detection fails.
    """
    try:
        host_docker_ip = socket.gethostbyname("host.docker.internal")
        if host_docker_ip and not host_docker_ip.startswith("127."):
            logger.info(
                "Detected Docker host IP via host.docker.internal: %s",
                host_docker_ip,
            )
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
    """
    Get the IP address for Consul service registration.

    Priority:
    0. Kubernetes pod IP (when running in Kubernetes)
    1. Auto-detected host IP when running in Docker
    2. Container/local IP (fallback)
    """
    if is_running_in_kubernetes():
        pod_ip = (os.getenv("POD_IP") or os.getenv("MY_POD_IP") or "").strip()
        if pod_ip and not pod_ip.startswith("127."):
            logger.info("Using Kubernetes pod IP for Consul registration: %s", pod_ip)
            return pod_ip

        try:
            host_ip = socket.gethostbyname(socket.gethostname())
            if host_ip and not host_ip.startswith("127."):
                logger.info(
                    "Using hostname-resolved IP for Consul registration in Kubernetes: %s",
                    host_ip,
                )
                return host_ip
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.debug("Failed to resolve hostname IP in Kubernetes: %s", exc)

    if is_running_in_docker() and not is_running_in_kubernetes():
        host_ip = get_host_ip_from_docker()
        if host_ip:
            logger.info("Auto-detected host IP in Docker: %s", host_ip)
            return host_ip
        logger.warning(
            "Running in Docker but could not detect host IP. "
            "Falling back to container IP for Consul registration."
        )

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 80))
        ip = s.getsockname()[0]
        s.close()
        logger.info("Using local IP for Consul registration: %s", ip)
        return ip
    except Exception as e:  # pragma: no cover - network dependent
        logger.warning("Failed to get local IP via UDP method: %s", e)
        try:
            fallback_ip = socket.gethostbyname(socket.gethostname())
            if fallback_ip and not fallback_ip.startswith("127."):
                logger.info("Using hostname-resolved IP for Consul registration: %s", fallback_ip)
                return fallback_ip
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.debug("Failed to resolve hostname IP: %s", exc)
        logger.warning("Falling back to localhost for Consul registration")
        return "127.0.0.1"


class ConsulServiceRegistry:
    """
    Handles Consul service registration and deregistration.
    """

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
                logger.info(
                    "Consul client initialized: %s:%s",
                    settings.CONSUL_HOST,
                    settings.CONSUL_PORT,
                )
            except Exception as e:
                logger.error("Failed to initialize Consul client: %s", e)
                self.consul_client = None

    def register_service(
        self,
        service_name: Optional[str] = None,
        service_port: Optional[int] = None,
        health_check_url: Optional[str] = None,
        service_path: Optional[str] = None,
        auth_required: Optional[str] = None,
    ) -> bool:
        """
        Register the service with Consul.
        """
        if not getattr(settings, "CONSUL_ENABLED", False):
            logger.info("Consul registration is disabled")
            return False

        if not self.consul_client:
            logger.warning("Consul client not initialized, skipping registration")
            return False

        try:
            service_name = service_name or settings.CONSUL_SERVICE_NAME
            # Service port is driven by status-service specific env config.
            service_port = int(os.getenv("STATUS_SERVICE_PORT", str(settings.CONSUL_SERVICE_PORT)))
            service_path = service_path or settings.CONSUL_SERVICE_PATH
            service_path = service_path.rstrip("/") or "/"
            auth_required = (auth_required or settings.CONSUL_SERVICE_AUTH or "mixed").lower()
            # Default Consul health check should use root-path endpoint.
            health_check_url = health_check_url or f"{service_path}/health"

            if service_port == int(settings.CONSUL_PORT):
                logger.warning(
                    "Service registration port equals Consul server port (%s). "
                    "Check STATUS_SERVICE_PORT/CONSUL_SERVICE_PORT env values.",
                    service_port,
                )

            service_address = get_local_ip()
            health_check_address = (
                "127.0.0.1"
                if str(settings.CONSUL_HOST) in {"localhost", "127.0.0.1"}
                else service_address
            )

            self.service_id = f"{service_name}-{service_address}-{service_port}"

            health_check_enabled = getattr(settings, "CONSUL_HEALTH_CHECK_ENABLED", True)
            if health_check_enabled:
                health_check = consul.Check.http(
                    url=f"http://{health_check_address}:{service_port}{health_check_url}",
                    interval="10s",
                    timeout="5s",
                    deregister="30s",
                )
            else:
                health_check = None

            tags = ["status-service", "api", "fastapi", f"path={service_path}", f"auth={auth_required}"]

            # Add no-auth paths for endpoints that do not require JWT.
            # In this service, those are defined in `app.api.status_api`.
            if auth_required in {"mixed", "jwt"}:
                try:
                    from app.api.status_api import NO_AUTH_PATHS

                    no_auth_paths: Iterable[str] = NO_AUTH_PATHS
                except Exception:
                    # Fallback to a safe minimal list
                    no_auth_paths = [
                        "/health",
                        f"{service_path}/health",
                        f"{service_path}/model/api/docs",
                        f"{service_path}/openapi.json",
                        f"{service_path}/redoc",
                        f"{service_path}/ws/tasks/{{task_id}}",
                    ]

                # Ensure deterministic + de-duplicated tags
                for path in sorted(set(no_auth_paths)):
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
                "Service registered with Consul: %s "
                "(ID: %s, Address: %s:%s, Path: %s, Auth: %s%s)",
                service_name,
                self.service_id,
                service_address,
                service_port,
                service_path,
                auth_required,
                f", Health Check: http://{health_check_address}:{service_port}{health_check_url}"
                if health_check
                else ", Health Check: disabled",
            )
            return True

        except Exception as e:
            logger.error("Failed to register service with Consul: %s", e, exc_info=True)
            return False

    def deregister_service(self) -> bool:
        """
        Deregister the service from Consul.
        """
        if not getattr(settings, "CONSUL_ENABLED", False) or not self.consul_client:
            return False

        if not self.registered or not self.service_id:
            logger.warning("Service not registered, skipping deregistration")
            return False

        try:
            self.consul_client.agent.service.deregister(self.service_id)
            self.registered = False
            logger.info("Service deregistered from Consul: %s", self.service_id)
            return True
        except Exception as e:
            logger.error("Failed to deregister service from Consul: %s", e, exc_info=True)
            return False

    def is_registered(self) -> bool:
        return self.registered


consul_registry = ConsulServiceRegistry()

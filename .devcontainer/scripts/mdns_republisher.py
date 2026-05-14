"""Publish virtual Duckietown robots on the dev container mDNS interface."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from importlib import import_module
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

# Import dbus dynamically so static analysis does not fail in
# environments where dbus stubs are unavailable.
dbus = import_module("dbus")
import_module("dbus.mainloop.glib")

LOGGER = logging.getLogger("mdns_republisher")

AVAHI_BUS_NAME = "org.freedesktop.Avahi"
AVAHI_SERVER_INTERFACE = "org.freedesktop.Avahi.Server"
AVAHI_ENTRY_GROUP_INTERFACE = "org.freedesktop.Avahi.EntryGroup"

AVAHI_PROTO_INET = 0
AVAHI_DOMAIN = "local"
AVAHI_FLAGS = dbus.UInt32(0)
DNS_CLASS_IN = dbus.UInt16(0x0001)
DNS_TYPE_A = dbus.UInt16(0x0001)
DNS_TTL_SECONDS = dbus.UInt32(10)

# Duckietown mDNS service names follow DT::<FIELD>::<ROBOT>.
MIN_SERVICE_NAME_PARTS = 3
MIN_AVAHI_COLUMNS = 9
PORT_COLUMN = 8
TXT_START_COLUMN = 9
ROBOT_CONTAINER_PREFIX = "dts-virtual-"
# Startup scripts can run before PATH is fully settled, so use absolute
# fallbacks for the external commands this process depends on.
DOCKER_EXECUTABLE = shutil.which("docker") or "/usr/bin/docker"
IP_EXECUTABLE = shutil.which("ip") or "/usr/sbin/ip"


class SystemBusLike(Protocol):
    """Subset of the D-Bus system bus API used by the republisher."""

    def get_object(self, bus_name: str, object_path: object) -> object:
        """Return the D-Bus object at one path."""


class AvahiServerLike(Protocol):
    """Subset of the Avahi server API used by the republisher."""

    EntryGroupNew: Callable[[], object]


@dataclass(frozen=True)
class ServiceRecord:
    """Single mDNS service payload for a robot."""

    port: int
    txt_records: tuple[bytes, ...]


@dataclass
class PublishedRobot:
    """In-memory state for a republished robot."""

    services: dict[str, ServiceRecord] = field(default_factory=dict)
    group_path: str | None = None


class MdnsRepublisher:
    """Mirror Duckietown mDNS entries between interfaces."""

    SOURCE_INTERFACE_DEFAULT = "docker0"
    PUBLISH_INTERFACE_DEFAULT = "eth0"
    SERVICE_TYPE = "_duckietown._tcp"
    POLL_INTERVAL_SECONDS = 2
    DEFAULT_DISCOVERY_PORT = 11911
    PUBLISH_IPV4_ENV = "MDNS_PUBLISH_IPV4"

    source_interface: str
    publish_interface: str
    publish_interface_index: int
    bus: SystemBusLike
    server: AvahiServerLike
    publish_ipv4: str
    robots: dict[str, PublishedRobot]
    running: bool

    def __init__(
        self,
        source_interface: str = SOURCE_INTERFACE_DEFAULT,
        publish_interface: str = PUBLISH_INTERFACE_DEFAULT,
    ) -> None:
        """Create a republisher for source and publish interfaces."""
        self.source_interface = source_interface
        self.publish_interface = publish_interface
        self.publish_interface_index = self._interface_index(publish_interface)

        self.bus = self._system_bus()
        root_object = self._root_object(self.bus)
        self.server = self._server_interface(root_object)
        publish_ip = os.environ.get(
            self.PUBLISH_IPV4_ENV,
            "",
        )
        publish_ipv4_override = publish_ip.strip()
        # Allow startup or tests to override the published A record when the
        # interface address is not the endpoint visible to clients.
        if publish_ipv4_override:
            self.publish_ipv4 = publish_ipv4_override
        else:
            self.publish_ipv4 = self._interface_ipv4(publish_interface)
        self.robots = {}
        self.running = True

    @staticmethod
    def _interface_index(interface_name: str) -> int:
        """Resolve OS interface index from interface name."""
        return socket.if_nametoindex(interface_name)

    @staticmethod
    def _system_bus() -> SystemBusLike:
        """Create a D-Bus system bus client."""
        return dbus.SystemBus()

    @staticmethod
    def _root_object(system_bus: SystemBusLike) -> object:
        """Get Avahi root D-Bus object."""
        return system_bus.get_object(AVAHI_BUS_NAME, "/")

    @staticmethod
    def _server_interface(root_object: object) -> AvahiServerLike:
        """Get Avahi server interface wrapper."""
        return dbus.Interface(root_object, AVAHI_SERVER_INTERFACE)

    @staticmethod
    def _interface_ipv4(interface_name: str) -> str:
        """Get the primary IPv4 address of a network interface."""
        result = subprocess.run(  # noqa: S603
            [IP_EXECUTABLE, "-4", "addr", "show", interface_name],
            check=True,
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            line_ = line.strip()
            if not line_.startswith("inet "):
                continue
            address_parts = line_.split()
            address = address_parts[1]
            cidr_parts = address.split("/", 1)
            return cidr_parts[0]

        msg = f"Could not determine IPv4 for interface '{interface_name}'"
        raise RuntimeError(msg)

    def start(self) -> None:
        """Start polling and keep republished records in sync."""
        LOGGER.info(
            "Republishing %s services from %s to %s",
            self.SERVICE_TYPE,
            self.source_interface,
            self.publish_interface,
        )

        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        while self.running:
            self._sync_services()
            time.sleep(self.POLL_INTERVAL_SECONDS)

    def _stop(self, *_: object) -> None:
        """Stop polling and free entry groups created by this script."""
        LOGGER.info("Stopping mDNS republisher")
        self.running = False

        for robot_name in list(self.robots):
            self._free_group(robot_name)

    def _sync_services(self) -> None:
        """Sync source services into publish interface groups."""
        discovered = self._discover_services()
        virtual_robot_names = self._discover_virtual_robot_names()
        # Virtual containers may take a moment to publish their own records, so
        # synthesize the baseline discovery records as soon as Docker sees them.
        for robot_name in virtual_robot_names:
            default_services = self._default_virtual_services(robot_name)
            discovered_services = discovered.get(robot_name, {})
            default_services.update(discovered_services)
            discovered[robot_name] = default_services

        known_robots = set(self.robots)
        discovered_robots = set(discovered)

        removed_robots = sorted(known_robots - discovered_robots)
        for robot_name in removed_robots:
            self._free_group(robot_name)
            self.robots.pop(robot_name, None)
            LOGGER.info("Removed republished services for %s", robot_name)

        sorted_robot_names = sorted(discovered)
        for robot_name in sorted_robot_names:
            services = discovered[robot_name]
            robot = self.robots.get(robot_name)
            is_new_robot = robot is None
            if robot is None:
                robot = PublishedRobot()
                self.robots[robot_name] = robot
            needs_republish = is_new_robot or (robot.group_path is None)
            # Avoid Avahi churn when the discovered service payload is unchanged.
            if not needs_republish and robot.services == services:
                continue

            robot.services = services
            self._republish_robot(robot_name)
            count = len(services)
            LOGGER.info("Republished %s service(s) for %s", count, robot_name)

    def _discover_services(self) -> dict[str, dict[str, ServiceRecord]]:
        """Read source interface records from avahi-browse output."""
        # Use parseable output and a stable locale so field positions stay fixed.
        command = ["avahi-browse", "-r", "-p", "-t", self.SERVICE_TYPE]
        env = dict(os.environ)
        env["LC_ALL"] = "C"

        try:
            result = subprocess.run(  # noqa: S603
                command,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            LOGGER.warning("avahi-browse failed: %s", exc)
            return {}

        robots: dict[str, dict[str, ServiceRecord]] = {}
        output_lines = result.stdout.splitlines()
        for raw_line in output_lines:
            line = raw_line.strip()
            # Resolved service rows begin with '=;' in avahi-browse -p output.
            if not line.startswith("=;"):
                continue

            parts = line.split(";")
            if len(parts) < MIN_AVAHI_COLUMNS:
                continue

            interface_name = parts[1]
            if interface_name != self.source_interface:
                continue

            service_name = self._unescape_avahi(parts[3])
            try:
                robot_name = self._robot_name_from_service(service_name)
            except ValueError:
                continue

            port = int(parts[PORT_COLUMN])
            txt_records = self._parse_txt_records(parts[TXT_START_COLUMN:])
            service_record = ServiceRecord(port=port, txt_records=txt_records)

            robot_services = robots.get(robot_name)
            if robot_services is None:
                robot_services = {}
                robots[robot_name] = robot_services
            robot_services[service_name] = service_record

        return robots

    @staticmethod
    def _discover_virtual_robot_names() -> set[str]:
        """Get names of running virtual robots from container names."""
        command = [DOCKER_EXECUTABLE, "ps", "--format", "{{.Names}}"]
        try:
            result = subprocess.run(  # noqa: S603
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            return set()

        names = set()
        for raw_line in result.stdout.splitlines():
            container_name = raw_line.strip()
            if not container_name.startswith(ROBOT_CONTAINER_PREFIX):
                continue
            robot_name = container_name.removeprefix(ROBOT_CONTAINER_PREFIX)
            if not robot_name:
                continue
            names.add(robot_name)
        return names

    @staticmethod
    def _unescape_avahi(value: str) -> str:
        """Convert avahi-browse escaped values to plain strings."""
        return value.replace(r"\058", ":")

    @staticmethod
    def _parse_txt_records(parts: list[str]) -> tuple[bytes, ...]:
        """Parse avahi-browse TXT columns into bytes payload items."""
        if not parts:
            return ()

        txt = ";".join(parts)
        txt = txt.strip()
        if not txt:
            return ()

        # TXT records can contain semicolons, so rejoin Avahi's trailing columns
        # before shlex splits the quoted payloads back into individual strings.
        try:
            parsed_items = shlex.split(txt)
        except ValueError:
            stripped = txt.strip('"')
            parsed_items = [stripped]

        encoded_items = []
        for item in parsed_items:
            if not item:
                continue
            encoded_items.append(item.encode("utf-8"))
        return tuple(encoded_items)

    @classmethod
    def _default_virtual_services(
        cls,
        robot_name: str,
    ) -> dict[str, ServiceRecord]:
        """Return baseline DT services for virtual robot discovery."""
        port = cls.DEFAULT_DISCOVERY_PORT
        return {
            f"DT::PRESENCE::{robot_name}": ServiceRecord(
                port=port,
                txt_records=(),
            ),
            f"DT::ONLINE::{robot_name}": ServiceRecord(
                port=port,
                txt_records=(),
            ),
            f"DT::ROBOT_TYPE::{robot_name}": ServiceRecord(
                port=port,
                txt_records=(b'{"type":"duckiebot"}',),
            ),
            f"DT::ROBOT_CONFIGURATION::{robot_name}": ServiceRecord(
                port=port,
                txt_records=(b'{"configuration":"virtual"}',),
            ),
            f"DT::ROBOT_HARDWARE::{robot_name}": ServiceRecord(
                port=port,
                txt_records=(b'{"hardware":"virtual"}',),
            ),
        }

    @staticmethod
    def _robot_name_from_service(service_name: str) -> str:
        """Extract robot name from a DT::<SERVICE>::<ROBOT> record."""
        parts = service_name.split("::")
        if len(parts) != MIN_SERVICE_NAME_PARTS:
            msg = f"Unsupported Duckietown service name: {service_name}"
            raise ValueError(msg)
        return parts[2]

    def _republish_robot(self, robot_name: str) -> None:
        """Publish one robot's service records to publish interface."""
        robot = self.robots[robot_name]
        self._free_group(robot_name)

        try:
            group_path = self.server.EntryGroupNew()
            group_object = self.bus.get_object(AVAHI_BUS_NAME, group_path)
            group = dbus.Interface(group_object, AVAHI_ENTRY_GROUP_INTERFACE)

            # The robot container sits behind nested Docker. Publish the
            # dev container address so host can use Docker-published ports.
            host_fqdn = f"{robot_name}.{AVAHI_DOMAIN}"
            target_ipv4 = self.publish_ipv4
            ipv4_packed = socket.inet_aton(target_ipv4)
            ipv4_bytes = dbus.ByteArray(ipv4_packed)
            group.AddRecord(
                self.publish_interface_index,
                AVAHI_PROTO_INET,
                AVAHI_FLAGS,
                host_fqdn,
                DNS_CLASS_IN,
                DNS_TYPE_A,
                DNS_TTL_SECONDS,
                ipv4_bytes,
            )

            sorted_service_names = sorted(robot.services)
            for service_name in sorted_service_names:
                record = robot.services[service_name]
                # Avahi's D-Bus API expects TXT entries as arrays of bytes.
                txt_array_items = [
                    dbus.ByteArray(value) for value in record.txt_records
                ]
                txt_array = dbus.Array(txt_array_items, signature="ay")
                record_port = dbus.UInt16(record.port)

                group.AddService(
                    self.publish_interface_index,
                    AVAHI_PROTO_INET,
                    AVAHI_FLAGS,
                    service_name,
                    self.SERVICE_TYPE,
                    AVAHI_DOMAIN,
                    host_fqdn,
                    record_port,
                    txt_array,
                )

            group.Commit()
            robot.group_path = str(group_path)
        except dbus.DBusException as exc:
            robot.group_path = None
            LOGGER.warning(
                "Could not republish mDNS records for %s: %s",
                robot_name,
                exc,
            )

    def _free_group(self, robot_name: str) -> None:
        """Free Avahi entry group for a robot if it exists."""
        robot = self.robots.get(robot_name)
        if robot is None:
            return
        if not robot.group_path:
            return

        try:
            group_path = robot.group_path
            group_object = self.bus.get_object(AVAHI_BUS_NAME, group_path)
            group = dbus.Interface(group_object, AVAHI_ENTRY_GROUP_INTERFACE)
            # Free stale groups so removed or renamed robots stop advertising.
            group.Free()
        except dbus.DBusException:
            pass

        robot.group_path = None


def main() -> int:
    """Entrypoint for the republisher process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    source_interface_value = os.environ.get(
        "MDNS_SOURCE_INTERFACE",
        MdnsRepublisher.SOURCE_INTERFACE_DEFAULT,
    )
    source_interface = source_interface_value.strip()
    if not source_interface:
        source_interface = MdnsRepublisher.SOURCE_INTERFACE_DEFAULT

    publish_interface_value = os.environ.get(
        "MDNS_PUBLISH_INTERFACE",
        MdnsRepublisher.PUBLISH_INTERFACE_DEFAULT,
    )
    publish_interface = publish_interface_value.strip()
    if not publish_interface:
        publish_interface = MdnsRepublisher.PUBLISH_INTERFACE_DEFAULT

    LOGGER.info(
        "Using source interface '%s' and publish interface '%s'",
        source_interface,
        publish_interface,
    )
    republisher = MdnsRepublisher(
        source_interface=source_interface,
        publish_interface=publish_interface,
    )
    republisher.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

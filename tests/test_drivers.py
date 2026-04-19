"""Driver registry + DeviceSpec tests.

Fast unit-level tests. No network, no real device poll — these verify
that the config schema is parsed correctly and that drivers fail loudly
on misconfiguration rather than silently producing broken pollers.
"""

from __future__ import annotations

import pytest

from pollers.drivers import DRIVERS, DeviceSpec, get_driver
from pollers.drivers.base import DeviceSpec as DeviceSpec2
from pollers.drivers.peplink_router import PeplinkRouterDriver
from pollers.drivers.unifi_network import UniFiNetworkDriver
from pollers.drivers.icmp_ping import IcmpPingDriver


class TestRegistry:
    def test_known_drivers_registered(self) -> None:
        # Any new driver added to the registry should appear here. The
        # list doubles as a manifest for docs ("these kinds are
        # currently supported").
        assert set(DRIVERS.keys()) == {
            "peplink_router",
            "unifi_network",
            "icmp_ping",
        }

    def test_get_driver_returns_class(self) -> None:
        cls = get_driver("peplink_router")
        assert cls is PeplinkRouterDriver
        assert cls is DRIVERS["peplink_router"]

    def test_unknown_kind_raises_with_listing(self) -> None:
        with pytest.raises(KeyError) as exc:
            get_driver("not_a_real_kind")
        # The error should list the known kinds so users of
        # config.yaml get a useful hint in the server log.
        msg = str(exc.value)
        assert "not_a_real_kind" in msg
        assert "peplink_router" in msg
        assert "unifi_network" in msg


class TestDeviceSpecParsing:
    def test_full_config(self) -> None:
        spec = DeviceSpec.from_config(
            "truck",
            {
                "kind": "peplink_router",
                "name": "Truck Router",
                "host": "192.168.50.1",
                "username": "admin",
                "password": "s3cret",
                "poll_interval": 20,
                "verify_ssl": True,
                "is_mobile": True,
                "wan_carriers": {1: "att", "2": "verizon"},
                "ssh": {"enabled": True, "port": 8822},
            },
        )
        assert spec.id == "truck"
        assert spec.kind == "peplink_router"
        assert spec.display_name == "Truck Router"
        assert spec.host == "192.168.50.1"
        assert spec.username == "admin"
        assert spec.password == "s3cret"
        assert spec.poll_interval == 20
        assert spec.verify_ssl is True
        assert spec.is_mobile is True
        # Keys coerced to str (YAML can yield ints for "1", "2")
        assert spec.wan_carriers == {"1": "att", "2": "verizon"}
        # Unknown fields land in extra, preserved verbatim
        assert spec.extra == {"ssh": {"enabled": True, "port": 8822}}

    def test_minimal_config(self) -> None:
        spec = DeviceSpec.from_config(
            "minimal",
            {"kind": "icmp_ping", "targets": [{"host": "1.1.1.1"}]},
        )
        assert spec.id == "minimal"
        # Display name defaults to id when `name:` missing
        assert spec.display_name == "minimal"
        assert spec.host == ""
        assert spec.poll_interval == 10
        assert spec.extra == {"targets": [{"host": "1.1.1.1"}]}

    def test_missing_kind_raises(self) -> None:
        with pytest.raises(KeyError, match="missing required 'kind'"):
            DeviceSpec.from_config("bad", {"host": "1.2.3.4"})

    def test_wan_carriers_none_is_empty_dict(self) -> None:
        spec = DeviceSpec.from_config(
            "x",
            {"kind": "unifi_network", "host": "1.1.1.1",
             "username": "a", "wan_carriers": None},
        )
        assert spec.wan_carriers == {}


class TestPeplinkRouterDriver:
    def _good_spec(self, **overrides) -> DeviceSpec:
        base = {
            "kind": "peplink_router",
            "host": "192.168.1.1",
            "username": "admin",
        }
        base.update(overrides)
        return DeviceSpec.from_config("rt", base)

    def test_requires_host(self) -> None:
        spec = DeviceSpec(id="x", kind="peplink_router",
                          display_name="x", host="", username="admin")
        with pytest.raises(ValueError, match="missing required 'host'"):
            PeplinkRouterDriver(spec)

    def test_requires_username(self) -> None:
        spec = DeviceSpec(id="x", kind="peplink_router",
                          display_name="x", host="1.1.1.1", username="")
        with pytest.raises(ValueError, match="missing required 'username'"):
            PeplinkRouterDriver(spec)

    def test_builds_rest_poller_only_when_ssh_disabled(self, state, ws) -> None:
        # Default spec — no ssh config → only the REST poller is built.
        drv = PeplinkRouterDriver(self._good_spec())
        pollers = drv.build_pollers(state=state, ws_manager=ws)
        assert len(pollers) == 1
        # The REST poller's `name` is the state-key prefix, which equals
        # the device id — this is the invariant the whole dashboard
        # depends on, so lock it down with a test.
        assert pollers[0].name == "rt"

    def test_ssh_disabled_explicitly(self, state, ws) -> None:
        drv = PeplinkRouterDriver(
            self._good_spec(ssh={"enabled": False, "targets": []})
        )
        pollers = drv.build_pollers(state=state, ws_manager=ws)
        assert len(pollers) == 1

    def test_ssh_enabled_builds_two_pollers(self, state, ws) -> None:
        drv = PeplinkRouterDriver(
            self._good_spec(ssh={
                "enabled": True,
                "targets": [{"host": "8.8.8.8", "name": "Google"}],
            })
        )
        pollers = drv.build_pollers(state=state, ws_manager=ws)
        # REST + SSH ping streamer
        assert len(pollers) == 2
        names = {p.name for p in pollers}
        assert names == {"rt", "rt_ssh"}

    def test_default_key_prefixes_match_device_id(self) -> None:
        # State keys follow the convention <id>_internet.* / <id>_tunnel.*
        # The dashboard's ping sections look up those specific prefixes.
        prefixes = PeplinkRouterDriver._default_key_prefixes("truck")
        assert prefixes == {
            "internet": "truck_internet",
            "tunnel":   "truck_tunnel",
        }


class TestUniFiNetworkDriver:
    def test_requires_host_and_username(self) -> None:
        no_host = DeviceSpec(id="x", kind="unifi_network",
                             display_name="x", host="", username="a")
        with pytest.raises(ValueError, match="host"):
            UniFiNetworkDriver(no_host)
        no_user = DeviceSpec(id="x", kind="unifi_network",
                             display_name="x", host="1.1.1.1", username="")
        with pytest.raises(ValueError, match="username"):
            UniFiNetworkDriver(no_user)

    def test_patches_poller_name_to_device_id(self, state, ws) -> None:
        spec = DeviceSpec.from_config(
            "gateway",
            {"kind": "unifi_network", "host": "192.168.1.1",
             "username": "netmon"},
        )
        drv = UniFiNetworkDriver(spec)
        pollers = drv.build_pollers(state=state, ws_manager=ws)
        assert len(pollers) == 1
        # Critical: state keys must be prefixed by the user-chosen id,
        # not the legacy "udm" hardcode.
        assert pollers[0].name == "gateway"


class TestIcmpPingDriver:
    def test_requires_targets(self) -> None:
        spec = DeviceSpec(id="x", kind="icmp_ping",
                          display_name="x")
        with pytest.raises(ValueError, match="at least one target"):
            IcmpPingDriver(spec)

    def test_builds_one_poller(self, state, ws) -> None:
        spec = DeviceSpec.from_config(
            "lan",
            {"kind": "icmp_ping",
             "targets": [{"host": "192.168.1.1", "name": "gw"}]},
        )
        drv = IcmpPingDriver(spec)
        pollers = drv.build_pollers(state=state, ws_manager=ws)
        assert len(pollers) == 1
        assert pollers[0].name == "lan"

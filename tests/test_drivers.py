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
# InControl is no longer a driver — it's a top-level integration
# (see `/api/integrations/incontrol` in server.py). Its former tests now
# live in test_integration.py / test_migration.py.


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


# ---- set_wan_enabled contract per driver -------------------------------
#
# One test per driver so a future refactor can't silently drop the
# method or regress the 501-path. Peplink's happy-path test mocks the
# aiohttp session to avoid touching real hardware.


class _FakeUnifiResp:
    """Minimal aiohttp-response surrogate the UniFi driver reads."""

    def __init__(self, payload, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"http {self.status}")

    async def json(self):
        return self._payload


class _FakeUnifiSession:
    """Captures the GET/PUT pair issued by UniFi set_wan_enabled."""

    closed = False

    def __init__(self, networkconf_list: list[dict],
                 get_status: int = 200, put_status: int = 200) -> None:
        self.networks = networkconf_list
        self.get_status = get_status
        self.put_status = put_status
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get(self, url: str):
        self.calls.append(("GET", url, None))
        return _FakeUnifiResp({"data": self.networks}, status=self.get_status)

    async def put(self, url: str, json):
        self.calls.append(("PUT", url, json))
        return _FakeUnifiResp(
            {"data": [json]}, status=self.put_status,
        )


def _unifi_driver_with_fake_session(networks, **status_overrides):
    """Build a UniFi driver with `_poller` stubbed enough for the code
    path that reuses an already-authed session."""
    from pollers.drivers.unifi_network import UniFiNetworkDriver
    spec = DeviceSpec(
        id="gw", kind="unifi_network",
        display_name="gw", host="1.1.1.1", username="netmon", password="x",
    )
    drv = UniFiNetworkDriver(spec)

    fake = _FakeUnifiSession(networks, **status_overrides)

    class _FakePoller:
        _session = fake
        _authenticated = True
        base_url = "https://1.1.1.1"

        async def _authenticate(self):
            self._authenticated = True

    drv._poller = _FakePoller()  # type: ignore[assignment]
    return drv, fake


class TestSetWanEnabled:
    def test_unifi_happy_path_rmw(self) -> None:
        """UniFi driver should GET networkconf, pick the right WAN by
        wan_networkgroup, and PUT the full doc back with enabled flipped."""
        import asyncio as _asyncio
        networks = [
            {
                "_id": "abc123",
                "name": "WAN2",
                "purpose": "wan",
                "wan_networkgroup": "WAN2",
                "enabled": True,
                "wan_type": "dhcp",
                "other_field": "preserved",
            },
            {
                "_id": "def456",
                "name": "WAN",
                "purpose": "wan",
                "wan_networkgroup": "WAN",
                "enabled": True,
            },
            {
                "_id": "lan001",
                "name": "LAN",
                "purpose": "corporate",
            },
        ]
        drv, fake = _unifi_driver_with_fake_session(networks)
        result = _asyncio.run(drv.set_wan_enabled(2, False))
        assert result["ok"] is True
        assert result["wan_index"] == 2
        assert result["enabled"] is False
        assert result["ui_name"] in ("WAN2", "WAN2")

        # Expect a GET list + PUT /abc123.
        methods = [c[0] for c in fake.calls]
        urls = [c[1] for c in fake.calls]
        assert methods == ["GET", "PUT"]
        assert urls[0].endswith("/rest/networkconf")
        assert urls[1].endswith("/rest/networkconf/abc123")

        # The PUT body must be the FULL doc (not a partial) with only
        # `enabled` flipped; other_field must survive.
        put_body = fake.calls[1][2]
        assert put_body["_id"] == "abc123"
        assert put_body["enabled"] is False
        assert put_body["other_field"] == "preserved"
        assert put_body["wan_type"] == "dhcp"

    def test_unifi_wan_not_found_raises(self) -> None:
        import asyncio as _asyncio
        networks = [
            {"_id": "x", "purpose": "corporate", "name": "LAN"},
        ]
        drv, _ = _unifi_driver_with_fake_session(networks)
        with pytest.raises(ValueError, match="no WAN network"):
            _asyncio.run(drv.set_wan_enabled(1, True))

    def test_unifi_401_bubbles_as_connection_error(self) -> None:
        import asyncio as _asyncio
        networks = [
            {"_id": "abc", "purpose": "wan",
             "wan_networkgroup": "WAN", "enabled": True},
        ]
        drv, _ = _unifi_driver_with_fake_session(
            networks, get_status=401,
        )
        with pytest.raises(ConnectionError, match="UniFi authentication"):
            _asyncio.run(drv.set_wan_enabled(1, False))

    def test_icmp_ping_raises_not_implemented(self) -> None:
        spec = DeviceSpec.from_config(
            "lan", {"kind": "icmp_ping",
                    "targets": [{"host": "1.1.1.1"}]},
        )
        drv = IcmpPingDriver(spec)
        import asyncio as _asyncio
        with pytest.raises(NotImplementedError, match="WAN"):
            _asyncio.run(drv.set_wan_enabled(1, True))

    def test_peplink_router_happy_path_via_mocked_session(
        self, state, ws
    ) -> None:
        """Verify the Peplink driver issues the right POST without
        touching real hardware. We build the driver, stub its REST
        poller's authenticated aiohttp session with a fake that records
        the call, then assert the expected endpoint + body."""
        import asyncio as _asyncio

        spec = DeviceSpec.from_config(
            "rt",
            {"kind": "peplink_router", "host": "1.2.3.4", "username": "a"},
        )
        drv = PeplinkRouterDriver(spec)
        pollers = drv.build_pollers(state=state, ws_manager=ws)
        rest = pollers[0]

        # Fake aiohttp response that mimics the attributes we read.
        class _FakeResp:
            def __init__(self, payload: dict, status: int = 200) -> None:
                self.status = status
                self._payload = payload
                self.raised = False

            def raise_for_status(self) -> None:
                if self.status >= 400:
                    self.raised = True
                    raise RuntimeError(f"http {self.status}")

            async def json(self) -> dict:
                return self._payload

        captured: list[tuple[str, dict]] = []

        class _FakeSession:
            closed = False

            async def post(self, url: str, json: dict):
                captured.append((url, json))
                # Differentiate the apply call vs the wan.connection
                # write — both return {"stat":"ok"} in practice.
                return _FakeResp({"stat": "ok"})

        # Pretend the REST poller is already authenticated so
        # set_wan_enabled takes the session-reuse path.
        rest._session = _FakeSession()  # type: ignore[assignment]
        rest._authenticated = True

        result = _asyncio.run(drv.set_wan_enabled(2, False))
        # The function returns the wan.connection response as-is.
        assert result == {"stat": "ok"}
        # Expect two POSTs: the wan.connection write + the config apply.
        urls = [c[0] for c in captured]
        bodies = [c[1] for c in captured]
        assert "https://1.2.3.4/api/config.wan.connection" in urls
        assert "https://1.2.3.4/api/cmd.config.apply" in urls
        # The wan.connection call must carry the {id, enable} body.
        wan_call = next(c for c in captured
                        if c[0].endswith("config.wan.connection"))
        assert wan_call[1] == {"id": 2, "enable": False}


# ---- Peplink driver: carrier / RAT / SF control contracts --------------
#
# Each test swaps the driver's internal PeplinkController with a stub
# that records the call. We assert the method the endpoint is expected
# to invoke gets called with the right args — this is the contract the
# /api/devices/{id}/control/* endpoints rely on.


class _StubController:
    """Captures the high-level controller methods called by the driver."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def set_roamlink_auto_and_reconnect(self) -> dict:
        self.calls.append(("auto", (), {}))
        return {"carrier": "ok", "reconnect": {"disable": "ok", "enable": "ok"}}

    async def set_roamlink_carrier_and_reconnect(
        self, mcc: str, mnc: str, name: str,
    ) -> dict:
        self.calls.append(("carrier", (mcc, mnc, name), {}))
        return {"carrier": "ok", "reconnect": {"disable": "ok", "enable": "ok"}}

    async def set_cellular_rat_and_reconnect(self, mode: str) -> dict:
        self.calls.append(("rat", (mode,), {}))
        return {"rat": "ok", "reconnect": {"disable": "ok", "enable": "ok"}}

    async def set_sf_profile_enable(self, profile_id: int, enable: bool) -> dict:
        self.calls.append(("sf", (profile_id, enable), {}))
        return {"stat": "ok"}

    async def apply_config(self) -> dict:
        self.calls.append(("apply", (), {}))
        return {"stat": "ok"}


class TestPeplinkRouterControlAPI:
    def _driver_with_stub(self) -> tuple[PeplinkRouterDriver, _StubController]:
        spec = DeviceSpec.from_config(
            "edge",
            {"kind": "peplink_router", "host": "1.1.1.1", "username": "a"},
        )
        drv = PeplinkRouterDriver(spec)
        stub = _StubController()
        drv._controller = stub  # type: ignore[assignment]
        return drv, stub

    def test_set_carrier_auto_dispatches_to_auto_reconnect(self) -> None:
        import asyncio as _asyncio
        drv, stub = self._driver_with_stub()
        _asyncio.run(drv.set_carrier("auto"))
        assert [c[0] for c in stub.calls] == ["auto"]

    def test_set_carrier_verizon_translates_plmn(self) -> None:
        import asyncio as _asyncio
        drv, stub = self._driver_with_stub()
        _asyncio.run(drv.set_carrier("Verizon"))  # case-insensitive
        assert stub.calls[0][0] == "carrier"
        mcc, mnc, name = stub.calls[0][1]
        assert (mcc, mnc, name) == ("311", "480", "Verizon")

    def test_set_carrier_unknown_raises(self) -> None:
        import asyncio as _asyncio
        drv, _ = self._driver_with_stub()
        with pytest.raises(ValueError, match="Unknown carrier"):
            _asyncio.run(drv.set_carrier("sprint"))

    def test_set_rat_valid_passes_through(self) -> None:
        import asyncio as _asyncio
        drv, stub = self._driver_with_stub()
        _asyncio.run(drv.set_rat("LTE"))
        assert stub.calls[0] == ("rat", ("LTE",), {})

    def test_set_rat_invalid_raises(self) -> None:
        import asyncio as _asyncio
        drv, _ = self._driver_with_stub()
        with pytest.raises(ValueError, match="Invalid mode"):
            _asyncio.run(drv.set_rat("6G"))

    def test_set_sf_enable_toggles_and_applies(self) -> None:
        import asyncio as _asyncio
        drv, stub = self._driver_with_stub()
        _asyncio.run(drv.set_sf_enable(False, profile_id=1))
        names = [c[0] for c in stub.calls]
        assert names == ["sf", "apply"]
        assert stub.calls[0][1] == (1, False)

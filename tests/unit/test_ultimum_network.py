"""Network allocation and SSH access regressions without cloud mutations."""

import copy
from unittest import mock

from tests.unit import test_ultimum_runner as runner


# isort: split
# RunnerCase installs the repository's standalone runner on sys.path.
from ultimum_validation import config  # noqa: E402
from ultimum_validation import external  # noqa: E402
from ultimum_validation.cloud import APIError  # noqa: E402
from ultimum_validation.cloud import Cloud  # noqa: E402


class ExternalPoolTest(runner.RunnerCase):
    def setUp(self):
        super().setUp()
        self.cfg["network"]["external_ip_pool"].update(
            start="92.119.67.128", end="92.119.67.250"
        )
        self.subnet = {
            "id": "public-subnet",
            "name": "public-v4",
            "network_id": "external",
            "cidr": "92.119.67.0/24",
            "gateway_ip": "92.119.67.1",
        }
        self.cloud.list.return_value = [self.subnet]
        self.pool = external.resolve_pool(self.cfg, self.cloud, "external")
        self.runtime["external_ip_pool"] = self.pool
        self.admin.list.return_value = []

    def occupied(self, *addresses):
        return [
            {
                "fixed_ips": [
                    {"subnet_id": "public-subnet", "ip_address": address}
                    for address in addresses
                ]
            }
        ]

    def test_config_rejects_incomplete_reversed_ipv6_and_conflicting_pool(
        self,
    ):
        for values in (
            {"start": None},
            {"end": None},
            {"start": "92.119.67.251"},
            {"start": "2001:db8::1", "end": "2001:db8::2"},
            {"start": None, "end": None, "subnet": "public-subnet"},
        ):
            with self.subTest(values=values):
                cfg = copy.deepcopy(self.cfg)
                cfg["network"]["external_ip_pool"].update(values)
                with self.assertRaises(config.InvalidError):
                    config.validate(cfg)
        self.cfg["network"]["router"].update(
            external_subnet="public-subnet", external_fixed_ip="92.119.67.5"
        )
        with self.assertRaisesRegex(config.InvalidError, "outside"):
            config.validate(self.cfg)
        # A selected existing router may keep a gateway outside the test pool.
        self.cfg["create_router"] = False
        config.validate(self.cfg)

    def test_resolves_only_matching_external_subnet(self):
        other = dict(self.subnet, id="unrelated", cidr="198.51.100.0/24")
        self.cloud.list.return_value = [other, self.subnet]
        self.assertEqual(
            self.pool, external.resolve_pool(self.cfg, self.cloud, "external")
        )
        self.cloud.list.assert_called_with(
            "network", "/subnets", "subnets", network_id="external"
        )
        self.cfg["network"]["external_ip_pool"]["subnet"] = "unrelated"
        with self.assertRaisesRegex(config.InvalidError, "outside"):
            external.resolve_pool(self.cfg, self.cloud, "external")

    def test_ambiguous_subnet_requires_explicit_selection(self):
        self.cloud.list.return_value = [
            self.subnet,
            dict(self.subnet, id="two"),
        ]
        with self.assertRaisesRegex(
            config.InvalidError, "one external subnet"
        ):
            external.resolve_pool(self.cfg, self.cloud, "external")
        self.cfg["network"]["external_ip_pool"]["subnet"] = "two"
        self.assertEqual(
            "two",
            external.resolve_pool(self.cfg, self.cloud, "external")[
                "subnet_id"
            ],
        )

    def test_both_boundaries_are_inclusive_and_used_ips_are_skipped(self):
        create = mock.Mock(return_value="created")
        self.assertEqual(
            "created",
            external.allocate(self.pool, self.admin, create, mock.Mock()),
        )
        create.assert_called_once_with("92.119.67.128", "public-subnet")
        create.reset_mock()
        self.admin.list.return_value = self.occupied(
            *(f"92.119.67.{i}" for i in range(128, 250))
        )
        external.allocate(self.pool, self.admin, create, mock.Mock())
        create.assert_called_once_with("92.119.67.250", "public-subnet")

    def test_exhaustion_never_uses_outside_pool_or_automatic_ipam(self):
        self.admin.list.return_value = self.occupied(
            *(f"92.119.67.{i}" for i in range(128, 251))
        )
        with self.assertRaisesRegex(
            config.InvalidError, "No free external IP"
        ):
            self.engine().floating("tenant-port")
        self.cloud.post.assert_not_called()
        self.admin.post.assert_not_called()

    def test_reserved_addresses_are_never_selected(self):
        self.cfg["network"]["external_ip_pool"].update(
            start="92.119.67.0", end="92.119.67.255"
        )
        pool = external.resolve_pool(self.cfg, self.cloud, "external")
        create = mock.Mock()
        external.allocate(pool, self.admin, create, mock.Mock())
        create.assert_called_once_with("92.119.67.2", "public-subnet")
        for address in ("92.119.67.0", "92.119.67.1", "92.119.67.255"):
            self.assertFalse(external.contains(pool, address))

    def test_only_known_ip_conflict_retries(self):
        create = mock.Mock(
            side_effect=[
                APIError("network", 409, error_type="IpAddressInUse"),
                "created",
            ]
        )
        self.assertEqual(
            "created",
            external.allocate(self.pool, self.admin, create, mock.Mock()),
        )
        self.assertEqual(
            [
                mock.call("92.119.67.128", "public-subnet"),
                mock.call("92.119.67.129", "public-subnet"),
            ],
            create.call_args_list,
        )
        for failure in (
            APIError("network", 409, error_type="OverQuota"),
            APIError("network", 409),
            APIError("network", 403),
            TimeoutError(),
        ):
            create = mock.Mock(side_effect=failure)
            with self.assertRaises(type(failure)):
                external.allocate(self.pool, self.admin, create, mock.Mock())
            create.assert_called_once_with("92.119.67.128", "public-subnet")

    def test_explicit_gateway_address_never_replaced_by_another(self):
        create = mock.Mock(
            side_effect=APIError("network", 409, error_type="IpAddressInUse")
        )
        with self.assertRaisesRegex(config.InvalidError, "requested"):
            external.allocate(
                self.pool,
                self.admin,
                create,
                mock.Mock(),
                requested="92.119.67.200",
            )
        create.assert_called_once_with("92.119.67.200", "public-subnet")

    def test_vm_and_vip_fips_use_exact_external_ip_as_test_user(self):
        def create(service, path, body):
            self.assertEqual(("network", "/floatingips"), (service, path))
            return {"floatingip": dict(body["floatingip"], id="fip")}

        self.cloud.post.side_effect = create
        for internal_port in ("vm-port", "octavia-vip-port"):
            self.engine().floating(internal_port)
            values = self.cloud.post.call_args.args[2]["floatingip"]
            self.assertEqual("external", values["floating_network_id"])
            self.assertEqual("public-subnet", values["subnet_id"])
            self.assertEqual("92.119.67.128", values["floating_ip_address"])
            self.assertEqual(internal_port, values["port_id"])
        self.admin.post.assert_not_called()
        self.assertEqual(
            "92.119.67.128",
            self.ledger.data["intents"][0]["marker"]["floating_ip_address"],
        )

    def test_fip_permission_error_has_policy_and_no_admin_fallback(self):
        self.cloud.post.side_effect = APIError("network", 403, "req-denied")
        with self.assertRaisesRegex(
            config.InvalidError, "create_floatingip:floating_ip_address"
        ) as ctx:
            self.engine().floating("tenant-port")
        self.assertIn("req-denied", str(ctx.exception))
        self.cloud.post.assert_called_once()
        self.admin.post.assert_not_called()

    def test_rejects_fip_response_outside_pool_but_retains_cleanup_ownership(
        self,
    ):
        self.cloud.post.return_value = {
            "floatingip": {
                "id": "wrong-fip",
                "floating_ip_address": "92.119.67.5",
            }
        }
        with self.assertRaisesRegex(AssertionError, "did not honor"):
            self.engine().floating("tenant-port")
        self.assertEqual("wrong-fip", self.ledger.data["resources"][0]["id"])

    def test_missing_resolved_pool_cannot_silently_allocate(self):
        self.runtime.pop("external_ip_pool")
        with self.assertRaisesRegex(config.InvalidError, "prepare again"):
            self.engine().floating("tenant-port")
        self.cloud.post.assert_not_called()

    def test_unconfigured_pool_preserves_neutron_automatic_allocation(self):
        self.cfg["network"]["external_ip_pool"].update(start=None, end=None)
        self.runtime["external_ip_pool"] = None
        self.assertIsNone(
            external.resolve_pool(self.cfg, self.cloud, "external")
        )
        self.cloud.post.return_value = {
            "floatingip": {"id": "fip", "floating_ip_address": "92.119.67.5"}
        }
        self.engine().floating("tenant-port")
        values = self.cloud.post.call_args.args[2]["floatingip"]
        self.assertNotIn("floating_ip_address", values)
        self.assertNotIn("subnet_id", values)
        self.admin.list.assert_not_called()

    def test_neutron_error_type_is_available_without_exposing_response_body(
        self,
    ):
        session = mock.Mock()
        response = session.request.return_value
        response.status_code = 409
        response.headers = {"x-openstack-request-id": "req-conflict"}
        response.json.return_value = {
            "NeutronError": {
                "type": "IpAddressInUse",
                "message": "private",
                "detail": "secret",
            }
        }
        cloud = Cloud(session, {})
        cloud.endpoints["network"] = "https://example.invalid/v2.0"
        with self.assertRaises(APIError) as ctx:
            cloud.post("network", "/floatingips", {})
        self.assertEqual("IpAddressInUse", ctx.exception.error_type)
        self.assertIn("req-conflict", str(ctx.exception))
        self.assertNotIn("private", str(ctx.exception))
        self.assertNotIn("secret", str(ctx.exception))


class SSHAccessTest(runner.RunnerCase):
    def network_api(self):
        records = {"security_group": {}, "port": {}, "security_group_rule": []}

        def create(service, path, body):
            kind, values = next(iter(body.items()))
            obj = dict(values, id=kind + "-id")
            if kind == "security_group_rule":
                records[kind].append(obj)
            else:
                records[kind] = obj
            return {kind: copy.deepcopy(obj)}

        def get(service, path):
            if path.startswith("/ports/"):
                return {"port": copy.deepcopy(records["port"])}
            return {
                "security_group": dict(
                    records["security_group"],
                    security_group_rules=copy.deepcopy(
                        records["security_group_rule"]
                    ),
                )
            }

        self.cloud.post.side_effect = create
        self.cloud.get.side_effect = get
        engine = self.engine()
        engine.access()
        port = engine.port()
        records["port"]["device_id"] = "server"
        records["port"]["port_security_enabled"] = True
        return engine, port, records

    def test_ssh_ingress_rule_and_port_binding_with_automatic_tenant_ip(self):
        for ssh_port, cidr in ((22, "192.0.2.1/32"), (2222, "0.0.0.0/0")):
            with self.subTest(ssh_port=ssh_port, cidr=cidr):
                self.cfg["guest"]["ssh_port"] = ssh_port
                self.cfg["network"]["probe_source_cidr"] = cidr
                engine, port, records = self.network_api()
                rule = records["security_group_rule"][0]
                self.assertEqual("tcp", rule["protocol"])
                self.assertEqual("ingress", rule["direction"])
                self.assertEqual(cidr, rule["remote_ip_prefix"])
                self.assertEqual(ssh_port, rule["port_range_min"])
                self.assertEqual(ssh_port, rule["port_range_max"])
                self.assertEqual([engine.ssh_group], port["security_groups"])
                self.assertEqual([{"subnet_id": "subnet"}], port["fixed_ips"])
                if cidr == "0.0.0.0/0":
                    rule["remote_ip_prefix"] = None  # Neutron normalization.
                    rule["protocol"] = "6"
                engine.verify_ssh_access({"id": "server"}, port)
                self.assertEqual(
                    [],
                    self.ledger.data["evidence"][
                        "ssh_access_server_before_ssh"
                    ]["errors"],
                )

    def test_bad_sg_or_port_is_reported_with_evidence_before_ssh(self):
        changes = (
            ("security_groups", [], "missing"),
            ("device_id", "other-server", "expected VM"),
            ("port_security_enabled", False, "disabled"),
        )
        for key, value, error in changes:
            engine, port, records = self.network_api()
            records["port"][key] = value
            with self.assertRaisesRegex(AssertionError, error):
                engine.verify_ssh_access({"id": "server"}, port)
            self.assertTrue(
                self.ledger.data["evidence"]["ssh_access_server_before_ssh"][
                    "errors"
                ]
            )
        for key, value in (
            ("direction", "egress"),
            ("ethertype", "IPv6"),
            ("protocol", "udp"),
            ("port_range_min", 80),
            ("remote_ip_prefix", "198.51.100.1/32"),
            ("remote_group_id", "other-sg"),
        ):
            engine, port, records = self.network_api()
            records["security_group_rule"][0][key] = value
            with self.assertRaisesRegex(AssertionError, "no configured IPv4"):
                engine.verify_ssh_access({"id": "server"}, port)

    def test_live_migration_checks_sg_before_ssh_and_after_migration(self):
        engine, port, records = self.network_api()
        engine.options = self.cfg["scenarios"]["nova-live-migration"]
        engine.server = mock.Mock(return_value={"id": "server"})
        engine.floating = mock.Mock(return_value={"id": "fip"})
        guest = mock.Mock()
        order = []
        engine.connect = mock.Mock(
            side_effect=lambda *args: (order.append("ssh"), guest)[1]
        )
        engine.migrate = mock.Mock(
            side_effect=lambda *args: order.append("migrate")
        )
        verify = engine.verify_ssh_access

        def check(server, port, phase="before_ssh"):
            verify(server, port, phase)
            order.append(phase)

        engine.verify_ssh_access = check
        engine.port = mock.Mock(return_value=port)
        with mock.patch(
            "ultimum_validation.scenarios.Continuity"
        ) as continuity:
            engine.nova_live_migration()
        continuity.assert_called_once_with(guest, engine.options, self.ledger)
        self.assertEqual(
            ["before_ssh", "ssh", "migrate", "after_migration"], order
        )
        guest.command.assert_called_once_with("true")

        records["security_group_rule"].clear()
        engine.connect.reset_mock()
        engine.migrate.reset_mock()
        with self.assertRaisesRegex(AssertionError, "no configured IPv4"):
            engine.nova_live_migration()
        engine.connect.assert_not_called()
        engine.migrate.assert_not_called()

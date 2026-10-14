# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

import functools
import importlib.util
import pathlib
from unittest import mock

from rally import exceptions

from tests.unit import test


PLUGIN = (pathlib.Path(__file__).resolve().parents[2]
          / "ultimum" / "plugins" / "project_users.py")


@functools.cache
def load_plugin():
    """Import the plugin with "<name>_under_test" plugin names.

    users@openstack, nova_flavor and glance_image stay the upstream plugins
    in this test process.
    """
    spec = importlib.util.spec_from_file_location(
        "ultimum_project_users_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    # Imported here: tests.unit.test registers the [openstack] options first.
    from rally.common.plugin import plugin

    from rally_openstack.task import context
    from rally_openstack.task import types
    from rally_openstack.task.contexts.keystone import users

    configure_context = context.configure
    configure_plugin = plugin.configure

    def rename_context(name, **kwargs):
        return configure_context(name=name + "_under_test", **kwargs)

    def rename_plugin(name, **kwargs):
        # context.configure also ends up here with the renamed context.
        if not name.endswith("_under_test"):
            name += "_under_test"
        return configure_plugin(name=name, **kwargs)

    with mock.patch.object(users.UserGenerator, "unregister"), \
            mock.patch.object(types.Flavor, "unregister"), \
            mock.patch.object(types.GlanceImage, "unregister"), \
            mock.patch.object(context, "configure", rename_context), \
            mock.patch.object(plugin, "configure", rename_plugin):
        spec.loader.exec_module(module)
    return module


class ProjectUserGeneratorTestCase(test.ScenarioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.module = load_plugin()

    def setUp(self):
        super().setUp()
        osclients = mock.patch.object(self.module, "osclients").start()
        self.addCleanup(mock.patch.stopall)
        self.keystone = osclients.Clients.return_value.keystone
        self.keystone.get_domain_id.return_value = "dom-id"
        self.keystone.list_projects.return_value = [
            mock.Mock(id="other", domain_id="dom-id"),
            mock.Mock(id="p-id", domain_id="dom-id"),
            mock.Mock(id="elsewhere", domain_id="other-dom"),
        ]
        for project, name in zip(self.keystone.list_projects.return_value,
                                 ("other-project", "rally-az", "rally-az")):
            project.name = name
        self.context.update({
            "config": {"users_under_test": {
                "project": {"name": "rally-az", "domain": "Dom"},
                "users_per_tenant": 2,
                "resource_management_workers": 1,
            }},
            "env": {"platforms": {"openstack": {
                "admin": {"username": "admin", "password": "secret",
                          "auth_url": "https://example.com",
                          "project_domain_name": "Default",
                          "user_domain_name": "Default"},
                "users": [],
            }}},
            "task": {"uuid": "task-id", "deployment_uuid": "env"},
        })

    def test_schema_accepts_project(self):
        self.module.ProjectUserGenerator.validate(
            "users_under_test", self.context, None,
            {"project": {"name": "rally-az"}, "users_per_tenant": 3},
            vtype="syntax")

    def test_project_forces_one_tenant_in_its_domain(self):
        generator = self.module.ProjectUserGenerator(self.context)
        self.assertEqual(1, generator.config["tenants"])
        self.assertEqual("Dom", generator.config["project_domain"])

    def test_more_tenants_are_rejected(self):
        self.context["config"]["users_under_test"]["tenants"] = 2
        self.assertRaises(exceptions.ContextSetupFailure,
                          self.module.ProjectUserGenerator, self.context)

    def test_existing_project_is_used_and_never_created(self):
        generator = self.module.ProjectUserGenerator(self.context)
        tenants = generator._create_tenants(1)
        self.assertEqual(
            {"p-id": {"id": "p-id", "name": "rally-az", "users": []}},
            tenants)
        self.keystone.get_domain_id.assert_called_once_with("Dom")
        self.assertFalse(self.keystone.create_project.called)

    def test_missing_project_fails(self):
        config = self.context["config"]["users_under_test"]
        config["project"]["name"] = "missing"
        generator = self.module.ProjectUserGenerator(self.context)
        self.assertRaises(exceptions.ContextSetupFailure,
                          generator._create_tenants, 1)
        self.assertFalse(self.keystone.create_project.called)

    def test_missing_domain_fails(self):
        self.keystone.get_domain_id.side_effect = (
            exceptions.GetResourceNotFound(resource="Dom"))
        generator = self.module.ProjectUserGenerator(self.context)
        self.assertRaises(exceptions.ContextSetupFailure,
                          generator._create_tenants, 1)

    def test_cleanup_deletes_only_users(self):
        generator = self.module.ProjectUserGenerator(self.context)
        generator._delete_users = mock.Mock()
        generator._delete_tenants = mock.Mock()
        generator._remove_default_security_group = mock.Mock()
        generator.cleanup()
        generator._delete_users.assert_called_once_with()
        self.assertFalse(generator._delete_tenants.called)
        self.assertFalse(generator._remove_default_security_group.called)

    def test_without_project_behaves_like_upstream(self):
        del self.context["config"]["users_under_test"]["project"]
        generator = self.module.ProjectUserGenerator(self.context)
        upstream = self.module.users.UserGenerator
        with mock.patch.object(upstream, "_create_tenants") as create, \
                mock.patch.object(upstream, "cleanup") as cleanup:
            generator._create_tenants(1)
            generator.cleanup()
        create.assert_called_once_with(1)
        cleanup.assert_called_once_with()

    def test_project_from_environment_when_config_has_none(self):
        # Rally's validation context is {"users@openstack": {}}.
        self.context["config"]["users_under_test"] = {}
        with mock.patch.dict(self.module.os.environ, {
                "ULTIMUM_RALLY_PROJECT_NAME": "rally-az",
                "ULTIMUM_RALLY_PROJECT_DOMAIN": "Dom"}):
            generator = self.module.ProjectUserGenerator(self.context)
        self.assertEqual({"name": "rally-az", "domain": "Dom"},
                         generator.config["project"])
        self.assertEqual("Dom", generator.config["project_domain"])
        self.assertEqual(
            {"p-id": {"id": "p-id", "name": "rally-az", "users": []}},
            generator._create_tenants(1))
        self.assertFalse(self.keystone.create_project.called)

    def test_config_project_wins_over_environment(self):
        with mock.patch.dict(self.module.os.environ, {
                "ULTIMUM_RALLY_PROJECT_NAME": "other"}):
            generator = self.module.ProjectUserGenerator(self.context)
        self.assertEqual("rally-az", generator.config["project"]["name"])

    def test_without_project_and_environment_behaves_like_upstream(self):
        self.context["config"]["users_under_test"] = {}
        with mock.patch.dict(self.module.os.environ, clear=True):
            generator = self.module.ProjectUserGenerator(self.context)
        self.assertNotIn("project", generator.config)


class UsersFirstResourceTypeTestCase(test.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.module = load_plugin()

    def setUp(self):
        super().setUp()
        self.clients = mock.patch.object(
            self.module.osclients, "Clients").start()
        self.addCleanup(mock.patch.stopall)
        self.nova = self.clients.return_value.nova.return_value
        flavor = mock.Mock(id="private-id")
        flavor.name = "pc1.1c2g"
        self.nova.flavors.list.return_value = [flavor]

    def resolve(self, context):
        processor = self.module.ProjectFlavor(context, scenario_cls=None)
        return processor.pre_process(resource_spec={"name": "pc1.1c2g"},
                                     config={"type": "nova_flavor"},
                                     output_type=str)

    def test_flavor_is_resolved_as_the_workload_user(self):
        context = {"admin": {"credential": "admin-cred"},
                   "users": [{"credential": "user-cred"}]}
        self.assertEqual("private-id", self.resolve(context))
        self.clients.assert_called_with("user-cred")

    def test_admin_is_used_without_users(self):
        self.assertEqual("private-id",
                         self.resolve({"admin": {"credential": "admin-cred"}}))
        self.clients.assert_called_with("admin-cred")

    def test_image_type_is_replaced_too(self):
        self.assertTrue(issubclass(self.module.ProjectGlanceImage,
                                   self.module.UsersFirstResourceType))
        self.assertEqual("glance_image_under_test",
                         self.module.ProjectGlanceImage.get_name())

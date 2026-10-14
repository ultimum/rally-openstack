# ruff: noqa: E402
"""Rally ``users`` context that keeps every workload in one existing project.

This module replaces the upstream ``users@openstack`` context with a subclass.
Without the ``project`` option it behaves exactly like the upstream context.
With it, the context creates no project: it looks up the given project, creates
the temporary users requested by ``users_per_tenant`` inside it and deletes
only those users at cleanup. The project, its default security group and
everything else in it are left untouched.
"""

import copy

from rally import plugins


# Rally loads --plugin-paths before the installed plugins, but the upstream
# modules imported below read the [openstack] options at import time.
plugins.load()

from rally import exceptions
from rally.common import validation

from rally_openstack.common import osclients
from rally_openstack.task import context
from rally_openstack.task.contexts.keystone import users


PROJECT = {
    "type": "object",
    "description": "Existing project for all users of the workload. No "
                   "project is created or deleted; 'tenants' must be 1.",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "domain": {"type": "string", "minLength": 1},
    },
    "required": ["name"],
    "additionalProperties": False,
}


def _schema():
    schema = copy.deepcopy(users.UserGenerator.CONFIG_SCHEMA)
    schema["anyOf"][0]["properties"]["project"] = PROJECT
    return schema


# Only one plugin may own the "users@openstack" name.
users.UserGenerator.unregister()


@validation.add("required_platform", platform="openstack", users=True)
@context.configure(name="users", platform="openstack", order=100)
class ProjectUserGenerator(users.UserGenerator):
    """Creates temporary users in one existing project or, without the
    'project' option, temporary projects and users like upstream."""

    CONFIG_SCHEMA = _schema()

    def __init__(self, context):
        super().__init__(context)
        project = self.config.get("project")
        if project and not self.existing_users:
            if self.config.get("tenants", 1) != 1:
                raise exceptions.ContextSetupFailure(
                    ctx_name=self.get_name(),
                    msg="'tenants' must be 1 when 'project' is set.")
            with self.config.unlocked():
                self.config["tenants"] = 1
                self.config["project_domain"] = project.get("domain",
                                                            "Default")

    def _create_tenants(self, threads):
        project = self.config.get("project")
        if not project:
            return super()._create_tenants(threads)

        keystone = osclients.Clients(self.credential).keystone
        domain = project.get("domain", "Default")
        try:
            domain_id = keystone.get_domain_id(domain)
        except exceptions.GetResourceNotFound:
            raise exceptions.ContextSetupFailure(
                ctx_name=self.get_name(),
                msg="Project domain '%s' was not found." % domain) from None
        found = [p for p in keystone.list_projects()
                 if p.name == project["name"] and p.domain_id == domain_id]
        if len(found) != 1:
            raise exceptions.ContextSetupFailure(
                ctx_name=self.get_name(),
                msg="Project '%s' was not found in domain '%s'; it is "
                    "never created by Rally." % (project["name"], domain))
        return {found[0].id: {"id": found[0].id, "name": found[0].name,
                              "users": []}}

    def cleanup(self):
        if self.config.get("project") and not self.existing_users:
            # The project is not ours: remove only the users created here.
            self._delete_users()
            self.context["tenants"] = {}
        else:
            super().cleanup()

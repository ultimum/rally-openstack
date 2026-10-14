=========================
Ultimum AZ validation task
=========================

``../az-production.yaml`` runs the Barbican, Cinder, Heat, Nova and Octavia
workloads as one Rally task. Every server, volume, volume attachment, Heat
stack server/volume and Octavia load balancer is created in the availability
zone you choose. The task is independent of ``<service>-production.yaml`` and
of ``rally-jobs/``; those files are unchanged.

Project and users
=================

All workloads run in **one existing project** that you define. Rally never
creates or deletes a project. Each workload creates the temporary users it
needs (``users_per_tenant``, role ``member``) inside that project and deletes
only those users afterwards; the project, its default security group and
other resources in it are left alone. Resources created by the workloads are
removed by Rally's cleanup, which matches only names generated for the task.

Set the project in ``/etc/ultimum/ultimum.yaml``::

    az_validation:
      project:
        name: rally-az
        domain: Default

With ``name: null`` the wrapper uses ``identity.project`` of the acceptance
runner. ``RALLY_PROJECT_NAME`` / ``RALLY_PROJECT_DOMAIN`` override the YAML;
``RALLY_ULTIMUM_CONFIG`` selects another YAML file. Without a project the
wrapper stops before starting Rally. Use the domain name, not its ID.

Admin-only APIs (aggregates, flavors, hypervisors, services, volume types,
QoS, encryption types, quotas, role assignment) still use the admin
credentials of the active Rally deployment created from ``admin.rc``.
Barbican scenarios are upstream admin scenarios: their secrets, containers
and orders are created in the admin's project, not in the test project.

``Quotas.*_update`` and ``Quotas.*_update_and_delete`` change and then reset
the quotas of the test project, so they only run with
``RALLY_PROJECT_QUOTA_TESTS=true``. The ``Quotas.*_get`` workloads always run.
Users created in the project need to fit into its quotas while workloads run.

Run it
======

Inside the container, with an active Rally deployment::

    ultimum-rally-az-verification

For example::

    RALLY_AVAILABILITY_ZONE=cz01 RALLY_FLAVOR_NAME=m1.small \
        ultimum-rally-az-verification

    RALLY_SERVICES=nova,cinder RALLY_VOLUME_TYPE=ssd \
        ultimum-rally-az-verification

The wrapper validates and starts the task once, passing
``--plugin-paths`` with ``ultimum/plugins/availability_zone.py`` and
``ultimum/plugins/project_users.py``, and returns
the exit code of ``rally task start``. It prints the commands for inspecting
the task and generating its HTML report.

Configuration
=============

``RALLY_SERVICES``
    Comma-separated parts to run, in order. Default:
    ``barbican,cinder,heat,nova,octavia``.

``RALLY_AVAILABILITY_ZONE``
    Nova AZ of every server. Default: ``cz03``.

``RALLY_VOLUME_AVAILABILITY_ZONE``
    Cinder AZ of every volume, including volumes attached to servers, boot
    volumes, restore targets and Heat volumes. Defaults to
    ``RALLY_AVAILABILITY_ZONE``. Set it to an empty string to keep the Cinder
    default.

``RALLY_OCTAVIA_AVAILABILITY_ZONE``
    Octavia availability zone of every load balancer. Defaults to
    ``RALLY_AVAILABILITY_ZONE``; it must exist in Octavia
    (``openstack loadbalancer availability zone list``). Set it to an empty
    string to keep the Octavia default.

``RALLY_NETWORK_AVAILABILITY_ZONE``
    Optional ``availability_zone_hints`` value for the tenant networks.
    Default: empty (no hint).

``RALLY_IMAGE_NAME`` / ``RALLY_FLAVOR_NAME`` / ``RALLY_RESIZE_FLAVOR_NAME``
    Image name pattern, flavor and Nova resize target. Defaults:
    ``^Cirros.*``, ``m1.tiny`` and ``m1.medium``.

``RALLY_VOLUME_TYPE``
    Volume type of every volume. Default: ``cz03``. An empty string keeps
    the Cinder default type.

``RALLY_VOLUME_BACKEND_NAME``
    Backend name used by the volume type keys workload. Default:
    ``huawei-fc``.

``RALLY_CINDER_BACKUP``
    JSON boolean; ``true`` adds the Cinder backup workloads. Default:
    ``false``.

``RALLY_FLOATING_NETWORK``
    External network UUID for the two Nova floating IP workloads. Defaults
    to ``ab983650-138d-449b-8782-c91861e65fd0``; an empty string skips them.

``RALLY_HEAT_IMAGE``
    Image file or URL uploaded per tenant for the Heat stacks with servers
    (Heat needs an exact image name, not a pattern). Defaults to the CirrOS
    QCOW2 prepared by the Glance wrapper in ``$RALLY_DATA_DIR/extra``, or the
    CirrOS download URL when that file does not exist. ``CIRROS_VERSION``
    selects the version (default ``0.6.3``).

``RALLY_DATA_DIR`` / ``RALLY_TASK_DIR`` / ``RALLY_AZ_PLUGIN_DIR``
    Data directory (``/data``), task root (``/opt/rally-openstack/tasks``) and
    the directory with ``availability_zone.py`` and ``project_users.py``
    (``/opt/ultimum/plugins``; a checkout falls back to ``ultimum/plugins``).

Plugins
=======

``users@openstack`` (``project_users.py``)
    Replaces the upstream ``users`` context while the plugin is loaded. Its
    new ``project`` option selects the existing project described above;
    without that option it behaves exactly like upstream.

Upstream plugins that cannot place their resources are replaced by the
plugins in ``ultimum/plugins/availability_zone.py``. Subtask titles stay the
same as in ``rally-jobs``:

``volumes@ultimum`` / ``servers@ultimum``
    The ``volumes`` and ``servers`` contexts with an ``availability_zone``
    option.

``UltimumCinderVolumes.create_snapshot_and_attach_volume``
    Takes ``create_volume_kwargs`` for the attached volume.

``UltimumCinderVolumes.create_and_restore_volume_backup``
    Restores into a volume created in the volume AZ; Cinder would otherwise
    create the restore target in its default zone.

``UltimumNovaAggregates.create_aggregate_add_and_remove_host`` /
``UltimumNovaAggregates.create_aggregate_add_host_and_boot_server``
    Create the aggregate in the AZ and use an enabled compute host of that
    AZ. The boot variant requires ``AggregateInstanceExtraSpecsFilter``.

``UltimumOctavia.*``
    The nine Octavia scenarios with an ``availability_zone`` argument.

Heat templates with servers and volumes are in ``heat/``; the others are
copies of the ``rally-jobs/extra`` templates.

Building changes
================

``ultimum/Dockerfile`` already copies ``tasks/ultimum/``, ``ultimum/plugins``
and the ``ultimum-*`` wrappers, so rebuilding the image includes this task.

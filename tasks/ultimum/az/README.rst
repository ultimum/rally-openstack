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
other resources in it are left alone. This also applies to the users Rally
creates for task validation (flavor, image and volume type checks): the
wrapper passes the project to Rally as ``ULTIMUM_RALLY_PROJECT_NAME`` /
``ULTIMUM_RALLY_PROJECT_DOMAIN``. Flavors, images and volume types used by the
task must therefore be visible to this project (public, or with project
access). Resources created by the workloads are
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
admin credentials from ``admin.rc`` (see *Admin environment* below).
``BarbicanOrders.create_certificate_and_delete`` from ``rally-jobs`` is not
included: certificate orders were removed from Barbican in 2024.1 (18.0.0)
and from python-barbicanclient in 7.0.0, so the scenario always fails with
``'OrderManager' object has no attribute 'create_certificate'``.
Barbican scenarios are upstream admin scenarios: their secrets, containers
and orders are created in the admin's project, not in the test project.

``Quotas.*_update`` and ``Quotas.*_update_and_delete`` change and then reset
the quotas of the test project, so they only run with
``RALLY_PROJECT_QUOTA_TESTS=true``. The ``Quotas.*_get`` workloads always run.
Users created in the project need to fit into its quotas while workloads run.

Run it
======

Inside the container::

    ultimum-rally-az-verification

Settings come from ``az_validation`` in ``/etc/ultimum/ultimum.yaml``;
environment variables override them for one run, for example::

    RALLY_AVAILABILITY_ZONE=cz01 RALLY_FLAVOR_NAME=m1.small \
        ultimum-rally-az-verification

    RALLY_SERVICES=nova,cinder RALLY_VOLUME_TYPE=ssd \
        ultimum-rally-az-verification

The wrapper starts the task once (``rally task start`` validates it before
running any workload, so there is no separate ``rally task validate``), passing
``--plugin-paths`` with ``ultimum/plugins/availability_zone.py``,
``ultimum/plugins/project_users.py``,
``ultimum/plugins/boot_from_volume.py`` and
``ultimum/plugins/connect_retries.py``, and returns
the exit code of ``rally task start``. It prints the commands for inspecting
the task and generating its HTML report.

Boot from volume
================

Every server boots from a volume, since flavors without a local disk refuse
image-backed servers. ``ultimum/plugins/boot_from_volume.py`` wraps the two
methods all Nova and Cinder scenarios and the ``servers`` context boot
through: before each boot it creates a volume from the image with
``boot_volume_size`` (raised to what the image needs), ``volume_type`` and
``volume_availability_zone``, and boots from it with
``delete_on_termination``. Workloads that bring their own block device
mapping (``boot_server_from_volume*``) are unchanged. Heat stacks boot from
volumes created by their templates (``heat/boot_server.yaml``).

Exceptions and side effects:

* ``boot_and_delete_multiple_servers`` boots several servers per request,
  which cannot share one volume; Nova creates their root volumes from the
  image with Cinder's default type, in the zone Nova chooses.
* Snapshots of volume-backed servers also snapshot the root volume in
  Cinder. When ``snapshot_server`` deletes its image, the plugin deletes
  those volume snapshots and the then unused root volume as well.
* ``boot_and_bounce_server`` uses compute API 2.87 to rescue a volume-backed
  server.

Admin environment
=================

The wrapper does not use the active Rally deployment. It builds its own
Rally environment from the admin OpenRC (``admin.openrc`` in ``ultimum.yaml``,
or ``RALLY_OPENRC``) with the ``tls`` section of ``ultimum.yaml`` applied, the
same way ``ultimum-rally`` does, so ``tls.ca_cert`` / ``tls.insecure`` work
here too. The environment is named ``ultimum-az-<hash>`` after its contents:
it is reused while ``admin.rc`` and ``tls`` stay the same and a new one is
created when they change. It is never made the active deployment.
Credentials are passed to Rally only through a private temporary file.

Configuration
=============

All settings live in the ``az_validation`` section of
``/etc/ultimum/ultimum.yaml``; ``ultimum/ultimum.yaml.example`` documents
every key and its default. The wrapper reads the file with the same loader
as ``ultimum-rally``, so unknown keys or wrong types stop it before Rally
starts. ``RALLY_ULTIMUM_CONFIG`` selects another file.

For one run, the ``RALLY_<KEY>`` environment variable overrides the matching
key (``flavor_name`` -> ``RALLY_FLAVOR_NAME``, ``project.name`` ->
``RALLY_PROJECT_NAME``). A variable set to an empty string also overrides it.
Without the YAML file the defaults below apply. The keys are:

``RALLY_SERVICES``
    Parts to run, in order (YAML list; the variable is comma-separated). Default:
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

``RALLY_BOOT_VOLUME_SIZE``
    Size in GiB of the volume every server boots from; raised to the image's
    ``min_disk``/size when needed. Default: ``1``.

``RALLY_CONNECT_RETRIES``
    How often an API call is retried when its TCP connection cannot be
    opened, e.g. during a short VPN outage (``connect_retries.py``). Only
    connection failures are retried: nothing was sent, so no request runs
    twice. Read timeouts and API errors fail as before. ``0`` disables it.
    Default: ``5`` (waits 4, 8, 16, 32 s between attempts).

``RALLY_CONNECT_TIMEOUT``
    Seconds an API call may take to open its connection (TCP and TLS
    handshake) before it fails and ``RALLY_CONNECT_RETRIES`` retries it from
    a new source port. Waiting for the response keeps Rally's
    ``openstack_client_http_timeout``. ``0`` leaves the connect to the
    kernel's SYN retransmissions (about 2 minutes). Default: ``10``.

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

========================
Ultimum validation tasks
========================

Each service has one standalone task in this directory. Its wrapper validates
and starts that task once, then returns the exit code from ``rally task start``.
The wrappers do not load or run files from ``rally-jobs/``.

The suites preserve the workloads developed in the container. Cinder, Nova
and Neutron use the customized task definitions from that environment. Glance
combines its two previous suites into one task with 20 workloads. Runner and
SLA settings are preserved, including the two Glance import workloads allowing
100% failures; those two workloads do not determine whether validation passes.

Run a suite
===========

Inside the container, with an active Rally deployment and the data volume
mounted at ``/data``::

    ultimum-rally-glance-verification
    ultimum-rally-cinder-verification
    ultimum-rally-nova-verification
    ultimum-rally-neutron-verification

Each command starts only the corresponding ``<service>-production.yaml``.
The scripts print commands for inspecting the task and generating an HTML
report; they do not generate the report automatically.

Configuration
=============

``RALLY_DATA_DIR``
    Data directory, default ``/data``. Glance stores prepared images in its
    ``extra/`` subdirectory.

``RALLY_TASK_DIR``
    Task root, default ``/opt/rally-openstack/tasks``. The wrappers load files
    from its ``ultimum/`` subdirectory.

``RALLY_IMAGE_NAME`` / ``RALLY_FLAVOR_NAME``
    Image name pattern and flavor for Cinder, Nova and Neutron.
    Defaults: ``^Cirros.*`` and ``m1.tiny``.

``RALLY_VOLUME_TYPE`` / ``RALLY_AVAILABILITY_ZONE``
    Volume type and AZ supplied to the configured Cinder, Nova and Neutron
    workloads. Both default to ``cz03``. Workloads retain their individual
    placement settings; not every workload accepts these parameters.

``RALLY_VOLUME_BACKEND_NAME``
    Cinder backend name used by the volume type keys workload.
    Default: ``huawei-fc``.

``RALLY_CINDER_BACKUP``
    Set to the JSON boolean ``true`` to include Cinder backup workloads.
    Default: ``false`` (58 workloads; 65 when enabled).

``RALLY_RESIZE_FLAVOR_NAME``
    Nova resize target flavor. Default: ``m1.small``.

``RALLY_FLOATING_NETWORK``
    External network for the two optional Nova floating IP workloads.
    Default: empty (72 workloads; 74 with a network configured).

``CIRROS_VERSION``
    CirrOS version prepared by the Glance wrapper. Default: ``0.6.3``.
    Local upload workloads use the prepared QCOW2/RAW paths; web-download
    uses the matching HTTPS URL. This setting applies to the Glance wrapper.

Neutron runs 22 workloads, each with one iteration and concurrency one.
All its workloads require a zero failure rate.

For example::

    RALLY_CINDER_BACKUP=true ultimum-rally-cinder-verification
    RALLY_FLOATING_NETWORK=public ultimum-rally-nova-verification

Glance can also be run directly with ``qcow2_image``, ``raw_image`` and
``qcow2_image_url`` task arguments. The first two are local upload paths;
the third must be an HTTP(S) URL accessible to Glance for web-download.

Building changes
================

``ultimum/Dockerfile`` copies this directory and the wrappers from ``ultimum/`` from
the local build context. Changes to these tasks are therefore included when
the image is rebuilt, independently of the Git clone layer used to install
the Rally plugins.

Build from the repository root with::

    docker build -f ultimum/Dockerfile --build-arg GITHUB_TOKEN -t ultimum-rally:local .

The ``GITHUB_TOKEN`` build argument uses the value exported in the shell.
See ``../../ultimum/README.rst`` for the container startup configuration.

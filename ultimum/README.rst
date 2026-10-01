=======================
Ultimum Rally container
=======================

Build the Ultimum image from the repository root using its own Dockerfile::

    docker build -f ultimum/Dockerfile --build-arg GITHUB_TOKEN -t ultimum-rally:local .

Export ``GITHUB_TOKEN`` in the shell before building so Git can clone the
Ultimum Rally repositories. The root Dockerfile belongs to the upstream image;
the repository root is still the build context for ``ultimum/Dockerfile``.

The container uses ``rally-start`` to initialize Rally and then remain running
for commands executed through its console. Mount persistent storage at
``/data`` and an OpenStack RC file at ``/etc/rally/admin.rc``. The Rally database
is stored at ``/data/db/rally.sqlite``.

``RALLY_OPENRC`` changes the RC file location. ``RALLY_DEPLOYMENT_NAME`` changes
the deployment name, which defaults to ``openstack``.

The service validation task definitions and their wrappers are maintained in
``tasks/ultimum/`` and ``ultimum/``. Their configuration is documented in
``tasks/ultimum/README.rst``.

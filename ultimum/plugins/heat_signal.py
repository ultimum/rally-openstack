# ruff: noqa: E402
"""Scale Heat stacks through the Heat API instead of EC2-signed URLs.

``HeatStacks.create_stack_and_scale`` POSTs to the URL in a stack output
without any credentials, so upstream it only works with a scaling policy's
``alarm_url``: a URL pre-signed with EC2 credentials of a Heat stack user and
validated by heat-api-cfn against Keystone ``/v3/ec2tokens``.

Ultimum templates output the policy's ``signal_url`` instead, a Heat API
URL (``.../stacks/<name>/<id>/resources/<resource>/signal``) that needs no
EC2 credentials, only a Keystone token. For such URLs
``HeatScenario._stack_webhook`` signals the resource through heatclient as
the scenario user, over Rally's own Heat endpoint and Keystone session.
EC2-signed URLs (``Signature`` in the query) are still POSTed as upstream
does.
"""

from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urlparse

from rally import plugins


# Rally loads --plugin-paths before the installed plugins, but the upstream
# modules imported below read the [openstack] options at import time.
plugins.load()

from rally import exceptions
from rally.task import atomic

from rally_openstack.task.scenarios.heat import utils as heat_utils


upstream_stack_webhook = heat_utils.HeatScenario._stack_webhook

EC2_SIGNATURES = ("Signature", "X-Amz-Signature")


def heat_signal_target(url):
    """(stack, resource) of a Heat API signal URL, or None for other URLs."""
    parsed = urlparse(url)
    if any(key in parse_qs(parsed.query) for key in EC2_SIGNATURES):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    # .../stacks/<name>/<id>/resources/<resource>/signal
    if (len(parts) < 6 or parts[-1] != "signal"
            or parts[-3] != "resources" or parts[-6] != "stacks"):
        return None
    return "%s/%s" % (parts[-5], parts[-4]), parts[-2]


def stack_webhook(self, stack, output_key):
    for output in stack.outputs:
        if output["output_key"] == output_key:
            url = output["output_value"]
            break
    else:
        raise exceptions.InvalidConfigException(
            "No output key %(key)s found in stack %(id)s" %
            {"key": output_key, "id": stack.id})
    target = heat_signal_target(url or "")
    if target is None:
        return upstream_stack_webhook(self, stack, output_key)
    stack_id, resource = target
    with atomic.ActionTimer(self, "heat.%s_webhook" % output_key):
        self.clients("heat").resources.signal(stack_id, resource)


heat_utils.HeatScenario._stack_webhook = stack_webhook

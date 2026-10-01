"Small REST adapter; authentication and endpoint selection use Keystoneauth."

import re
import time
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from .config import InvalidError
from .config import UnsupportedError


class APIError(Exception):
    def __init__(self, service, status, request_id=""):
        self.status = status
        super().__init__(
            f"{service} API returned HTTP {status} (request {request_id})"
        )


def wait_for(fetch, ready, timeout=600, interval=2, description="operation"):
    end = time.monotonic() + timeout
    while True:
        value = fetch()
        if ready(value):
            return value
        if time.monotonic() >= end:
            raise TimeoutError(f"Timed out waiting for {description}")
        time.sleep(min(interval, max(0, end - time.monotonic())))


def unique(objects, value, kind):
    matches = [
        o
        for o in objects
        if value in (o.get("id"), o.get("uuid"), o.get("name"))
    ]
    if len(matches) != 1:
        raise InvalidError(
            f"Expected one {kind} named/identified {value!r}, "
            f"found {len(matches)}"
        )
    return matches[0]


def session_from_rc(rc, identity=None):
    from keystoneauth1 import session
    from keystoneauth1.identity import v3

    kwargs = {"auth_url": rc["OS_AUTH_URL"]}
    if identity:
        kwargs.update(
            username=identity["user"]["name"],
            password=identity["user"]["password"],
            user_domain_name=identity["user"]["domain"],
            project_name=identity["project"]["name"],
            project_domain_name=identity["project"]["domain"],
        )
    else:
        for key in (
            "username",
            "password",
            "user_domain_name",
            "user_domain_id",
            "project_name",
            "project_id",
            "project_domain_name",
            "project_domain_id",
        ):
            if rc.get("OS_" + key.upper()):
                kwargs[key] = rc["OS_" + key.upper()]
        if not (
            kwargs.get("user_domain_id") or kwargs.get("user_domain_name")
        ):
            kwargs["user_domain_name"] = "Default"
        if not (
            kwargs.get("project_domain_id")
            or kwargs.get("project_domain_name")
        ):
            kwargs["project_domain_name"] = "Default"
    verify = rc.get("OS_CACERT") or True
    if rc.get("OS_INSECURE", "").lower() in ("1", "true", "yes"):
        verify = False
    return session.Session(
        auth=v3.Password(**kwargs), verify=verify, timeout=60
    )


class Cloud:
    def __init__(self, session, rc):
        self.session = session
        self.rc = rc
        self.endpoints = {}
        self.versions = {}

    @property
    def project_id(self):
        return self.session.get_project_id()

    def endpoint(self, service):
        if service not in self.endpoints:
            aliases = {
                "volume": ("volumev3", "block-storage"),
                "lb": ("load-balancer",),
                "ha": ("instance-ha",),
            }.get(service, (service,))
            endpoint = None
            for alias in aliases:
                try:
                    endpoint = self.session.get_endpoint(
                        service_type=alias,
                        interface=self.rc.get(
                            "OS_INTERFACE",
                            self.rc.get("OS_ENDPOINT_TYPE", "public"),
                        ).removesuffix("URL"),
                        region_name=self.rc.get("OS_REGION_NAME") or None,
                    )
                except Exception as exc:
                    if type(exc).__name__ != "EndpointNotFound":
                        raise
                if endpoint:
                    break
            if not endpoint:
                raise UnsupportedError(
                    f"No {service} endpoint in the service catalog"
                )
            endpoint = endpoint.rstrip("/")
            suffix = {
                "network": "/v2.0",
                "image": "/v2",
                "lb": "/v2",
                "ha": "/v1",
                "identity": "/v3",
            }.get(service)
            if suffix and not urlsplit(endpoint).path.rstrip("/").endswith(
                suffix
            ):
                endpoint += suffix
            self.endpoints[service] = endpoint
        return self.endpoints[service]

    def request(
        self, service, method, path, body=None, params=None, version=None
    ):
        url = self.endpoint(service) + path
        headers = {}
        version = version or self.versions.get(service)
        if version:
            headers["OpenStack-API-Version"] = (
                ("volume" if service == "volume" else "compute")
                + " "
                + version
            )
        response = self.session.request(
            url,
            method,
            json=body,
            params=params,
            headers=headers,
            raise_exc=False,
            connect_retries=0,
        )
        if response.status_code >= 400:
            raise APIError(
                service,
                response.status_code,
                response.headers.get("x-openstack-request-id", "unknown"),
            )
        return response.json() if response.content else {}

    def get(self, service, path, **kwargs):
        return self.request(service, "GET", path, **kwargs)

    def post(self, service, path, body):
        return self.request(service, "POST", path, body)

    def delete(self, service, path):
        return self.request(service, "DELETE", path)

    def list(self, service, path, key, **filters):
        "Follow server-provided next links, rejecting a change of authority."
        result, seen = [], set()
        params = filters
        while path:
            if path in seen:
                raise InvalidError("API pagination cycle")
            seen.add(path)
            data = self.get(service, path, params=params)
            result.extend(data[key])
            links = data.get(key + "_links", data.get("links", []))
            next_url = (
                links.get("next")
                if isinstance(links, dict)
                else next(
                    (x["href"] for x in links if x.get("rel") == "next"), None
                )
            )
            next_url = next_url or data.get("next")
            if next_url:
                base = self.endpoint(service)
                if next_url.startswith(base + "/"):
                    path = next_url[len(base) :]
                elif next_url.startswith("/") and not next_url.startswith(
                    "//"
                ):
                    base_path = urlsplit(base).path
                    path = (
                        next_url[len(base_path) :]
                        if next_url.startswith(base_path + "/")
                        else next_url
                    )
                else:
                    raise InvalidError(
                        "Refusing API pagination link outside service endpoint"
                    )
            else:
                path = None
            params = None
        return result

    def require_version(self, service, required):
        endpoint = self.endpoint(service)
        parts = urlsplit(endpoint)
        match = re.search(r"/v[23](?:\.1)?(?:/|$)", parts.path)
        if not match:
            raise UnsupportedError(f"Cannot discover {service} API version")
        discovery = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path[: match.end()].rstrip("/"),
                "",
                "",
            )
        )
        response = self.session.get(discovery, raise_exc=False)
        if response.status_code >= 400:
            raise UnsupportedError(f"Cannot discover {service} API version")
        data = response.json()
        versions = (
            [data["version"]]
            if "version" in data
            else data.get("versions", [])
        )
        if isinstance(versions, dict):
            versions = versions.get("values", [])
        maximum = max(
            (
                tuple(map(int, (v.get("version") or "0.0").split(".")))
                for v in versions
            ),
            default=(0, 0),
        )
        if maximum < tuple(map(int, required.split("."))):
            raise UnsupportedError(
                f"{service} requires API >= {required}; "
                f"available {'.'.join(map(str, maximum))}"
            )
        self.versions[service] = required
        return ".".join(map(str, maximum))

    def wait_status(self, service, path, key, statuses, timeout=600):
        def ready(obj):
            status = obj.get("status", obj.get("provisioning_status"))
            if status == "ERROR" or str(status).startswith("error"):
                raise AssertionError(
                    f"{service} resource {obj.get('id')} entered {status}"
                )
            return status in statuses

        return wait_for(
            lambda: self.get(service, path)[key],
            ready,
            timeout,
            description=path,
        )

    def absent(self, service, path):
        try:
            self.get(service, path)
        except APIError as exc:
            if exc.status == 404:
                return True
            raise
        return False

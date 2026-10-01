"""Operator CLI. No cloud changes occur during list, explain or check."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import uuid

from .cloud import APIError
from .config import SCENARIOS
from .config import InvalidError
from .config import UnsupportedError
from .config import fingerprint
from .config import load
from .config import openrc
from .config import validate
from .prepare import clouds
from .prepare import ensure_identity
from .prepare import preflight
from .prepare import prepare_network
from .prepare import resolve_base
from .prepare import set_quotas
from .progress import progress
from .progress import watch
from .resources import Resources
from .state import Ledger
from .state import project_lock
from .state import timestamp
from .state import write_json


def run_path(cfg, run_id):
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError()
    except ValueError:
        raise InvalidError("run-id must be a canonical UUID") from None
    return (
        pathlib.Path(cfg["execution"]["state_dir"])
        / "runs"
        / run_id
        / "state.json"
    )


def lock_key(cfg, rc):
    value = (
        rc["OS_AUTH_URL"].rstrip("/")
        + "/"
        + cfg["identity"]["project"]["domain"]
        + "/"
        + cfg["identity"]["project"]["name"]
    )
    return hashlib.sha256(value.encode()).hexdigest()


def rally(cfg, *args, capture=True):
    command = [
        "rally",
        "--plugin-paths",
        cfg["rally"]["plugin_dir"],
        *map(str, args),
    ]
    result = subprocess.run(command, capture_output=capture, text=True)
    return result


def register_environment(cfg, rc, runtime):
    # Credentials go only in a private file / Rally DB, never argv or task
    # args.
    identity = cfg["identity"]
    user = {
        "username": identity["user"]["name"],
        "password": identity["user"]["password"],
        "user_domain_name": identity["user"]["domain"],
        "project_name": identity["project"]["name"],
        "project_domain_name": identity["project"]["domain"],
    }
    platform = {
        "auth_url": rc["OS_AUTH_URL"],
        "users": [user],
        "region_name": rc.get("OS_REGION_NAME") or None,
        "endpoint_type": rc.get(
            "OS_INTERFACE", rc.get("OS_ENDPOINT_TYPE", "public")
        ).removesuffix("URL"),
        "https_insecure": rc.get("OS_INSECURE", "").lower()
        in ("1", "true", "yes"),
        "https_cacert": rc.get("OS_CACERT", ""),
    }
    digest = fingerprint(platform)
    state_dir = pathlib.Path(cfg["execution"]["state_dir"])
    saved = state_dir / ("environment-" + digest + ".json")
    if saved.exists():
        env_id = json.loads(saved.read_text())["environment_id"]
        result = rally(cfg, "env", "show", env_id, "--json")
        if result.returncode == 0:
            progress(f"REUSE Rally environment: {env_id}")
            return env_id
    spec_path = state_dir / (".environment-spec-" + uuid.uuid4().hex + ".json")
    try:
        progress("CREATE Rally environment for the configured user/project")
        write_json(spec_path, {"existing@openstack": platform})
        result = rally(
            cfg,
            "env",
            "create",
            "--name",
            cfg["rally"]["deployment_name"] + "-" + digest[:12],
            "--spec",
            spec_path,
            "--json",
            "--no-use",
        )
        if result.returncode:
            raise InvalidError(
                "Rally environment creation failed; check "
                "Rally installation, auth and its database "
                "(credential output suppressed)"
            )
        try:
            data = json.loads(result.stdout)
            env_id = data["uuid"]
        except (ValueError, KeyError):
            raise InvalidError(
                "Rally env create did not return expected JSON"
            ) from None
        write_json(
            saved,
            {"environment_id": env_id, "project_id": runtime["project_id"]},
        )
        return env_id
    finally:
        spec_path.unlink(missing_ok=True)


@watch()
def prepare(cfg, rc, admin, cloud):
    progress("CHECK configuration and local prerequisites")
    validate(cfg)
    for key in ("state_dir", "results_dir"):
        directory = pathlib.Path(cfg["execution"][key])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        progress(f"READY directory: {directory}")
    project_id = ensure_identity(cfg, admin)
    progress("CHECK authentication as the test user in the configured project")
    if cloud.project_id != project_id:
        raise InvalidError(
            "Test user did not authenticate into the configured project"
        )
    set_quotas(cfg, admin, project_id)
    runtime = prepare_network(cfg, cloud, resolve_base(cfg, cloud))
    runtime["auth_url"] = rc["OS_AUTH_URL"]
    runtime["environment_id"] = register_environment(cfg, rc, runtime)
    write_json(
        pathlib.Path(cfg["execution"]["state_dir"])
        / ("prepared-" + lock_key(cfg, rc) + ".json"),
        runtime,
    )
    progress("OK preparation complete")
    return runtime


def publish(cfg, ledger):
    target = (
        pathlib.Path(cfg["execution"]["results_dir"]) / ledger.data["run_id"]
    )
    write_json(target / "result.json", ledger.data)
    return target


def run_one(cfg, config_path, scenario, admin, cloud, runtime):
    run_id = str(uuid.uuid4())
    ledger = Ledger(
        run_path(cfg, run_id),
        dict(
            run_id=run_id,
            scenario=scenario,
            status="PREPARING",
            created_at=timestamp(),
            config_path=str(pathlib.Path(config_path).resolve()),
            config_fingerprint=fingerprint(cfg),
            project_id=runtime["project_id"],
            auth_url=runtime["auth_url"],
            runtime=runtime,
        ),
    )
    print(f"Run {run_id}: {scenario}", flush=True)
    try:
        progress("CHECK scenario prerequisites and API versions", scenario)
        checks = preflight(cfg, scenario, admin, cloud)
        runtime = dict(runtime, **checks)
        ledger.update(runtime=runtime, status="READY")
        task = pathlib.Path(cfg["rally"]["task_dir"]) / (scenario + ".yaml")
        args_file = ledger.path.parent / "task-args.json"
        write_json(
            args_file,
            {
                "config_path": str(pathlib.Path(config_path).resolve()),
                "state_path": str(ledger.path.resolve()),
            },
        )
        progress("VALIDATE Rally task", scenario)
        validated = rally(
            cfg,
            "task",
            "validate",
            task,
            "--env",
            runtime["environment_id"],
            "--task-args-file",
            args_file,
        )
        if validated.returncode:
            # Task arguments contain paths only. Safe to retain validation
            # output.
            (ledger.path.parent / "rally-validation.log").write_text(
                validated.stdout + validated.stderr
            )
            raise InvalidError(
                "Rally task validation failed; see private "
                "rally-validation.log"
            )
        log_path = ledger.path.parent / "rally.log"
        progress("START Rally task; following live output", scenario)
        command = [
            "rally",
            "--plugin-paths",
            cfg["rally"]["plugin_dir"],
            "task",
            "start",
            str(task),
            "--env",
            runtime["environment_id"],
            "--task-args-file",
            str(args_file),
            "--tag",
            "ultimum-" + run_id,
            "--no-use",
        ]
        with log_path.open("w") as log:
            with subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=dict(os.environ, PYTHONUNBUFFERED="1"),
                start_new_session=True,
            ) as process:
                try:
                    for line in process.stdout:
                        print(line, end="", flush=True)
                        log.write(line)
                        log.flush()
                    code = process.wait()
                except BaseException:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise
        ledger = Ledger(ledger.path)  # Rally worker wrote the outcome.
        ledger.update(rally_exit_code=code)
        if code or ledger.data["status"] != "PASS":
            if ledger.data["status"] not in ("FAIL", "BLOCKED", "UNSUPPORTED"):
                ledger.update(
                    status="FAIL",
                    error="Rally did not complete the scenario successfully",
                )
        tasks = rally(
            cfg,
            "task",
            "list",
            "--env",
            runtime["environment_id"],
            "--tag",
            "ultimum-" + run_id,
            "--uuids-only",
        )
        ids = re.findall(
            r"(?m)^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
            tasks.stdout or "",
        )
        if len(ids) == 1:
            progress("EXPORT Rally HTML and JSON reports", scenario)
            ledger.update(rally_task_id=ids[0])
            target = publish(cfg, ledger)
            reports = []
            for extension, flag in (
                ("html", "--html-static"),
                ("json", "--json"),
            ):
                result = rally(
                    cfg,
                    "task",
                    "report",
                    ids[0],
                    flag,
                    "--out",
                    target / ("rally." + extension),
                )
                if result.returncode:
                    reports.append(
                        "Could not export Rally " + extension + " report"
                    )
            ledger.update(report_errors=reports)
        else:
            ledger.update(
                report_errors=[
                    "Could not identify exactly one Rally task for run tag"
                ]
            )
    except BaseException as exc:
        ledger = Ledger(ledger.path)
        status = (
            "UNSUPPORTED"
            if isinstance(exc, UnsupportedError)
            else "BLOCKED"
            if isinstance(exc, InvalidError)
            else "FAIL"
        )
        ledger.update(
            status=status,
            error=str(exc)
            if isinstance(
                exc, (InvalidError, AssertionError, TimeoutError, APIError)
            )
            else type(exc).__name__,
        )
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            ledger.update(interrupted=True)
    finally:
        ledger.update(finished_at=timestamp())
        # Worker or CLI can fail before normal teardown. Ledger cleanup is
        # repeatable.
        if cfg["execution"]["cleanup"] == "always" or (
            cfg["execution"]["cleanup"] == "on-success"
            and ledger.data["status"] == "PASS"
        ):
            resources = Resources(
                cfg, admin, cloud, ledger, ledger.data["runtime"]
            )
            try:
                resources.cleanup()
            except Exception as exc:
                ledger.update(cleanup_error=str(exc))
        elif ledger.data["restore"]:
            try:
                Resources(
                    cfg, admin, cloud, ledger, ledger.data["runtime"]
                ).restore_services()
            except Exception as exc:
                ledger.update(restore_error=str(exc))
        target = publish(cfg, ledger)
        print(
            f"{scenario}: {ledger.data['status']}; "
            f"result: {target / 'result.json'}",
            flush=True,
        )
    success = (
        ledger.data["status"] == "PASS"
        and not ledger.data.get("cleanup_errors")
        and not ledger.data.get("cleanup_error")
        and not ledger.data.get("report_errors")
        and all(item["done"] for item in ledger.data["restore"])
    )
    return success, ledger.data.get("interrupted", False)


def execute_plugin(config_path, state_path, scenario, timer=None):
    with project_lock(pathlib.Path(state_path).parent, "rally-worker"):
        _execute_plugin(config_path, state_path, scenario, timer)


@watch()
def _execute_plugin(config_path, state_path, scenario, timer=None):
    progress("LOAD prepared run and authenticate", scenario)
    cfg = load(config_path)
    ledger = Ledger(state_path)
    if ledger.data["scenario"] != scenario or ledger.data[
        "config_fingerprint"
    ] != fingerprint(cfg):
        raise InvalidError(
            "Rally task scenario/config differs from prepared run"
        )
    if ledger.data["status"] != "READY":
        raise InvalidError(
            "Run is not READY; create a new run through ultimum-rally"
        )
    rc = openrc(cfg["admin"]["openrc"])
    admin, cloud = clouds(cfg, rc)
    if (
        cloud.project_id != ledger.data["project_id"]
        or rc["OS_AUTH_URL"] != ledger.data["auth_url"]
    ):
        raise InvalidError("Prepared run belongs to another cloud or project")
    runtime = ledger.data["runtime"]
    for client in (admin, cloud):
        client.versions["compute"] = runtime["compute_version"]
    if "volume_version" in runtime:
        cloud.versions["volume"] = runtime["volume_version"]
    from .scenarios import Scenarios

    engine = Scenarios(cfg, admin, cloud, ledger, runtime, timer)
    ledger.update(status="RUNNING", started_at=timestamp())
    try:
        engine.run(scenario)
        ledger.update(status="PASS")
    except Exception as exc:
        status = (
            "UNSUPPORTED"
            if isinstance(exc, UnsupportedError)
            else "BLOCKED"
            if isinstance(exc, InvalidError)
            else "FAIL"
        )
        # Keystone/requests exception text can contain URLs. Store only safe
        # types.
        message = (
            str(exc)
            if isinstance(
                exc, (InvalidError, AssertionError, TimeoutError, APIError)
            )
            else type(exc).__name__
        )
        ledger.update(status=status, error=message)
        raise RuntimeError(f"{scenario}: {status}: {message}") from None
    finally:
        engine.close()
        # Keep restoration independent of keep-resources policy.
        try:
            engine.restore_services()
        except Exception as exc:
            ledger.update(restore_error=str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Ultimum project-scoped Rally acceptance runner"
    )
    parser.add_argument("--config", default="/etc/rally/ultimum.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List independent scenarios")
    explain = sub.add_parser(
        "explain", help="Print documented runner/scenario steps"
    )
    explain.add_argument(
        "scenario", choices=("runner", *SCENARIOS), default="runner", nargs="?"
    )
    check = sub.add_parser(
        "check", help="Read-only validation; never provisions resources"
    )
    check.add_argument("scenario", choices=SCENARIOS, nargs="?")
    check.add_argument(
        "--offline", action="store_true", help="Configuration validation only"
    )
    sub.add_parser(
        "prepare",
        help=(
            "Ensure identity, configured quotas, network and Rally environment"
        ),
    )
    run = sub.add_parser(
        "run",
        help="Prepare and run separate Rally task per scenario/repetition",
    )
    run.add_argument("scenario", choices=SCENARIOS, nargs="?")
    run.add_argument(
        "--all", action="store_true", help="Run enabled scenarios sequentially"
    )
    for name in ("report", "cleanup", "fault-start"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command in ("list", "explain"):
            if args.command == "list":
                print("\n".join(SCENARIOS))
            else:
                doc = (
                    pathlib.Path(__file__).resolve().parent.parent
                    / "SCENARIOS.rst"
                )
                text = doc.read_text()
                start = text.index(".. scenario: " + args.scenario + "\n")
                end = text.find(".. scenario: ", start + 1)
                print(text[start : end if end >= 0 else None])
            return 0
        cfg = load(args.config)
        if args.command in ("report", "fault-start"):
            ledger = Ledger(run_path(cfg, args.run_id))
            if args.command == "report":
                print(json.dumps(ledger.data, indent=2))
            else:
                if (
                    ledger.data["scenario"] != "masakari-host-failure"
                    or ledger.data["status"] != "WAITING_FOR_FAULT"
                ):
                    raise InvalidError(
                        "Run is not waiting for a Masakari fault"
                    )
                marker = ledger.path.parent / "fault-start.json"
                with marker.open("x") as stream:
                    json.dump(
                        {"epoch": time.time(), "time": timestamp()}, stream
                    )
                print(
                    "Fault timer started. Hard power off the "
                    "dedicated host now."
                )
            return 0
        if args.command == "check" and args.offline:
            validate(cfg, args.scenario)
            print(
                "Configuration valid (offline; cloud capabilities not checked)"
            )
            return 0
        progress(f"LOAD admin OpenRC: {cfg['admin']['openrc']}", args.command)
        rc = openrc(cfg["admin"]["openrc"])
        admin, cloud = clouds(cfg, rc)
        if args.command == "check":
            validate(cfg, args.scenario)
            # Authentication as configured user verifies project, password and
            # scope.
            print("Test project: " + cloud.project_id)
            selected = (
                [args.scenario]
                if args.scenario
                else [s for s in SCENARIOS if cfg["scenarios"][s]["enabled"]]
            )
            failed = False
            for scenario in selected:
                try:
                    info = preflight(cfg, scenario, admin, cloud)
                    print(scenario + ": prerequisites OK " + json.dumps(info))
                except (InvalidError, AssertionError) as exc:
                    failed = True
                    print(scenario + ": " + str(exc))
            return int(failed)
        with project_lock(cfg["execution"]["state_dir"], lock_key(cfg, rc)):
            if args.command == "cleanup":
                ledger = Ledger(run_path(cfg, args.run_id))
                if rc["OS_AUTH_URL"] != ledger.data["auth_url"]:
                    raise InvalidError("Cleanup cloud differs from run ledger")
                for client in (admin, cloud):
                    client.versions["compute"] = ledger.data["runtime"].get(
                        "compute_version", "2.64"
                    )
                try:
                    Resources(
                        cfg, admin, cloud, ledger, ledger.data["runtime"]
                    ).cleanup()
                finally:
                    publish(cfg, ledger)
                print(
                    "Cleanup complete; project/user/prepared network retained"
                )
                return 0
            if args.command == "run" and bool(args.scenario) == bool(args.all):
                raise InvalidError("Use run <scenario> or run --all")
            # Validate explicit scenario before any prepare mutations.
            if args.command == "run" and args.scenario:
                validate(cfg, args.scenario)
            runtime = prepare(cfg, rc, admin, cloud)
            if args.command == "prepare":
                print(json.dumps(runtime, indent=2))
                return 0
            selected = (
                [args.scenario]
                if args.scenario
                else [s for s in SCENARIOS if cfg["scenarios"][s]["enabled"]]
            )
            success = True
            for scenario in selected:
                for _ in range(cfg["execution"]["repetitions"]):
                    ok, interrupted = run_one(
                        cfg, args.config, scenario, admin, cloud, runtime
                    )
                    success &= ok
                    if interrupted:
                        return 130
            return 0 if success else 1
    except (InvalidError, FileNotFoundError, ValueError) as exc:
        print("BLOCKED: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except APIError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Avoid credential-bearing authentication tracebacks in operator
        # output.
        print(
            "ERROR: "
            + type(exc).__name__
            + "; check configuration, credentials and service availability",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

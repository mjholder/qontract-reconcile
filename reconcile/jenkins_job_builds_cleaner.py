import logging
import operator
import re
import time
from typing import Any

from reconcile import queries
from reconcile.utils.jenkins_api import JenkinsApi
from reconcile.utils.secret_reader import SecretReader

QONTRACT_INTEGRATION = "jenkins-job-builds-cleaner"


def hours_to_ms(hours: int) -> int:
    return hours * 60 * 60 * 1000


def delete_builds(
    jenkins: JenkinsApi, builds_todel: list[dict[str, Any]], dry_run: bool = True
) -> None:
    delete_builds_count = len(builds_todel)
    for idx, build in enumerate(builds_todel, start=1):
        job_name = build["job_name"]
        build_id = build["build_id"]
        progress_str = f"{idx}/{delete_builds_count}"
        logging.debug([
            progress_str,
            job_name,
            build["rule_name"],
            build["rule_keep_hours"],
            build_id,
        ])
        if not dry_run:
            try:
                jenkins.delete_build(build["job_name"], build["build_id"])
            except Exception:
                msg = f"failed to delete {job_name}/{build_id}"
                logging.exception(msg)


def get_last_build_ids(builds: list[dict[str, Any]]) -> list[str]:
    builds_to_keep = []
    sorted_builds = sorted(builds, key=operator.itemgetter("timestamp"), reverse=True)
    if sorted_builds:
        last_build = sorted_builds[0]
        builds_to_keep.append(last_build["id"])

    for build in sorted_builds:
        if build["result"] == "SUCCESS":
            builds_to_keep.append(build["id"])
            break
    return builds_to_keep


def find_builds(
    jenkins: JenkinsApi, job_names: list[str], rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    # Current time in ms
    time_ms = time.time() * 1000

    builds_found = []
    for job_name in job_names:
        for rule in rules:
            if rule["name_re"].search(job_name):
                builds = jenkins.get_builds(job_name)
                # We need to keep very last and last successful builds (https://issues.redhat.com/browse/APPSRE-8701)
                builds_to_keep = get_last_build_ids(builds)
                for build in builds:
                    if build["id"] in builds_to_keep:
                        logging.debug(
                            f"{jenkins.url}: {job_name} build: {build['id']} will be kept"
                        )
                        continue
                    if time_ms - rule["keep_ms"] > build["timestamp"]:
                        builds_found.append({
                            "job_name": job_name,
                            "rule_name": rule["name"],
                            "rule_keep_hours": rule["keep_hours"],
                            "build_id": build["id"],
                        })
                # Only act on the first rule matched
                break
    return builds_found


def run(dry_run: bool) -> None:
    jenkins_instances = queries.get_jenkins_instances()
    secret_reader = SecretReader(queries.get_secret_reader_settings())

    for instance in jenkins_instances:
        instance_cleanup_rules = instance.get("buildsCleanupRules", [])
        if not instance_cleanup_rules:
            # Skip instance if no cleanup rules defined
            continue

        # Process cleanup rules, pre-compile as regexes
        cleanup_rules = [
            {
                "name": rule["name"],
                "name_re": re.compile(rule["name"]),
                "keep_hours": rule["keep_hours"],
                "keep_ms": hours_to_ms(rule["keep_hours"]),
            }
            for rule in instance_cleanup_rules
        ]

        token = instance["token"]
        instance_name = instance["name"]
        jenkins = JenkinsApi.init_jenkins_from_secret(secret_reader, token)
        all_job_names = jenkins.get_job_names()

        builds_todel = find_builds(jenkins, all_job_names, cleanup_rules)

        logging.info(f"{instance_name}: {len(builds_todel)} builds will be deleted")
        delete_builds(jenkins, builds_todel, dry_run)
        logging.info(f"{instance_name}: deletion completed")

#!/usr/bin/env python3

import argparse
import copy
import logging
import re
import sys
from typing import Any

from botocore.exceptions import ClientError
from tabulate import tabulate

from .common import AWSSessionBase

logger = logging.getLogger("ssm-tools.resolver")


class InstanceResolver(AWSSessionBase):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)

        # Create boto3 clients from session
        self.ssm_client = self.session.client("ssm")
        self.ec2_client = self.session.client("ec2")

    def get_list(self) -> dict[str, dict[str, Any]]:
        def _try_append(_list: list, _dict: dict, _key: str) -> None:
            if _key in _dict:
                _list.append(_dict[_key])

        items = {}

        # List instances from SSM
        logger.debug("Fetching SSM inventory")
        paginator = self.ssm_client.get_paginator("get_inventory")
        response_iterator = paginator.paginate(
            Filters=[
                {
                    "Key": "AWS:InstanceInformation.ResourceType",
                    "Values": ["EC2Instance", "ManagedInstance"],
                    "Type": "Equal",
                },
                {
                    "Key": "AWS:InstanceInformation.InstanceStatus",
                    "Values": ["Terminated", "Stopped", "ConnectionLost"],
                    "Type": "NotEqual",
                },
            ],
        )

        for inventory in response_iterator:
            for entity in inventory["Entities"]:
                logger.debug(entity)
                content = entity["Data"]["AWS:InstanceInformation"]["Content"][0]
                instance_id = content["InstanceId"]
                items[instance_id] = {
                    "InstanceId": instance_id,
                    "InstanceName": "",
                    "HostName": content.get("ComputerName", ""),
                    "Addresses": [content.get("IpAddress")],
                }
                logger.debug("Added instance: %s: %r", instance_id, items[instance_id])

        # Add attributes from EC2
        paginator = self.ec2_client.get_paginator("describe_instances")
        ec2_instance_ids = list(filter(lambda x: x.startswith("i-"), items))

        tries = 5
        while tries:
            # The SSM inventory sometimes returns instances that have been terminated
            # a short while ago which makes the following call fail
            # with InvalidInstanceID.NotFound exception. We'll try and remove the invalid
            # instance ids a {tries} times or until we succeed. If unsuccessful we'll remove
            # the list obtained from SSM without extra details (host name, public IPs, etc).
            # This mostly / only affects accounts with high churn of starting / stopping
            # instances - most users will pass this loop only once.
            try:
                response_iterator = paginator.paginate(InstanceIds=ec2_instance_ids)
                for reservations in response_iterator:
                    for reservation in reservations["Reservations"]:
                        for instance in reservation["Instances"]:
                            instance_id = instance["InstanceId"]
                            if instance_id not in items:
                                continue

                            # Find instance IPs
                            items[instance_id]["Addresses"] = []
                            _try_append(items[instance_id]["Addresses"], instance, "PrivateIpAddress")
                            _try_append(items[instance_id]["Addresses"], instance, "PublicIpAddress")

                            # Store instance AZ - useful for EC2 Instance Connect
                            items[instance_id]["AvailabilityZone"] = instance["Placement"]["AvailabilityZone"]

                            # Find instance name from tag Name
                            for tag in instance.get("Tags", []):
                                if tag["Key"] == "Name" and tag["Value"]:
                                    items[instance_id]["InstanceName"] = tag["Value"]

                            logger.debug("Updated instance: %s: %r", instance_id, items[instance_id])
                    return items

            except ClientError as ex:
                if ex.response.get("Error", {}).get("Code", "") != "InvalidInstanceID.NotFound":
                    raise
                message = ex.response.get("Error", {}).get("Message", "")
                if not message.startswith("The instance ID") or not message.endswith("not exist"):
                    logger.warning("Unexpected InvalidInstanceID.NotFound message: %s", message)
                # Try to extract instace ids ...
                remove_instance_ids = re.findall("i-[0-9a-f]+", message)
                logger.debug("Removing non-existent InstanceIds: %s", remove_instance_ids)
                # Remove the failed ids from the list and try again
                ec2_instance_ids = list(set(ec2_instance_ids) - set(remove_instance_ids))
                tries -= 1

        if not tries:
            logger.warning("Unable to list instance details. Some instance names and IPs may be missing.")

        return items

    def print_list(self, quiet: bool = False) -> tuple[str, list[Any]]:
        items = self.get_list().values()

        if not items:
            logger.warning("No instances registered in SSM!")
            sys.exit(1)

        items_list = list(items)
        del items
        items_list.sort(key=lambda x: x.get("InstanceName") or x.get("HostName"))  # type: ignore

        for instance in items_list:
            instance["Addresses"] = ", ".join(instance["Addresses"])
            del instance["AvailabilityZone"]

        table = tabulate(items_list, headers="keys")
        if not quiet:
            print(table)

        menu_header = "\n".join([f"  {line}" for line in table.split("\n")[:2]])
        menu_data = []

        for container_data, container_text in zip(items_list, table.split("\n")[2:]):
            menu_data.append({"summary": container_text, **container_data})

        return menu_header, menu_data

    def resolve_instance(self, instance: str) -> tuple[str, dict[str, Any]]:
        # Is it a valid Instance ID?
        if re.match("^m?i-[a-f0-9]+$", instance):
            return instance, {}

        # It is not - find it in the list
        instances = []

        items = self.get_list()
        for instance_id in items:
            item = items[instance_id]
            if instance.lower() in [item["HostName"].lower(), item["InstanceName"].lower()] + item["Addresses"]:
                instances.append(instance_id)

        if not instances:
            return "", {}

        if len(instances) > 1:
            logger.warning("Found %d instances for '%s': %s", len(instances), instance, " ".join(instances))
            logger.warning("Use INSTANCE_ID to connect to a specific one")
            sys.exit(1)

        # Print the matched instance we are connecting to
        row = []
        for v in items[instances[0]].values():
            row.append(", ".join(v) if isinstance(v, list) else v)
        print(tabulate([row], headers=list(items[instances[0]].keys())))

        # Found only one instance - return it
        return instances[0], items[instances[0]]


class ContainerResolver(AWSSessionBase):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)

        # Create boto3 clients from session
        self.ecs_client = self.session.client("ecs")

        self.args = args
        self.containers: list[dict[str, Any]] = []
        self._tasks: dict[str, Any] = {}

    def add_container(self, container: dict[str, Any]) -> None:
        _task_parsed = container["taskArn"].split(":")[-1].split("/")
        self.containers.append(
            {
                "cluster_name": _task_parsed[1],
                "task_id": _task_parsed[2],
                "cluster_arn": self._tasks[container["taskArn"]]["clusterArn"],
                "task_arn": container["taskArn"],
                "group_name": self._tasks[container["taskArn"]]["group"],
                "container_name": container["name"],
                "container_ip": container["networkInterfaces"][0]["privateIpv4Address"],
            },
        )

    def get_list(self) -> list[dict[str, Any]]:
        # List ECS Clusters
        clusters = []
        logger.debug("Listing ECS Clusters")
        paginator = self.ecs_client.get_paginator("list_clusters")
        for page in paginator.paginate():
            clusters.extend(page["clusterArns"])

        if self.args.cluster:
            filtered_clusters = []
            for cluster in clusters:
                if (self.args.cluster.startswith("arn:") and cluster == self.args.cluster) or cluster.endswith(
                    f"/{self.args.cluster}",
                ):
                    filtered_clusters.append(cluster)
                    break
            clusters = filtered_clusters

        if not clusters:
            logger.warning("No ECS Clusters found.")
            return []

        # List tasks in each cluster
        paginator = self.ecs_client.get_paginator("list_tasks")
        for cluster in clusters:
            logger.debug("Listing tasks in cluster: %s", cluster)

            # maxResults must be <= 100 because describe_tasks() doesn't accept more than that
            for page in paginator.paginate(cluster=cluster, maxResults=100):
                if "taskArns" not in page or not page["taskArns"]:
                    logger.debug(f"No tasks found in cluster {cluster}")
                    break
                response = self.ecs_client.describe_tasks(cluster=cluster, tasks=page["taskArns"])

                # Filter containers that have a running ExecuteCommandAgent
                for task in response["tasks"]:
                    logger.debug(task)
                    self._tasks[task["taskArn"]] = task
                    for container in task["containers"]:
                        if "managedAgents" not in container:
                            continue
                        for agent in container["managedAgents"]:
                            if agent["name"] == "ExecuteCommandAgent" and agent["lastStatus"] == "RUNNING":
                                self.add_container(container)

        return self.containers

    def print_containers(self, containers: list[dict[str, Any]], quiet: bool = False) -> tuple[str, list[Any]]:
        table_data = copy.deepcopy(containers)

        table_data.sort(key=lambda x: [x["cluster_name"], x["container_name"]])

        for container_details in table_data:
            del container_details["cluster_arn"], container_details["task_arn"]

        table = tabulate(table_data, headers="keys")
        if not quiet:
            print(table)

        menu_header = "\n".join([f"  {line}" for line in table.split("\n")[:2]])
        menu_data = []

        for container_data, container_text in zip(table_data, table.split("\n")[2:]):
            menu_data.append({"summary": container_text, **container_data})

        return menu_header, menu_data

    def print_list(self, quiet: bool = False) -> tuple[str, list[Any]]:
        containers = self.get_list()

        if not containers:
            logger.warning("No Execute-Command capable containers found!")
            sys.exit(1)

        return self.print_containers(containers, quiet=quiet)

    def resolve_container(self, keywords: list[str]) -> dict[str, Any]:
        containers = self.get_list()
        if not containers:
            logger.warning("No Execute-Command capable containers found!")
            sys.exit(1)

        logger.debug("Searching for containers matching all keywords: %s", " ".join(keywords))

        candidates: list[dict[str, Any]] = []
        for container in containers:
            for keyword in keywords:
                if keyword not in (
                    container["group_name"],
                    container["task_id"],
                    container["container_name"],
                    container["container_ip"],
                ):
                    logger.debug(
                        "IGNORED: Container %s/%s doesn't match keyword: %s",
                        container["task_id"],
                        container["container_name"],
                        keyword,
                    )
                    container = {}
                    break
            if container:
                logger.debug(
                    "ADDED: Container %s/%s matches all keywords: %s",
                    container["task_id"],
                    container["container_name"],
                    " ".join(keywords),
                )
                candidates.append(container)
        if not candidates:
            logger.warning("No container matches: %s", " AND ".join(keywords))
            sys.exit(1)
        elif len(candidates) == 1:
            self.print_containers(candidates, quiet=True)
            return candidates[0]
        else:
            logger.warning("Found %d instances for: %s", len(candidates), keyword)
            logger.warning("Use Container IP or Task ID to connect to a specific one")
            self.print_containers(candidates, quiet=True)
            sys.exit(1)

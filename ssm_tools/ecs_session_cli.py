#!/usr/bin/env python3

# Convenience wrapper around 'aws ecs execute-command'
#
# See https://aws.nz/aws-utils/ecs-session for more info.
#
# Author: Michael Ludvig (https://aws.nz)

# The script can list the available containers across all your ECS clusters.
# In the end it executes 'aws ecs execute-command' with the appropriate parameters.
# Supports both EC2 and Fargate ECS tasks.

import os
import sys
import logging
import argparse

from typing import Tuple, List, Dict, Any

import botocore.exceptions

from .common import add_general_parameters, configure_logging, show_version
from .resolver import ContainerResolver

logger = logging.getLogger("ssm-tools.ecs-session")


def parse_args(argv: list) -> Tuple[argparse.Namespace, List[str]]:
    """
    Parse command line arguments.
    """

    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)

    add_general_parameters(parser)

    group_container = parser.add_argument_group("Container Selection")
    group_container.add_argument("CONTAINER", nargs=argparse.ZERO_OR_MORE, help="Task ID, Container Name or IP address. Use multiple keywords (e.g. Task Name and IP) to narrow down ambiguous selections.")
    group_container.add_argument("--list", "-l", dest="list", action="store_true", help="List available containers configured for ECS RunTask")
    group_container.add_argument("--cluster", dest="cluster", metavar="CLUSTER", help="Specify an ECS cluster. (optional)")

    group_session = parser.add_argument_group("Session Parameters")
    group_session.add_argument("--command", "-c", dest="command", metavar="COMMAND", default="/bin/sh", help="Command to run inside the container. Default: /bin/sh")

    parser.description = "Execute 'ECS Run Task' in a given container"
    parser.epilog = f"""
IMPORTANT: containers must have "execute-command" setting enabled or they
will not be recognised by {parser.prog} nor show up in --list output.

Visit https://aws.nz/aws-utils/ecs-session for more info and usage examples.

Author: Michael Ludvig
"""

    # Parse supplied arguments
    args, extras = parser.parse_known_args(argv)

    # If --version do it now and exit
    if args.show_version:
        show_version(args)

    return args, extras


def start_session(container: Dict[str, Any], args: argparse.Namespace, command: str) -> None:
    exec_args = ["aws", "ecs", "execute-command"]
    if args.profile:
        exec_args += ["--profile", args.profile]
    if args.region:
        exec_args += ["--region", args.region]

    # fmt: off
    exec_args += [
        "--cluster", container["cluster_arn"],
        "--task", container["task_arn"],
        "--container", container["container_name"],
        "--command", command,
        "--interactive",
    ]
    # fmt: on

    logger.debug("Running: %s", exec_args)
    os.execvp(exec_args[0], exec_args)


def main() -> int:
    ## Split command line to main args and optional command to run
    args, _ = parse_args(sys.argv[1:])

    configure_logging(args.log_level)

    try:
        if bool(args.CONTAINER) + bool(args.list) != 1:
            containers = ContainerResolver(args).print_list()
            while True:
                try:
                    session = int(input(f"Choose a container to connect to between 0-{len(containers)-1} or type quit: "))
                    if 0 <= session < len(containers) -1:
                        args.CONTAINER = [containers[session]["task_id"], containers[session]["container_name"]]
                        break
                except ValueError:
                    print("Exiting")
                    sys.exit(0)
            
        if args.list:
            ContainerResolver(args).print_list()
            sys.exit(0)

        container = ContainerResolver(args).resolve_container(keywords=args.CONTAINER)

        if not container:
            logger.warning("Could not find any container matching: %s", " AND ".join(args.CONTAINER))
            sys.exit(1)

        start_session(container, args, args.command)

    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
        logger.error(e)
        sys.exit(1)

    return 0


if __name__ == "__main__":
    main()

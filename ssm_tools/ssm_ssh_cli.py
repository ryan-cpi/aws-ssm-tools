#!/usr/bin/env python3

# Open SSH connections through AWS Session Manager
#
# See https://aws.nz/aws-utils/ec2-ssh for more info.
#
# Author: Michael Ludvig (https://aws.nz)

# The script can list available instances, resolve instance names,
# and host names, etc. In the end it executes 'ssh' with the correct
# parameters to actually start the SSH session.

import argparse
import logging
import os
import sys

import botocore.exceptions
from simple_term_menu import TerminalMenu

from .common import (
    add_general_parameters,
    configure_logging,
    show_version,
    verify_awscli_version,
    verify_plugin_version,
)
from .ec2_instance_connect import EC2InstanceConnectHelper
from .resolver import InstanceResolver

logger = logging.getLogger("ssm-tools.ec2-ssh")


def parse_args(argv: list) -> tuple[argparse.Namespace, list[str]]:
    """
    Parse command line arguments.
    """

    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)

    add_general_parameters(parser, long_only=True)

    group_instance = parser.add_argument_group("Instance Selection")
    group_instance.add_argument(
        "--list",
        dest="list",
        action="store_true",
        help="List instances available for SSM Session",
    )

    group_ec2ic = parser.add_argument_group("EC2 Instance Connect")
    group_ec2ic.add_argument("--reason", help="The reason for connecting to the instance.")
    group_ec2ic.add_argument(
        "--user",
        dest="user",
        metavar="USER",
        help="USER after opening the session.",
    )
    group_ec2ic.add_argument(
        "--no-send-key",
        dest="send_key",
        action="store_false",
        help="Send the SSH key to instance metadata using EC2 Instance Connect",
    )
    group_ec2ic.add_argument(
        "--use-endpoint",
        dest="use_endpoint",
        action="store_true",
        default=False,
        help="Connect using 'EC2 Instance Connect Endpoint'",
    )

    parser.description = "Open SSH connection through Session Manager"
    parser.epilog = f"""
IMPORTANT: instances must be registered in AWS Systems Manager (SSM)
before you can start a shell session! Instances not registered in SSM
will not be recognised by {parser.prog} nor show up in --list output.

Visit https://aws.nz/aws-utils/ec2-ssh for more info and usage examples.

Author: Michael Ludvig
"""

    # Parse supplied arguments
    args, extra_args = parser.parse_known_args(argv)

    # If --version do it now and exit
    if args.show_version:
        show_version(args)

    # Require exactly one of INSTANCE or --list
    if bool(extra_args) + bool(args.list) != 1:
        parser.error("Specify either --list or SSH Options including instance name")

    return args, extra_args


def start_ssh_session(ssh_args: list, profile: str, region: str, use_endpoint: bool, reason: str = "") -> None:
    aws_args = ""
    if profile:
        aws_args += f"--profile {profile} "
    if region:
        aws_args += f"--region {region} "
    if reason:
        aws_args += f"--reason '{reason}' "

    if use_endpoint:
        min_awscli_version = "2.12.0"
        if not verify_awscli_version(min_awscli_version, logger):
            logger.error(
                f"AWS CLI v{min_awscli_version} or newer is required for --use-endpoint, falling back to SSM Session Manager",
            )
            use_endpoint = False
    if use_endpoint:
        proxy_option = ["-o", f"ProxyCommand=aws {aws_args} ec2-instance-connect open-tunnel --instance-id %h"]
    else:
        proxy_option = [
            "-o",
            f"ProxyCommand=aws {aws_args} ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p",
        ]
    command = ["ssh"] + proxy_option + ssh_args
    logger.debug("Running: %s", command)
    os.execvp(command[0], command)


def main() -> int:
    ## Split command line to main args and optional command to run
    args, extra_args = parse_args(sys.argv[1:])

    if args.log_level == logging.DEBUG:
        extra_args.append("-v")

    configure_logging(args.log_level)

    if not verify_plugin_version("1.1.23", logger):
        sys.exit(1)

    try:
        instance_resolver = InstanceResolver(args)

        if args.list:
            instance_resolver.print_list()
            sys.exit(0)

        # Loop through all SSH args to find:
        # - instance name
        # - user name (for use with --send-key)
        # - key name (for use with --send-key)
        ssh_args = []
        instance_id = ""
        login_name = ""
        key_file_name = ""

        extra_args_iter = iter(extra_args)
        for arg in extra_args_iter:
            # User name argument
            if arg.startswith("-l"):
                ssh_args.append(arg)
                if len(arg) > 2:
                    login_name = arg[2:]
                else:
                    login_name = next(extra_args_iter)
                    ssh_args.append(login_name)
                continue

            # SSH key argument
            if arg.startswith("-i"):
                ssh_args.append(arg)
                if len(arg) > 2:
                    key_file_name = arg[2:]
                else:
                    key_file_name = next(extra_args_iter)
                    ssh_args.append(key_file_name)
                continue

            # If we already have instance id just copy the args
            if instance_id:
                ssh_args.append(arg)
                continue

            # Some args that can't be an instance name
            if arg.startswith("-") or arg.find(":") > -1 or arg.find(os.path.sep) > -1:
                ssh_args.append(arg)
                continue

            # This may be an instance name - try to resolve it
            maybe_login_name = None
            if arg.find("@") > -1:  # username@hostname format
                maybe_login_name, instance = arg.split("@", 1)
            else:
                instance = arg

            instance_id, _ = instance_resolver.resolve_instance(instance)
            if not instance_id:
                # Not resolved as an instance name - put back to args
                ssh_args.append(arg)
                maybe_login_name = None
                continue

            # We got a login name from 'login_name@instance'
            if maybe_login_name:
                login_name = maybe_login_name

            # Woohoo we've got an instance id!
            logger.debug("Resolved instance name '%s' to '%s'", instance, instance_id)
            ssh_args.append(instance_id)

            if login_name:
                ssh_args.extend(["-l", login_name])

        if not instance_id:
            headers, session_details = InstanceResolver(args).print_list(quiet=True)
            terminal_menu = TerminalMenu(
                [text["summary"] for text in session_details],
                title=headers,
                show_search_hint=True,
                show_search_hint_text="Select a connection. Press 'q' to quit, or '/' to search.",
            )
            selected_index = terminal_menu.show()
            if selected_index is None:
                sys.exit(0)

            selected_session = session_details[selected_index]
            instance_id = selected_session["InstanceId"]
            ssh_args.append(instance_id)

            print(headers)
            print(f"  {selected_session['summary']}")
        if args.send_key:
            EC2InstanceConnectHelper(args).send_ssh_key(instance_id, login_name, key_file_name)

        start_ssh_session(
            ssh_args=ssh_args,
            profile=args.profile,
            region=args.region,
            use_endpoint=args.use_endpoint,
            reason=args.reason,
        )

    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
        logger.error(e)
        sys.exit(1)

    return 0


if __name__ == "__main__":
    main()

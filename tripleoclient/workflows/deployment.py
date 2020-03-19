# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
from __future__ import print_function

import copy
import getpass
import os
import time

import six

from heatclient.common import event_utils
from openstackclient import shell
from tripleo_common.actions import ansible
from tripleo_common.actions import config
from tripleo_common.actions import deployment
from tripleo_common.actions import swifthelper

from tripleoclient.constants import ANSIBLE_TRIPLEO_PLAYBOOKS
from tripleoclient.constants import DEFAULT_WORK_DIR
from tripleoclient import exceptions
from tripleoclient import utils


_WORKFLOW_TIMEOUT = 360  # 6 * 60 seconds


def deploy(log, clients, **workflow_input):
    utils.run_ansible_playbook(
        "cli-deploy-deployment-plan.yaml",
        'undercloud,',
        ANSIBLE_TRIPLEO_PLAYBOOKS,
        extra_vars={
            "container": workflow_input['container'],
            "run_validations": workflow_input['run_validations'],
            "skip_deploy_identifier": workflow_input['skip_deploy_identifier'],
            "timeout_mins": workflow_input['timeout'],
        },
        verbosity=3
    )

    print("Success.")


def deploy_and_wait(log, clients, stack, plan_name, verbose_level,
                    timeout=None, run_validations=False,
                    skip_deploy_identifier=False, deployment_options={}):
    """Start the deploy and wait for it to finish"""

    workflow_input = {
        "container": plan_name,
        "run_validations": run_validations,
        "skip_deploy_identifier": skip_deploy_identifier,
        "timeout": timeout
    }

    if timeout is not None:
        workflow_input['timeout'] = timeout

    deploy(log, clients, **workflow_input)

    # need to move this to the playbook I guess
    orchestration_client = clients.orchestration

    if stack is None:
        log.info("Performing Heat stack create")
        action = 'CREATE'
        marker = None
    else:
        log.info("Performing Heat stack update")
        # Make sure existing parameters for stack are reused
        # Find the last top-level event to use for the first marker
        events = event_utils.get_events(orchestration_client,
                                        stack_id=plan_name,
                                        event_args={'sort_dir': 'desc',
                                                    'limit': 1})
        marker = events[0].id if events else None
        action = 'UPDATE'

    time.sleep(10)
    verbose_events = verbose_level >= 1
    create_result = utils.wait_for_stack_ready(
        orchestration_client, plan_name, marker, action, verbose_events)
    if not create_result:
        shell.OpenStackShell().run(["stack", "failures", "list", plan_name])
        set_deployment_status(
            clients=clients,
            plan=plan_name,
            status='failed'
        )
        if stack is None:
            raise exceptions.DeploymentError("Heat Stack create failed.")
        else:
            raise exceptions.DeploymentError("Heat Stack update failed.")


def create_overcloudrc(clients, container="overcloud", no_proxy=''):
    context = clients.tripleoclient.create_mistral_context()
    return deployment.OvercloudRcAction(container, no_proxy).run(context)


def get_overcloud_hosts(stack, ssh_network):
    ips = []
    role_net_ip_map = utils.get_role_net_ip_map(stack)
    blacklisted_ips = utils.get_blacklisted_ip_addresses(stack)
    for net_ip_map in role_net_ip_map.values():
        # get a copy of the lists of ssh_network and ctlplane ips
        # as blacklisted_ips will only be the ctlplane ips, we need
        # both lists to determine which to actually blacklist
        net_ips = copy.copy(net_ip_map.get(ssh_network, []))
        ctlplane_ips = copy.copy(net_ip_map.get('ctlplane', []))

        blacklisted_ctlplane_ips = \
            [ip for ip in ctlplane_ips if ip in blacklisted_ips]

        # for each blacklisted ctlplane ip, remove the corresponding
        # ssh_network ip at that same index in the net_ips list
        for bcip in blacklisted_ctlplane_ips:
            index = ctlplane_ips.index(bcip)
            ctlplane_ips.pop(index)
            net_ips.pop(index)

        ips.extend(net_ips)

    return ips


def get_hosts_and_enable_ssh_admin(stack, overcloud_ssh_network,
                                   overcloud_ssh_user, overcloud_ssh_key,
                                   overcloud_ssh_port_timeout):
    """Enable ssh admin access.

    Get a list of hosts from a given stack and enable admin ssh across all of
    them.

    :param stack: Stack data.
    :type stack: Object

    :param overcloud_ssh_network: Network id.
    :type overcloud_ssh_network: String

    :param overcloud_ssh_user: SSH access username.
    :type overcloud_ssh_user: String

    :param overcloud_ssh_key: SSH access key.
    :type overcloud_ssh_key: String

    :param overcloud_ssh_port_timeout: Ansible connection timeout
    :type overcloud_ssh_port_timeout: Int
    """

    hosts = get_overcloud_hosts(stack, overcloud_ssh_network)
    if [host for host in hosts if host]:
        enable_ssh_admin(
            stack,
            hosts,
            overcloud_ssh_user,
            overcloud_ssh_key,
            overcloud_ssh_port_timeout
        )
    else:
        raise exceptions.DeploymentError(
            'Cannot find any hosts on "{}" in network "{}"'.format(
                stack.stack_name,
                overcloud_ssh_network
            )
        )


def enable_ssh_admin(stack, hosts, ssh_user, ssh_key, timeout):
    """Run enable ssh admin access playbook.

    :param stack: Stack data.
    :type stack: Object

    :param hosts: Machines to connect to.
    :type hosts: List

    :param ssh_user: SSH access username.
    :type ssh_user: String

    :param ssh_key: SSH access key.
    :type ssh_key: String

    :param timeout: Ansible connection timeout
    :type timeout: int
    """

    print(
        'Enabling ssh admin (tripleo-admin) for hosts: {}.'
        '\nUsing ssh user "{}" for initial connection.'
        '\nUsing ssh key at "{}" for initial connection.'
        '\n\nStarting ssh admin enablement playbook'.format(
            hosts,
            ssh_user,
            ssh_key
        )
    )
    with utils.TempDirs() as tmp:
        utils.run_ansible_playbook(
            playbook='cli-enable-ssh-admin.yaml',
            inventory=','.join(hosts),
            workdir=tmp,
            playbook_dir=ANSIBLE_TRIPLEO_PLAYBOOKS,
            key=ssh_key,
            ssh_user=ssh_user,
            extra_vars={
                "ssh_user": ssh_user,
                "ssh_servers": hosts,
                'tripleo_cloud_name': stack.stack_name
            },
            ansible_timeout=timeout
        )
    print("Enabling ssh admin - COMPLETE.")


def config_download(log, clients, stack, ssh_network=None,
                    output_dir=None, override_ansible_cfg=None,
                    timeout=None, verbosity=1, deployment_options=None,
                    in_flight_validations=False,
                    ansible_playbook_name='deploy_steps_playbook.yaml',
                    limit_list=None, extra_vars=None, inventory_path=None,
                    ssh_user='tripleo-admin', tags=None, skip_tags=None):
    """Run config download.

    :param log: Logging object
    :type log: Object

    :param clients: openstack clients
    :type clients: Object

    :param stack: Heat Stack object
    :type stack: Object

    :param ssh_network: Network named used to access the overcloud.
    :type ssh_network: String

    :param output_dir: Path to the output directory.
    :type output_dir: String

    :param override_ansible_cfg: Ansible configuration file location.
    :type override_ansible_cfg: String

    :param timeout: Ansible connection timeout. If None, the effective
                    default will be set to 30 at playbook runtime.
    :type timeout: Integer

    :param verbosity: Ansible verbosity level.
    :type verbosity: Integer

    :param deployment_options: Additional deployment options.
    :type deployment_options: Dictionary

    :param in_flight_validations: Enable or Disable inflight validations.
    :type in_flight_validations: Boolean

    :param ansible_playbook_name: Name of the playbook to execute.
    :type ansible_playbook_name: String

    :param limit_list: List of hosts to limit the current playbook to.
    :type limit_list: List

    :param extra_vars: Set additional variables as a Dict or the absolute
                       path of a JSON or YAML file type.
    :type extra_vars: Either a Dict or the absolute path of JSON or YAML

    :param inventory_path: Inventory file or path, if None is provided this
                           function will perform a lookup
    :type inventory_path: String

    :param ssh_user: SSH user, defaults to tripleo-admin.
    :type ssh_user: String

    :param tags: Ansible inclusion tags.
    :type tags: String

    :param skip_tags: Ansible exclusion tags.
    :type skip_tags: String
    """

    def _log_and_print(message, logger, level='info', print_msg=True):
        """Print and log a given message.

        :param message: Message to print and log.
        :type message: String

        :param log: Logging object
        :type log: Object

        :param level: Log level.
        :type level: String

        :param print_msg: Print messages to stdout.
        :type print_msg: Boolean
        """

        if print_msg:
            print(message)

        log = getattr(logger, level)
        log(message)

    if not output_dir:
        output_dir = DEFAULT_WORK_DIR

    if not deployment_options:
        deployment_options = dict()

    if not in_flight_validations:
        if skip_tags:
            skip_tags = 'opendev-validation,{}'.format(skip_tags)
        else:
            skip_tags = 'opendev-validation'

    if not timeout:
        timeout = 30

    # NOTE(cloudnull): List of hosts to limit the current playbook execution
    #                  The list is later converted into an ansible compatible
    #                  string. Storing hosts in list format will ensure all
    #                  entries are consistent.
    if not limit_list:
        limit_list = list()
    elif isinstance(limit_list, six.string_types):
        limit_list = [i.strip() for i in limit_list.split(',')]

    with utils.TempDirs() as tmp:
        utils.run_ansible_playbook(
            playbook='cli-grant-local-access.yaml',
            inventory='localhost,',
            workdir=tmp,
            playbook_dir=ANSIBLE_TRIPLEO_PLAYBOOKS,
            extra_vars={
                'access_path': output_dir,
                'execution_user': getpass.getuser()
            }
        )

    stack_work_dir = os.path.join(output_dir, stack.stack_name)
    context = clients.tripleoclient.create_mistral_context()
    _log_and_print(
        message='Checking for blacklisted hosts from stack: {}'.format(
            stack.stack_name
        ),
        logger=log,
        print_msg=(verbosity == 0)
    )
    blacklist_show = stack.output_show('BlacklistedHostnames')
    blacklist_stack_output = blacklist_show.get('output', dict())
    blacklist_stack_output_value = blacklist_stack_output.get('output_value')
    if blacklist_stack_output_value:
        limit_list.extend(
            ['!{}'.format(i) for i in blacklist_stack_output_value if i]
        )
    _log_and_print(
        message='Retrieving configuration for stack: {}'.format(
            stack.stack_name
        ),
        logger=log,
        print_msg=(verbosity == 0)
    )
    container_config = '{}-config'.format(stack.stack_name)

    utils.get_config(clients, container=stack.stack_name,
                     container_config=container_config)
    _log_and_print(
        message='Downloading configuration for stack: {}'.format(
            stack.stack_name
        ),
        logger=log,
        print_msg=(verbosity == 0)
    )
    download = config.DownloadConfigAction(
        work_dir=stack_work_dir,
        container_config=container_config)

    work_dir = download.run(context=context)
    _log_and_print(
        message='Retrieving keyfile for stack: {}'.format(
            stack.stack_name
        ),
        logger=log,
        print_msg=(verbosity == 0)
    )
    key_file = utils.get_key(stack=stack.stack_name)
    _log_and_print(
        message='Generating information for stack: {}'.format(
            stack.stack_name
        ),
        logger=log,
        print_msg=(verbosity == 0)
    )
    inventory_kwargs = {
        'ansible_ssh_user': ssh_user,
        'work_dir': work_dir,
        'plan_name': stack.stack_name,
        'undercloud_key_file': key_file
    }
    if ssh_network:
        inventory_kwargs['ssh_network'] = ssh_network
    python_interpreter = deployment_options.get('ansible_python_interpreter')
    if python_interpreter:
        inventory_kwargs['ansible_python_interpreter'] = python_interpreter
    if not inventory_path:
        inventory = ansible.AnsibleGenerateInventoryAction(**inventory_kwargs)
        inventory_path = inventory.run(context=context)
    _log_and_print(
        message='Executing deployment playbook for stack: {}'.format(
            stack.stack_name
        ),
        logger=log,
        print_msg=(verbosity == 0)
    )

    # NOTE(cloudnull): Join the limit_list into an ansible compatible string.
    #                  If it is an empty, the object will be reset to None.
    limit_hosts = ':'.join(limit_list)
    if not limit_hosts:
        limit_hosts = None
    else:
        limit_hosts = '{}'.format(limit_hosts)

    if isinstance(ansible_playbook_name, list):
        playbooks = [os.path.join(stack_work_dir, p)
                     for p in ansible_playbook_name]
    else:
        playbooks = os.path.join(stack_work_dir, ansible_playbook_name)

    with utils.TempDirs() as tmp:
        utils.run_ansible_playbook(
            playbook=playbooks,
            inventory=inventory_path,
            workdir=tmp,
            playbook_dir=work_dir,
            skip_tags=skip_tags,
            ansible_cfg=override_ansible_cfg,
            verbosity=verbosity,
            ssh_user=ssh_user,
            key=key_file,
            limit_hosts=limit_hosts,
            ansible_timeout=timeout,
            reproduce_command=True,
            extra_env_variables={
                'ANSIBLE_BECOME': True,
            },
            extra_vars=extra_vars,
            tags=tags
        )

    _log_and_print(
        message='Overcloud configuration completed for stack: {}'.format(
            stack.stack_name
        ),
        logger=log,
        print_msg=(verbosity == 0)
    )


def config_download_export(clients, plan, config_type):
    """Export a given config.

    :param clients: application client object.
    :type clients: Object

    :param plan: Plan name.
    :type plan: String

    :param config_type: List of config type options.
    :type config_type: List

    :returns: string
    """

    context = clients.tripleoclient.create_mistral_context()
    container_config = '{}-config'.format(plan)
    config.GetOvercloudConfig(
        container=plan,
        config_type=config_type,
        container_config=container_config
    ).run(context=context)
    print(
        'Config Download export complete for {}. Creating temp URL.'.format(
            plan
        )
    )
    return swifthelper.SwiftTempUrlAction(
        container=container_config,
        obj='{}.tar.gz'.format(container_config)
    ).run(context=context)


def get_horizon_url(stack):
    """Return horizon URL string.

    :params stack: Stack name
    :type stack: string
    :returns: string
    """

    with utils.TempDirs() as tmp:
        horizon_tmp_file = os.path.join(tmp, 'horizon_url')
        utils.run_ansible_playbook(
            playbook='cli-undercloud-get-horizon-url.yaml',
            inventory='localhost,',
            workdir=tmp,
            playbook_dir=ANSIBLE_TRIPLEO_PLAYBOOKS,
            extra_vars={
                'stack_name': stack,
                'horizon_url_output_file': horizon_tmp_file
            }
        )

        with open(horizon_tmp_file) as f:
            return f.read().strip()


def get_deployment_status(clients, plan):
    """Return current deployment status.

    :param clients: application client object.
    :type clients: Object

    :param plan: Plan name.
    :type plan: String

    :returns: string
    """

    context = clients.tripleoclient.create_mistral_context()
    get_deployment_status = deployment.DeploymentStatusAction(plan=plan)
    status = get_deployment_status.run(context=context)
    status_update = status.get('status_update')
    deployment_status = status.get('deployment_status')
    if status_update:
        utils.update_deployment_status(
            clients=clients,
            plan=plan,
            status=status
        )
        return status_update, plan
    else:
        return deployment_status, plan


def set_deployment_status(clients, plan, status):
    """Update a given deployment status.

    :param clients: application client object.
    :type clients: Object

    :param plan: Plan name.
    :type plan: String

    :param status: Current status of the deployment.
    :type status: String
    """

    deploy_status = 'DEPLOY_{}'.format(status.upper())
    utils.update_deployment_status(
        clients=clients,
        plan=plan,
        status={
            'deployment_status': deploy_status,
            'status_update': deploy_status
        }
    )


def get_deployment_failures(clients, plan):
    """Return a list of deployment failures.

    :param clients: application client object.
    :type clients: Object

    :param plan: Name of plan to lookup.
    :param plan: String

    :returns: Dictionary
    """

    context = clients.tripleoclient.create_mistral_context()
    get_failures = deployment.DeploymentFailuresAction(plan=plan)
    return get_failures.run(context=context)['failures']

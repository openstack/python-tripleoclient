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

import logging

import yaml

from tripleo_common.actions import plan
# TODO(cloudnull): Convert to a swiftutils in tripleo-common
# from tripleo_common.utils import swift as swiftutils

from tripleoclient import utils

LOG = logging.getLogger(__name__)


def get_roles_data(roles_file, tht_root):
    abs_roles_file = utils.get_roles_file_path(
        roles_file, tht_root)
    roles_data = None
    with open(abs_roles_file, 'r') as fp:
        roles_data = yaml.safe_load(fp)
    return roles_data


def get_roles(clients, roles_file, tht_root,
              stack_name,
              template,
              files,
              env_files,
              detail=False, valid=False):
    roles_data = get_roles_data(roles_file, tht_root)

    if detail:
        return roles_data

    role_names = [role['name'] for role in roles_data]

    if not valid:
        return role_names

    stack_data = utils.build_stack_data(
        clients, stack_name, template,
        files, env_files)

    valid_roles = []
    for name in role_names:
        role_count = stack_data['parameters'].get(
            name + 'Count', {}).get('default', 0)
        if role_count > 0:
            valid_roles.append(name)

    return valid_roles


def list_available_roles(clients, container='overcloud'):
    """Return a list of available roles.

    :param clients: openstack clients
    :type clients: Object

    :param container: Name of swift object container
    :type container: String

    :returns: List
    """

    LOG.info('Pulling role list from: {}'.format(container))
    obj_client = clients.tripleoclient.object_store
    available_yaml_roles = list()
    LOG.info('Indexing roles from: {}'.format(container))
    # TODO(cloudnull): Convert to a swiftutils in tripleo-common
    for obj in obj_client.get_container(container)[-1]:
        name = obj['name']
        if name.startswith('roles/') and name.endswith(('yml', 'yaml')):
            role_data = yaml.safe_load(
                obj_client.get_object(container, name)[-1]
            )
            available_yaml_roles.append(role_data[0])

    return available_yaml_roles


def list_roles(clients, container, detail=False):
    """Return a list of roles.

    :param clients: openstack clients
    :type clients: Object

    :param container: Name of swift object container
    :type container: String

    :param detail: Enable or disable extra detail
    :type detail: Boolean

    :returns: List
    """

    context = clients.tripleoclient.create_mistral_context()
    LOG.info('Pulling roles from: {}'.format(container))
    return plan.ListRolesAction(
        container=container,
        detail=detail
    ).run(context=context)

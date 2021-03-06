import functools

from six import with_metaclass

from .stdout import monitor, monitor_workflow
from .utils import CustomRegistryMeta, color_enabled
from awxkit import api
from awxkit.exceptions import NoContent


def handle_custom_actions(resource, action, page):
    key = ' '.join([resource, action])
    if key in CustomAction.registry:
        page = CustomAction.registry[key](page)
        action = 'perform'
    return page, action


class CustomActionRegistryMeta(CustomRegistryMeta):

    @property
    def name(self):
        return ' '.join([self.resource, self.action])


class CustomAction(with_metaclass(CustomActionRegistryMeta)):
    """Base class for defining a custom action for a resource."""

    def __init__(self, page):
        self.page = page

    @property
    def action(self):
        raise NotImplementedError()

    @property
    def resource(self):
        raise NotImplementedError()

    @property
    def perform(self):
        raise NotImplementedError()

    def add_arguments(self, parser):
        pass


class Launchable(object):

    def add_arguments(self, parser, with_pk=True):
        from .options import pk_or_name
        if with_pk:
            parser.choices[self.action].add_argument(
                'id',
                type=functools.partial(
                    pk_or_name, None, self.resource, page=self.page
                ),
                help=''
            )
        parser.choices[self.action].add_argument(
            '--monitor', action='store_true',
            help='If set, prints stdout of the launched job until it finishes.'
        )
        parser.choices[self.action].add_argument(
            '--timeout', type=int,
            help='If set with --monitor or --wait, time out waiting on job completion.'  # noqa
        )
        parser.choices[self.action].add_argument(
            '--wait', action='store_true',
            help='If set, waits until the launched job finishes.'
        )

    def monitor(self, response, **kwargs):
        mon = monitor_workflow if response.type == 'workflow_job' else monitor
        if kwargs.get('monitor') or kwargs.get('wait'):
            status = mon(
                response,
                self.page.connection.session,
                print_stdout=not kwargs.get('wait'),
                timeout=kwargs.get('timeout'),
            )
            if status:
                response.json['status'] = status
        return response

    def perform(self, **kwargs):
        response = self.page.get().related.get(self.action).post()
        self.monitor(response, **kwargs)
        return response


class JobTemplateLaunch(Launchable, CustomAction):
    action = 'launch'
    resource = 'job_templates'


class ProjectUpdate(Launchable, CustomAction):
    action = 'update'
    resource = 'projects'


class ProjectCreate(CustomAction):
    action = 'create'
    resource = 'projects'

    def add_arguments(self, parser):
        parser.choices[self.action].add_argument(
            '--monitor', action='store_true',
            help=('If set, prints stdout of the project update until '
                  'it finishes.')
        )
        parser.choices[self.action].add_argument(
            '--wait', action='store_true',
            help='If set, waits until the new project has updated.'
        )

    def post(self, kwargs):
        should_monitor = kwargs.pop('monitor', False)
        wait = kwargs.pop('wait', False)
        response = self.page.post(kwargs)
        if should_monitor or wait:
            update = response.related.project_updates.get(
                order_by='-created'
            ).results[0]
            monitor(
                update,
                self.page.connection.session,
                print_stdout=not wait,
            )
        return response


class InventoryUpdate(Launchable, CustomAction):
    action = 'update'
    resource = 'inventory_sources'


class AdhocCommandLaunch(Launchable, CustomAction):
    action = 'create'
    resource = 'ad_hoc_commands'

    def add_arguments(self, parser):
        Launchable.add_arguments(self, parser, with_pk=False)

    def perform(self, **kwargs):
        monitor_kwargs = {
            'monitor': kwargs.pop('monitor', False),
            'wait': kwargs.pop('wait', False),
        }
        response = self.page.post(kwargs)
        self.monitor(response, **monitor_kwargs)
        return response

    def post(self, kwargs):
        return self.perform(**kwargs)


class WorkflowLaunch(Launchable, CustomAction):
    action = 'launch'
    resource = 'workflow_job_templates'


class HasStdout(object):

    action = 'stdout'

    def add_arguments(self, parser):
        from .options import pk_or_name
        parser.choices['stdout'].add_argument(
            'id',
            type=functools.partial(
                pk_or_name, None, self.resource, page=self.page
            ),
            help=''
        )

    def perform(self):
        fmt = 'txt_download'
        if color_enabled():
            fmt = 'ansi_download'
        return self.page.connection.get(
            self.page.get().related.stdout,
            query_parameters=dict(format=fmt)
        ).content.decode('utf-8')


class JobStdout(HasStdout, CustomAction):
    resource = 'jobs'


class ProjectUpdateStdout(HasStdout, CustomAction):
    resource = 'project_updates'


class InventoryUpdateStdout(HasStdout, CustomAction):
    resource = 'inventory_updates'


class AdhocCommandStdout(HasStdout, CustomAction):
    resource = 'ad_hoc_commands'


class AssociationMixin(object):

    action = 'associate'

    def add_arguments(self, parser):
        from .options import pk_or_name
        parser.choices[self.action].add_argument(
            'id',
            type=functools.partial(
                pk_or_name, None, self.resource, page=self.page
            ),
            help=''
        )
        group = parser.choices[self.action].add_mutually_exclusive_group(required=True)
        for param, endpoint in self.targets.items():
            field, model_name = endpoint
            if not model_name:
                model_name = param
            help_text = 'The ID (or name) of the {} to {}'.format(model_name, self.action)

            class related_page(object):

                def __init__(self, connection, resource):
                    self.conn = connection
                    self.resource = {
                        'start_notification': 'notification_templates',
                        'success_notification': 'notification_templates',
                        'failure_notification': 'notification_templates',
                        'credential': 'credentials',
                    }[resource]

                def get(self, **kwargs):
                    v2 = api.Api(connection=self.conn).get().current_version.get()
                    return getattr(v2, self.resource).get(**kwargs)

            group.add_argument(
                '--{}'.format(param),
                metavar='',
                type=functools.partial(
                    pk_or_name, None, param,
                    page=related_page(self.page.connection, param)
                ),
                help=help_text
            )

    def perform(self, **kwargs):
        for k, v in kwargs.items():
            endpoint, _ = self.targets[k]
            try:
                self.page.get().related[endpoint].post(
                    {'id': v, self.action: True}
                )
            except NoContent:
                # we expect to enter this block because these endpoints return
                # HTTP 204 on success
                pass
            return self.page.get().related[endpoint].get()


class NotificationAssociateMixin(AssociationMixin):
    targets = {
        'start_notification': [
            'notification_templates_started',
            'notification_template'
        ],
        'success_notification': [
            'notification_templates_success',
            'notification_template'
        ],
        'failure_notification': [
            'notification_templates_error',
            'notification_template'
        ],
    }


class JobTemplateNotificationAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'job_templates'
    action = 'associate'
    targets = NotificationAssociateMixin.targets.copy()


class JobTemplateNotificationDisAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'job_templates'
    action = 'disassociate'
    targets = NotificationAssociateMixin.targets.copy()


JobTemplateNotificationAssociation.targets.update({
    'credential': ['credentials', None],
})
JobTemplateNotificationDisAssociation.targets.update({
    'credential': ['credentials', None],
})


class WorkflowJobTemplateNotificationAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'workflow_job_templates'
    action = 'associate'


class WorkflowJobTemplateNotificationDisAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'workflow_job_templates'
    action = 'disassociate'


class ProjectNotificationAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'projects'
    action = 'associate'


class ProjectNotificationDisAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'projects'
    action = 'disassociate'


class InventorySourceNotificationAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'inventory_sources'
    action = 'associate'


class InventorySourceNotificationDisAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'inventory_sources'
    action = 'disassociate'


class OrganizationNotificationAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'organizations'
    action = 'associate'


class OrganizationNotificationDisAssociation(NotificationAssociateMixin, CustomAction):
    resource = 'organizations'
    action = 'disassociate'


class SettingsList(CustomAction):
    action = 'list'
    resource = 'settings'

    def add_arguments(self, parser):
        parser.choices['list'].add_argument(
            '--slug', help='optional setting category/slug', default='all'
        )

    def perform(self, slug):
        self.page.endpoint = self.page.endpoint + '{}/'.format(slug)
        return self.page.get()


class RoleMixin(object):

    has_roles = [
        ['organizations', 'organization'],
        ['projects', 'project'],
        ['inventories', 'inventory'],
        ['inventory_scripts', 'inventory_script'],
        ['teams', 'team'],
        ['credentials', 'credential'],
        ['job_templates', 'job_template'],
        ['workflow_job_templates', 'workflow_job_template'],
    ]
    roles = {}  # this is calculated once

    def add_arguments(self, parser):
        from .options import pk_or_name

        if not RoleMixin.roles:
            for resource, flag in self.has_roles:
                options = self.page.__class__(
                    self.page.endpoint.replace(self.resource, resource),
                    self.page.connection
                ).options()
                RoleMixin.roles[flag] = [
                    role.replace('_role', '')
                    for role in options.json.get('object_roles', [])
                ]

        possible_roles = set()
        for v in RoleMixin.roles.values():
            possible_roles.update(v)

        resource_group = parser.choices[self.action].add_mutually_exclusive_group(
            required=True
        )
        parser.choices[self.action].add_argument(
            'id',
            type=functools.partial(
                pk_or_name, None, self.resource, page=self.page
            ),
            help='The ID (or name) of the {} to {} access to/from'.format(
                self.resource, self.action
            )
        )
        for _type in RoleMixin.roles.keys():
            if _type == 'team' and self.resource == 'team':
                # don't add a team to a team
                continue

            class related_page(object):

                def __init__(self, connection, resource):
                    self.conn = connection
                    if resource == 'inventories':
                        resource = 'inventory'  # d'oh, this is special
                    self.resource = resource

                def get(self, **kwargs):
                    v2 = api.Api(connection=self.conn).get().current_version.get()
                    return getattr(v2, self.resource).get(**kwargs)

            resource_group.add_argument(
                '--{}'.format(_type),
                type=functools.partial(
                    pk_or_name, None, _type,
                    page=related_page(
                        self.page.connection,
                        dict((v, k) for k, v in self.has_roles)[_type]
                    )
                ),
                metavar='ID',
                help='The ID (or name) of the target {}'.format(_type),
            )
        parser.choices[self.action].add_argument(
            '--role', type=str, choices=possible_roles, required=True,
            help='The name of the role to {}'.format(self.action)
        )

    def perform(self, **kwargs):
        for resource, flag in self.has_roles:
            if flag in kwargs:
                role = kwargs['role']
                if role not in RoleMixin.roles[flag]:
                    options = ', '.join(RoleMixin.roles[flag])
                    raise ValueError(
                        "invalid choice: '{}' must be one of {}".format(
                            role, options
                        )
                    )
                value = kwargs[flag]
                target = '/api/v2/{}/{}'.format(resource, value)
                detail = self.page.__class__(
                    target,
                    self.page.connection
                ).get()
                object_roles = detail['summary_fields']['object_roles']
                actual_role = object_roles[role + '_role']
                params = {'id': actual_role['id']}
                if self.action == 'grant':
                    params['associate'] = True
                if self.action == 'revoke':
                    params['disassociate'] = True

                try:
                    self.page.get().related.roles.post(params)
                except NoContent:
                    # we expect to enter this block because these endpoints return
                    # HTTP 204 on success
                    pass


class UserGrant(RoleMixin, CustomAction):

    resource = 'users'
    action = 'grant'


class UserRevoke(RoleMixin, CustomAction):

    resource = 'users'
    action = 'revoke'


class TeamGrant(RoleMixin, CustomAction):

    resource = 'teams'
    action = 'grant'


class TeamRevoke(RoleMixin, CustomAction):

    resource = 'teams'
    action = 'revoke'


class SettingsModify(CustomAction):
    action = 'modify'
    resource = 'settings'

    def add_arguments(self, parser):
        options = self.page.__class__(
            self.page.endpoint + 'all/', self.page.connection
        ).options()
        parser.choices['modify'].add_argument(
            'key',
            choices=sorted(options['actions']['PUT'].keys()),
            metavar='key',
            help=''
        )
        parser.choices['modify'].add_argument('value', help='')

    def perform(self, key, value):
        self.page.endpoint = self.page.endpoint + 'all/'
        resp = self.page.patch(**{key: value})
        return resp.from_json({'key': key, 'value': resp[key]})

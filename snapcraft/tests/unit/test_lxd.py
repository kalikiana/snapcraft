# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2016-2017 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import errno
import json
import logging
import os
import requests
from subprocess import CalledProcessError, TimeoutExpired
from unittest.mock import (
    call,
    patch,
)

import fixtures
from testtools import ExpectedException
from testtools.matchers import Contains, Equals

from snapcraft import ProjectOptions
from snapcraft.internal import lxd
from snapcraft.internal.errors import (
    ContainerConnectionError,
    ContainerRunError,
    MultipassNotInstalledError,
    MultipassSetupError,
    MultipassNetworkBridgeError,
    SnapdError,
    SnapcraftEnvironmentError,
)
from snapcraft.tests import (
    fixture_setup,
    unit
)


class LXDBaseTestCase(unit.TestCase):

    def setUp(self):
        super().setUp()
        self.fake_lxd = fixture_setup.FakeLXD()
        self.useFixture(self.fake_lxd)
        self.fake_lxd.kernel_arch = self.server
        self.fake_filesystem = fixture_setup.FakeFilesystem()
        self.useFixture(self.fake_filesystem)

        self.fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(self.fake_logger)
        self.project_options = ProjectOptions(target_deb_arch=self.target_arch)


class LXDTestCase(LXDBaseTestCase):

    scenarios = [
        ('local', dict(remote='local', target_arch=None, server='x86_64')),
        ('remote', dict(remote='myremote', target_arch=None, server='x86_64')),
        ('cross', dict(remote='local', target_arch='armhf', server='x86_64',
                       cross=True)),
        ('arm remote', dict(remote='pi', target_arch=None, server='armv7l')),
        ('arm same', dict(remote='pi', target_arch='armhf', server='armv7l')),
        ('arm cross', dict(remote='pi', target_arch='arm64', server='armv7l',
                           cross=True)),
    ]


class CleanbuilderTestCase(LXDTestCase):

    def make_containerbuild(self):
        return lxd.Cleanbuilder(output='snap.snap', source='project.tar',
                                metadata={'name': 'project'},
                                project_options=self.project_options,
                                remote=self.remote)

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    @patch('snapcraft.internal.lxd.Containerbuild._inject_snapcraft')
    @patch('petname.Generate')
    def test_cleanbuild(self, mock_pet, mock_inject, mock_container_run):
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd

        mock_pet.return_value = 'my-pet'

        project_folder = '/root/build_project'
        self.make_containerbuild().execute()

        self.assertIn('Waiting for a network connection...\n'
                      'Network connection established\n'
                      'Setting up container with project assets\n'
                      'Retrieved snap.snap\n', self.fake_logger.output)

        args = []
        if self.target_arch:
            self.assertIn('Setting target machine to \'{}\'\n'.format(
                          self.target_arch), self.fake_logger.output)
            args += ['--target-arch', self.target_arch]

        container_name = '{}:snapcraft-my-pet'.format(self.remote)
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'launch', '-e',
                  'ubuntu:xenial', container_name]),
            call(['lxc', 'config', 'set', container_name,
                  'environment.SNAPCRAFT_SETUP_CORE', '1']),
            call(['lxc', 'config', 'set', container_name,
                  'environment.LC_ALL', 'C.UTF-8']),
            call(['lxc', 'config', 'set', container_name,
                  'environment.SNAPCRAFT_IMAGE_INFO',
                  '{"fingerprint": "test-fingerprint", '
                  '"architecture": "test-architecture", '
                  '"created_at": "test-created-at"}']),
            call(['lxc', 'file', 'push', os.path.realpath('project.tar'),
                  '{}/root/build_project/project.tar'.format(container_name)]),
        ])
        mock_container_run.assert_has_calls([
            call(['python3', '-c', 'import urllib.request; ' +
                  'urllib.request.urlopen(' +
                  '"http://start.ubuntu.com/connectivity-check.html"' +
                  ', timeout=5)']),
            call(['apt-get', 'update']),
            call(['apt-get', 'install', 'squashfuse', '-y']),
            call(['mkdir', project_folder]),
            call(['tar', 'xvf', 'project.tar'],
                 cwd=project_folder),
            call(['snapcraft', 'snap', '--output', 'snap.snap', *args],
                 cwd=project_folder, user='root'),
        ])
        # Ensure there's no unexpected calls eg. two network checks
        self.assertThat(mock_container_run.call_count, Equals(6))
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'file', 'pull',
                  '{}{}/snap.snap'.format(container_name, project_folder),
                  'snap.snap']),
            call(['lxc', 'stop', '-f', container_name]),
        ])

    def test_failed_container_never_created(self):
        def call_effect(*args, **kwargs):
            if args[0][:2] == ['lxc', 'launch']:
                raise CalledProcessError(returncode=255, cmd=args[0])
            return self.fake_lxd.check_output_side_effect()(*args, **kwargs)

        self.fake_lxd.check_call_mock.side_effect = call_effect

        raised = self.assertRaises(
            ContainerConnectionError,
            self.make_containerbuild().execute)
        self.assertThat(self.fake_lxd.status, Equals(None))
        # lxc launch should fail and no further commands should come after that
        self.assertThat(str(raised), Contains('Failed to setup container'))


class ContainerbuildTestCase(LXDTestCase):

    def make_containerbuild(self):
        return lxd.Cleanbuilder(output='snap.snap', source='project.tar',
                                metadata={'name': 'project'},
                                project_options=self.project_options,
                                remote=self.remote)

    def test_parts_uri_set(self):
        self.useFixture(
            fixtures.EnvironmentVariable('SNAPCRAFT_PARTS_URI', 'foo'))
        self.make_containerbuild().execute()
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'config', 'set', self.fake_lxd.name,
                  'environment.SNAPCRAFT_PARTS_URI', 'foo']),
        ])

    def test_build_info_set(self):
        self.useFixture(
            fixtures.EnvironmentVariable(
                'SNAPCRAFT_BUILD_INFO', 'test_build_info_value'))
        self.make_containerbuild().execute()
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'config', 'set', self.fake_lxd.name,
                  'environment.SNAPCRAFT_BUILD_INFO',
                  'test_build_info_value']),
        ])

    def test_wait_for_network_loops(self):
        self.fake_lxd.check_call_mock.side_effect = CalledProcessError(
            -1, ['my-cmd'])

        builder = self.make_containerbuild()

        self.assertRaises(ContainerRunError,
                          builder._wait_for_network)

    def test_failed_build_with_debug(self):
        def call_effect(*args, **kwargs):
            if 'snapcraft snap --output snap.snap' in ' '.join(args[0]):
                raise CalledProcessError(returncode=255, cmd=args[0])
            return self.fake_lxd.check_output_side_effect()(*args, **kwargs)

        self.fake_lxd.check_call_mock.side_effect = call_effect

        self.project_options = ProjectOptions(debug=True)
        self.make_containerbuild().execute()

        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'exec', self.fake_lxd.name, '--', 'bash', '-i']),
        ])

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_failed_build_without_debug(self, mock_run):
        call_list = []

        def run_effect(*args, **kwargs):
            call_list.append(args[0])
            if args[0][:4] == ['snapcraft', 'snap', '--output', 'snap.snap']:
                raise CalledProcessError(returncode=255, cmd=args[0])

        mock_run.side_effect = run_effect

        self.assertRaises(
            CalledProcessError,
            self.make_containerbuild().execute)

        self.assertNotIn(['bash', '-i'], call_list)

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_lxc_check_fails(self, mock_run):
        self.fake_lxd.check_output_mock.side_effect = FileNotFoundError('lxc')

        with ExpectedException(
                ContainerConnectionError,
                'You must have LXD installed in order to use cleanbuild.\n'
                'Refer to the documentation at '
                'https://linuxcontainers.org/lxd/getting-started-cli.'):
            self.make_containerbuild()

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_remote_does_not_exist(self, mock_run):
        self.fake_lxd.check_output_mock.side_effect = CalledProcessError(
            255, ['lxd', 'list', self.remote])

        with ExpectedException(ContainerConnectionError,
                               'There are either.*{}.*'.format(self.remote)):
            self.make_containerbuild()

    @patch('snapcraft.internal.common.is_snap')
    def test_parallel_invocation(self, mock_is_snap):
        mock_is_snap.side_effect = lambda: False

        builder1 = self.make_containerbuild()
        builder2 = self.make_containerbuild()
        builder1.execute()
        builder2.execute()

    @patch('snapcraft.internal.common.is_snap')
    def test_parallel_invocation_inject_snap(self, mock_is_snap):
        mock_is_snap.side_effect = lambda: True

        fake_snapd = fixture_setup.FakeSnapd()
        self.useFixture(fake_snapd)
        fake_snapd.snaps_result = [
            {'name': 'core',
             'confinement': 'strict',
             'id': '2kkitQurgOkL3foImG4wDwn9CIANuHlt',
             'channel': 'stable',
             'revision': '123'},
            {'name': 'snapcraft',
             'confinement': 'classic',
             'id': '3lljuRvshPlM4gpJnH5xExo0DJBOvImu',
             'channel': 'edge',
             'revision': '345'},
        ]

        builder1 = self.make_containerbuild()
        builder2 = self.make_containerbuild()
        builder1.execute()
        builder2.execute()

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    @patch('snapcraft.internal.common.is_snap')
    def test_inject_apt(self,
                        mock_is_snap,
                        mock_container_run):
        mock_is_snap.side_effect = lambda: False

        fake_snapd = fixture_setup.FakeSnapd()
        self.useFixture(fake_snapd)

        builder = self.make_containerbuild()
        builder.execute()

        mock_container_run.assert_has_calls([
            call(['apt-get', 'install', 'squashfuse', '-y']),
            call(['apt-get', 'install', 'snapcraft', '-y']),
        ])

    @patch('snapcraft.internal.common.is_snap')
    def test_inject_socket_error(self,
                                 mock_is_snap):
        mock_is_snap.side_effect = lambda: True

        def snap_details(handler_instalce, snap_name):
            raise requests.exceptions.ConnectionError(
                'Connection aborted.',
                FileNotFoundError(2, 'No such file or directory'))

        fake_snapd = fixture_setup.FakeSnapd()
        fake_snapd.snap_details_func = snap_details
        self.useFixture(fake_snapd)

        builder = self.make_containerbuild()
        self.assertIn('Error connecting to',
                      str(self.assertRaises(SnapdError,
                                            builder.execute)))

    @patch('snapcraft.internal.common.is_snap')
    def test_inject_snap_api_error(self,
                                   mock_is_snap):
        mock_is_snap.side_effect = lambda: True

        fake_snapd = fixture_setup.FakeSnapd()
        fake_snapd.snaps_result = []
        self.useFixture(fake_snapd)

        builder = self.make_containerbuild()
        self.assertIn('Error querying \'core\' snap: not found',
                      str(self.assertRaises(SnapdError,
                                            builder.execute)))

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    @patch('snapcraft.internal.common.is_snap')
    def test_inject_snap(self,
                         mock_is_snap,
                         mock_container_run):
        mock_is_snap.side_effect = lambda: True
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd

        fake_snapd = fixture_setup.FakeSnapd()
        self.useFixture(fake_snapd)
        fake_snapd.snaps_result = [
            {'name': 'core',
             'confinement': 'strict',
             'id': '2kkitQurgOkL3foImG4wDwn9CIANuHlt',
             'channel': 'stable',
             'revision': '123'},
            {'name': 'snapcraft',
             'confinement': 'classic',
             'id': '3lljuRvshPlM4gpJnH5xExo0DJBOvImu',
             'channel': 'edge',
             'revision': '345'},
        ]

        builder = self.make_containerbuild()

        builder.execute()
        if hasattr(self, 'cross') and self.cross:
            mock_container_run.assert_has_calls([
                call(['snap', 'install', 'core', '--channel', 'stable']),
                call(['snap', 'refresh', 'core', '--channel', 'stable']),
                call(['snap', 'install', 'snapcraft', '--channel', 'edge',
                      '--classic']),
                call(['snap', 'refresh', 'snapcraft', '--channel', 'edge',
                      '--classic']),
            ])
            return

        tmp_dir = self.fake_filesystem.tmp_dir
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'file', 'push',
                  os.path.join(tmp_dir, 'core_123.assert'),
                  '{}/run/core_123.assert'.format(self.fake_lxd.name)]),
            call(['lxc', 'file', 'push',
                  os.path.join(tmp_dir, 'core_123.snap'),
                  '{}/run/core_123.snap'.format(self.fake_lxd.name)]),
            call(['lxc', 'file', 'push',
                  os.path.join(tmp_dir, 'snapcraft_345.assert'),
                  '{}/run/snapcraft_345.assert'.format(self.fake_lxd.name)]),
            call(['lxc', 'file', 'push',
                  os.path.join(tmp_dir, 'snapcraft_345.snap'),
                  '{}/run/snapcraft_345.snap'.format(self.fake_lxd.name)]),
        ])
        mock_container_run.assert_has_calls([
            call(['apt-get', 'install', 'squashfuse', '-y']),
            call(['snap', 'ack', '/run/core_123.assert']),
            call(['snap', 'install', '/run/core_123.snap']),
            call(['snap', 'ack', '/run/snapcraft_345.assert']),
            call(['snap', 'install', '/run/snapcraft_345.snap', '--classic']),
        ])

    @patch('os.getuid')
    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    @patch('snapcraft.internal.common.is_snap')
    def test_inject_snap_dangerous(self,
                                   mock_is_snap,
                                   mock_container_run,
                                   mock_getuid):
        mock_is_snap.side_effect = lambda: True
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd
        mock_getuid.return_value = 1234

        fake_snapd = fixture_setup.FakeSnapd()
        self.useFixture(fake_snapd)
        fake_snapd.snaps_result = [
            {'name': 'core',
             'confinement': 'strict',
             'id': '2kkitQurgOkL3foImG4wDwn9CIANuHlt',
             'channel': 'stable',
             'revision': '123'},
            {'name': 'snapcraft',
             'confinement': 'classic',
             'id': '',
             'channel': 'edge',
             'revision': 'x1'},
        ]

        builder = self.make_containerbuild()

        builder.execute()
        if hasattr(self, 'cross') and self.cross:
            mock_container_run.assert_has_calls([
                call(['snap', 'install', 'core', '--channel', 'stable']),
                call(['snap', 'refresh', 'core', '--channel', 'stable']),
                call(['snap', 'install', 'snapcraft', '--channel', 'edge',
                      '--classic']),
                call(['snap', 'refresh', 'snapcraft', '--channel', 'edge',
                      '--classic']),
            ])
            return

        tmp_dir = self.fake_filesystem.tmp_dir
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['sudo', 'cp', '/var/lib/snapd/snaps/snapcraft_x1.snap',
                  os.path.join(tmp_dir, 'snapcraft_x1.snap')]),
            call(['sudo', 'chown', str(os.getuid()),
                  os.path.join(tmp_dir, 'snapcraft_x1.snap')]),
            call(['lxc', 'file', 'push',
                  os.path.join(tmp_dir, 'snapcraft_x1.snap'),
                  '{}/run/snapcraft_x1.snap'.format(self.fake_lxd.name)]),
        ])
        mock_container_run.assert_has_calls([
            call(['snap', 'install', '/run/snapcraft_x1.snap',
                  '--dangerous', '--classic']),
        ])

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    @patch('snapcraft.internal.common.is_snap')
    def test_inject_snap_already_installed(self,
                                           mock_is_snap,
                                           mock_container_run):
        mock_is_snap.side_effect = lambda: True
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd

        def call_effect(*args, **kwargs):
            if args[0][:2] == ['lxc', 'exec']:
                if 'readlink' in args[0]:
                    if args[0][-1].endswith('/current'):
                        return '123\n'.encode('utf-8')
                if 'sha384sum' in args[0]:
                    if args[0][-1].endswith('core_123.snap'):
                        return 'deadbeef {}'.format(args[0][1]).encode('utf-8')
                    return 'abcdef {}'.format(args[0][1]).encode('utf-8')
            return default_side_effect(*args, **kwargs)

        default_side_effect = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        fake_snapd = fixture_setup.FakeSnapd()
        self.useFixture(fake_snapd)
        fake_snapd.snaps_result = [
            {'name': 'core',
             'confinement': 'strict',
             'id': '2kkitQurgOkL3foImG4wDwn9CIANuHlt',
             'channel': 'stable',
             'revision': '123'},
            {'name': 'snapcraft',
             'confinement': 'classic',
             'id': '',
             'channel': 'edge',
             'revision': '123'},
        ]

        builder = self.make_containerbuild()

        builder.execute()
        if hasattr(self, 'cross') and self.cross:
            mock_container_run.assert_has_calls([
                call(['snap', 'install', 'core', '--channel', 'stable']),
                call(['snap', 'refresh', 'core', '--channel', 'stable']),
                call(['snap', 'install', 'snapcraft', '--channel', 'edge',
                      '--classic']),
                call(['snap', 'refresh', 'snapcraft', '--channel', 'edge',
                      '--classic']),
            ])
            return

        tmp_dir = self.fake_filesystem.tmp_dir
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'file', 'push',
                  os.path.join(tmp_dir, 'snapcraft_123.assert'),
                  '{}/run/snapcraft_123.assert'.format(self.fake_lxd.name)]),
            call(['lxc', 'file', 'push',
                  os.path.join(tmp_dir, 'snapcraft_123.snap'),
                  '{}/run/snapcraft_123.snap'.format(self.fake_lxd.name)]),
        ])
        mock_container_run.assert_has_calls([
            call(['apt-get', 'install', 'squashfuse', '-y']),
            call(['snap', 'ack', '/run/snapcraft_123.assert']),
            call(['snap', 'install', '/run/snapcraft_123.snap', '--classic']),
        ])


class ProjectTestCase(ContainerbuildTestCase):

    scenarios = [
          ('remote/root', dict(remote='myremote', target_arch=None, euid=0,
                               server='x86_64', user='root', home='/root')),
          ('remote/user', dict(remote='myremote', target_arch=None, euid=1234,
                               server='x86_64', user='me', home='/home/me')),
          ('cross/user', dict(remote='myremote', target_arch='armhf',
                              cross=True, euid=1234,
                              server='x86_64', user='me', home='/home/me')),
    ]

    def setUp(self):
        super().setUp()
        patcher = patch('os.geteuid')
        mock_geteuid = patcher.start()
        mock_geteuid.return_value = self.euid
        self.addCleanup(patcher.stop)
        self.useFixture(
            fixtures.EnvironmentVariable('USER', self.user))

    def make_containerbuild(self):
        return lxd.Project(output='snap.snap', source='project.tar',
                           metadata={'name': 'project'},
                           project_options=self.project_options,
                           remote=self.remote)

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    @patch('snapcraft.internal.common.is_snap')
    def test_inject_snap_existing_container(self,
                                            mock_is_snap,
                                            mock_container_run):
        mock_is_snap.return_value = True

        fake_snapd = fixture_setup.FakeSnapd()
        self.useFixture(fake_snapd)
        fake_snapd.snaps_result = [
            {'name': 'core',
             'confinement': 'strict',
             'id': '2kkitQurgOkL3foImG4wDwn9CIANuHlt',
             'channel': 'stable',
             'revision': '123'},
            {'name': 'snapcraft',
             'confinement': 'classic',
             'id': '3lljuRvshPlM4gpJnH5xExo0DJBOvImu',
             'channel': 'edge',
             'revision': '345'},
        ]
        # Container was created before, and isn't running
        self.fake_lxd.name = 'myremote:snapcraft-project'
        self.fake_lxd.status = 'Stopped'

        self.make_containerbuild().execute()

        if hasattr(self, 'cross') and self.cross:
            mock_container_run.assert_has_calls([
                call(['snap', 'install', 'core', '--channel', 'stable']),
                call(['snap', 'refresh', 'core', '--channel', 'stable']),
                call(['snap', 'install', 'snapcraft', '--channel', 'edge',
                      '--classic']),
                call(['snap', 'refresh', 'snapcraft', '--channel', 'edge',
                      '--classic']),
            ])
        else:
            mock_container_run.assert_has_calls([
                call(['snap', 'ack', '/run/core_123.assert']),
                call(['snap', 'install', '/run/core_123.snap']),
                call(['snap', 'ack', '/run/snapcraft_345.assert']),
                call(['snap', 'install', '/run/snapcraft_345.snap',
                      '--classic']),
            ])

    def test_command_with_sudo(self):
        self.make_containerbuild().execute()
        project_folder = '{}/build_project'.format(self.home)
        args = ''
        if self.target_arch:
            args += ' --target-arch {}'.format(self.target_arch)
        sudo = []
        if self.user != 'root':
            sudo = ['sudo', '-H', '-E', '-u', self.user]
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'exec', self.fake_lxd.name, '--'] + sudo
                 + ['mkdir', '-p', project_folder]),
            call(['lxc', 'exec', self.fake_lxd.name, '--'] + sudo
                 + ['sh', '-c', 'cd {}; snapcraft snap --output snap.snap{}'.
                    format(project_folder, args)]),
        ])

    @patch('os.getuid')
    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_user_setup(self, mock_container_run, mock_getuid):
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd
        mock_getuid.return_value = 1234

        self.make_containerbuild().execute()

        if self.euid > 0:
            mock_container_run.assert_has_calls([
                call(['useradd', self.user, '--create-home']),
                call(['usermod', self.user, '-o', '-u', '1234', '-G', 'sudo']),
                call(['chown', 'me:me', self.home]),
            ])
            self.fake_lxd.check_output_mock.assert_has_calls([
                call(['lxc', 'exec', self.fake_lxd.name, '--',
                      'tee', '-a', '/etc/sudoers'],
                     input='{} ALL=(ALL) NOPASSWD: ALL\n'.format(
                         self.user).encode()),
                call(['lxc', 'exec', self.fake_lxd.name, '--',
                      'sudo', '-H', '-u', 'me', 'ls',
                      '{}/build_project'.format(self.home)]),
            ])

    def test_init_failed(self):
        def call_effect(*args, **kwargs):
            if args[0][:2] == ['lxc', 'init']:
                raise CalledProcessError(returncode=255, cmd=args[0])
            return self.fake_lxd.check_output_side_effect()(*args, **kwargs)

        self.fake_lxd.check_call_mock.side_effect = call_effect

        raised = self.assertRaises(ContainerConnectionError,
                                   self.make_containerbuild().execute)
        self.assertThat(self.fake_lxd.status, Equals(None))
        # lxc launch should fail and no further commands should come after that
        self.assertThat(str(raised), Contains('Failed to setup container'))

    def test_start_failed(self):
        def call_effect(*args, **kwargs):
            if args[0][:2] == ['lxc', 'start']:
                raise CalledProcessError(returncode=255, cmd=args[0])
            return self.fake_lxd.check_output_side_effect()(*args, **kwargs)

        self.fake_lxd.check_call_mock.side_effect = call_effect

        raised = self.assertRaises(ContainerConnectionError,
                                   self.make_containerbuild().execute)
        self.assertThat(self.fake_lxd.status, Equals('Stopped'))
        # lxc launch should fail and no further commands should come after that
        self.assertThat(str(raised),
                        Contains('The container could not be started'))

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_ftp_not_installed(self, mock_container_run):
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd

        def call_effect(*args, **kwargs):
            if args[0][:1] == ['/usr/lib/sftp-server']:
                raise FileNotFoundError(
                    2, 'No such file or directory')

        self.fake_lxd.popen_mock.side_effect = call_effect

        self.assertIn(
            'You must have openssh-sftp-server installed to use a LXD '
            'remote on a different host.\n',
            str(self.assertRaises(
                SnapcraftEnvironmentError,
                self.make_containerbuild().execute)))

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_ftp_error(self, mock_container_run):
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd

        def call_effect(*args, **kwargs):
            if args[0][:1] == ['/usr/lib/sftp-server']:
                raise CalledProcessError(
                    returncode=255, cmd=args[0])

        self.fake_lxd.popen_mock.side_effect = call_effect

        self.assertIn(
            'sftp-server seems to be installed but could not be run.\n',
            str(self.assertRaises(
                SnapcraftEnvironmentError,
                self.make_containerbuild().execute)))

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_sshfs_failed(self, mock_container_run):
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd

        def call_effect(*args, **kwargs):
            if args[0][:2] == ['lxc', 'exec'] and 'ls' in args[0]:
                return ''.encode('utf-8')
            return self.fake_lxd.check_output_side_effect()(*args, **kwargs)

        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.assertIn(
            'The project folder could not be mounted.\n',
            str(self.assertRaises(
                ContainerConnectionError,
                self.make_containerbuild().execute)))

    def test_debug_messages(self):
        class TestFormatter(logging.Formatter):
            def format(self, record):
                message = super().format(record)
                return '[{}]{}'.format(record.levelname, message)

        self.fake_logger = fixtures.FakeLogger(
            format='%(message)s', formatter=TestFormatter, level=logging.DEBUG)
        self.useFixture(self.fake_logger)

        self.make_containerbuild().execute()
        self.assertThat(self.fake_logger.output,
                        Contains('[DEBUG]Terminating'))


class LocalProjectTestCase(ContainerbuildTestCase):

    scenarios = [
        ('local', dict(remote='local', target_arch=None, server='x86_64')),
    ]

    def make_containerbuild(self):
        return lxd.Project(output='snap.snap', source='project.tar',
                           metadata={'name': 'project'},
                           project_options=self.project_options,
                           remote=self.remote)

    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_start_failed(self, mock_container_run):
        mock_container_run.side_effect = lambda cmd, **kwargs: cmd

        def call_effect(*args, **kwargs):
            if args[0][:2] == ['lxc', 'start']:
                raise CalledProcessError(
                    returncode=255, cmd=args[0])
            return d(*args, **kwargs)

        d = self.fake_lxd.check_call_mock.side_effect
        self.fake_lxd.check_call_mock.side_effect = call_effect

        self.assertIn(
            'The container could not be started.\n'
            'The files /etc/subuid and /etc/subgid need to contain this line ',
            str(self.assertRaises(
                ContainerConnectionError,
                self.make_containerbuild().execute)))
        # Should not attempt to stop a container that wasn't started
        self.assertNotIn(call(['lxc', 'stop', '-f', self.fake_lxd.name]),
                         self.fake_lxd.check_call_mock.call_args_list)


class MultipassTestCase(LXDBaseTestCase):

    server = 'x86_64'
    target_arch = None

    def make_containerbuild(self):
        return lxd.Project(output='snap.snap', source='project.tar',
                           metadata={'name': 'project'},
                           project_options=self.project_options,
                           remote='multipass')

    def test_lxd_client_not_installed(self):
        def call_effect(*args, **kwargs):
            if args[0][:1] == ['lxc']:
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), args[0])
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.assertIn(
            'You must have LXD installed in order to use Multipass.',
            str(self.assertRaises(
                ContainerConnectionError,
                self.make_containerbuild)))

    def test_multipass_not_installed(self):
        def call_effect(*args, **kwargs):
            if args[0][:1] == ['multipass']:
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), args[0])
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.assertIn(
            'Multipass is not installed',
            str(self.assertRaises(
                MultipassNotInstalledError,
                self.make_containerbuild)))

    def test_start_failed(self):
        def call_effect(*args, **kwargs):
            if args[0][:2] == ['multipass', 'start']:
                raise CalledProcessError(
                    returncode=255, cmd=args[0])
            elif args[0][:2] == ['multipass', 'info']:
                return '''{"info":{
                    "snapcraft":{
                        "mounts": {}}}}'''.encode()
            return d(*args, **kwargs)

        d = self.fake_lxd.check_call_mock.side_effect
        self.fake_lxd.check_call_mock.side_effect = call_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.assertIn(
            'Failed to setup multipass remote',
            str(self.assertRaises(
                MultipassSetupError,
                self.make_containerbuild)))

    def test_lxd_not_installed_in_vm(self):
        def call_effect(*args, **kwargs):
            if args[0][-2:] == ['snap', 'list']:
                return ''.encode()
            elif args[0][:2] == ['multipass', 'info']:
                return '''{"info":{
                    "snapcraft":{
                        "ipv4": ["1.2.3.4"]}}}'''.encode()
            elif args[0][-2:] == ['ifconfig', 'lxdbr0']:
                return 'inet addr:10.10.10.1'.encode()
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.make_containerbuild()
        self.fake_lxd.check_output_mock.assert_has_calls([
            call(['multipass', 'exec', 'snapcraft', '--',
                  'sudo', 'snap', 'install', 'lxd']),
        ])

    def test_lxd_init_needed(self):
        def call_effect(*args, **kwargs):
            if args[0][-2:] == ['snap', 'list']:
                return 'lxd 2.21 5408 canonical -'.encode()
            elif args[0][:2] == ['multipass', 'info']:
                return '''{"info":{
                    "snapcraft":{
                        "ipv4": ["1.2.3.4"]}}}'''.encode()
            elif args[0][-2:] == ['ifconfig', 'lxdbr0']:
                return 'inet addr:10.10.10.1'.encode()
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.make_containerbuild()
        self.fake_lxd.check_output_mock.assert_has_calls([
            call(['multipass', 'exec', 'snapcraft', '--',
                  'sudo', '/snap/bin/lxd', 'waitready']),
            call(['multipass', 'exec', 'snapcraft', '--',
                  '/snap/bin/lxc', 'config', 'show']),
            call(['multipass', 'exec', 'snapcraft', '--',
                  'sudo', '/snap/bin/lxd', 'init', '--auto',
                  '--network-address', '0.0.0.0',
                  '--network-port', '8443',
                  '--trust-password', 'snapcraft']),
            call(['multipass', 'exec', 'snapcraft', '--',
                  '/snap/bin/lxc', 'network', 'list']),
            call(['multipass', 'exec', 'snapcraft', '--',
                  'sudo', '/snap/bin/lxc', 'network',
                  'create', 'lxdbr0']),
            call(['multipass', 'exec', 'snapcraft', '--',
                  'sudo', '/snap/bin/lxc', 'network',
                  'attach-profile', 'lxdbr0', 'default', 'eth0']),
        ])
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'remote', 'add', 'multipass', '1.2.3.4',
                  '--password=snapcraft', '--accept-certificate']),
        ])

    def test_bridge_timeout(self):
        def call_effect(*args, **kwargs):
            if args[0][-2:] == ['ifconfig', 'lxdbr0']:
                raise TimeoutExpired(args[0], 5)
            elif args[0][:2] == ['multipass', 'info']:
                return '''{"info":{
                    "snapcraft":{
                        "ipv4": ["1.2.3.4"]}}}'''.encode()
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.assertIn(
            'Failed to setup LXD bridge',
            str(self.assertRaises(
                MultipassNetworkBridgeError,
                self.make_containerbuild)))

    def test_lxd_configured(self):
        def call_effect(*args, **kwargs):
            if args[0][-2:] == ['snap', 'list']:
                return 'lxd 2.21 5408 canonical -'.encode()
            elif args[0][-3:] == ['/snap/bin/lxc', 'config', 'show']:
                return 'core.trust_password: true'.encode()
            elif args[0][-3:] == ['/snap/bin/lxc', 'network', 'list']:
                return 'lxdbr0 | bridge | YES | | 1'.encode()
            elif args[0][:2] == ['multipass', 'info']:
                return '''{"info":{
                    "snapcraft":{
                        "ipv4": ["1.2.3.4"]}}}'''.encode()
            elif args[0][-2:] == ['ifconfig', 'lxdbr0']:
                return 'inet addr:10.10.10.1'.encode()
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.make_containerbuild()
        self.fake_lxd.check_output_mock.assert_has_calls([
            call(['multipass', 'exec', 'snapcraft', '--',
                  'sudo', '/snap/bin/lxd', 'waitready']),
        ])
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['lxc', 'remote', 'add', 'multipass', '1.2.3.4',
                  '--password=snapcraft', '--accept-certificate']),
        ])

    @patch('snapcraft.internal.lxd.Containerbuild._setup_multipass_remote')
    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_mount(self, mock_container_run, mock_remote):
        def call_effect(*args, **kwargs):
            if args[0][:2] == ['multipass', 'info']:
                return '''{"info":{
                    "snapcraft":{
                        "mounts": {"/root/build_project": {}}}}}'''.encode()
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        project_folder = '/root/build_project'
        self.make_containerbuild().execute()
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['multipass', 'mount', '{}/project.tar'.format(os.getcwd()),
                  'snapcraft:{}'.format(project_folder)]),
            call(['lxc', 'config', 'device', 'add',
                  self.fake_lxd.name, project_folder, 'disk',
                  'source={}'.format(project_folder),
                  'path={}'.format(project_folder)]),
        ])

    @patch('snapcraft.internal.lxd.Containerbuild._setup_multipass_remote')
    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_mount_exists(self, mock_container_run, mock_remote):
        project_folder = '/root/build_project'

        def call_effect(*args, **kwargs):
            if args[0][:2] == ['multipass', 'info']:
                return '''{"info":{
                    "snapcraft":{
                        "mounts": {"/root/build_project": {}}}}}'''.encode()
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.make_containerbuild().execute()
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['multipass', 'unmount',
                  'snapcraft:{}'.format(project_folder)]),
        ])

    @patch('snapcraft.internal.lxd.Containerbuild._setup_multipass_remote')
    @patch('snapcraft.internal.lxd.Containerbuild._container_run')
    def test_instance_stopped(self, mock_container_run, mock_remote):

        def call_effect(*args, **kwargs):
            if args[0][:2] == ['multipass', 'info']:
                return '''{"info":{
                    "snapcraft":{
                        "mounts": {}}}}'''.encode()
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect
        self.fake_lxd.check_output_mock.side_effect = call_effect

        mock_container_run.side_effect = lambda cmd, **kwargs: cmd
        self.make_containerbuild().execute()
        self.fake_lxd.check_call_mock.assert_has_calls([
            call(['multipass', 'stop', 'snapcraft']),
        ])


class FailedImageInfoTestCase(LXDBaseTestCase):

    remote = 'local'
    server = 'x86_64'
    target_arch = None

    scenarios = [
        ('CalledProcessError', dict(
            exception=CalledProcessError,
            kwargs=dict(cmd='testcmd', returncode=1, output='test output'),
            expected_warn=(
                "Failed to get container image info: "
                "`lxc image list --format=json ubuntu:xenial` "
                "returned with exit code 1, output: test output\n"
                "It will not be recorded in manifest.\n"))),
        ('JSONDecodeError', dict(
            exception=json.decoder.JSONDecodeError,
            kwargs=dict(msg='dummy', doc='dummy', pos=1),
            expected_warn=(
                "Failed to get container image info: Not in JSON format\n"
                "It will not be recorded in manifest.\n"))),
    ]

    def make_containerbuild(self):
        return lxd.Project(output='snap.snap', source='project.tar',
                           metadata={'name': 'project'},
                           project_options=self.project_options,
                           remote=self.remote)

    def test_failed_image_info_just_warns(self):
        self.fake_logger = fixtures.FakeLogger(level=logging.WARN)
        self.useFixture(self.fake_logger)

        def call_effect(*args, **kwargs):
            if args[0][:3] == ['lxc', 'image', 'list']:
                raise self.exception(**self.kwargs)
            return d(*args, **kwargs)

        d = self.fake_lxd.check_output_mock.side_effect

        self.fake_lxd.check_output_mock.side_effect = call_effect

        self.make_containerbuild().execute()
        self.assertEqual(self.fake_logger.output, self.expected_warn)

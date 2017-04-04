# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2015 Canonical Ltd
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

import logging
import os
import os.path
import subprocess
import tarfile
from testtools.matchers import Contains
from unittest import mock

import fixtures
from testtools.matchers import (
    FileContains,
    FileExists,
    Not,
)

from snapcraft.main import main
from snapcraft import tests
from snapcraft.tests.test_lxd import check_output_side_effect


class SnapCommandContainerized(tests.TestCase):

    yaml_template = """name: snap-test
version: 1.0
summary: test containerized snap
description: if snap is succesful a snap package will be available
architectures: ['amd64']
type: {}
confinement: strict
grade: stable

parts:
    part1:
      plugin: nil
"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch('snapcraft.internal.lxd.check_call')
        self.check_call_mock = patcher.start()
        self.addCleanup(patcher.stop)

        patcher = mock.patch('snapcraft.internal.lxd.check_output')
        self.check_output_mock = patcher.start()
        self.check_output_mock.side_effect = check_output_side_effect()
        self.addCleanup(patcher.stop)

        patcher = mock.patch('snapcraft.internal.lxd.sleep', lambda _: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_snapcraft_yaml(self, n=1):
        super().make_snapcraft_yaml(self.yaml_template)
        self.state_dir = os.path.join(self.parts_dir, 'part1', 'state')

    def test_snap_defaults(self):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)
        self.useFixture(fixtures.EnvironmentVariable(
                'SNAPCRAFT_CONTAINER_BUILDS', '1'))

        self.make_snapcraft_yaml()
        # simulate build artifacts

        dirs = [
            os.path.join(self.parts_dir, 'part1', 'src'),
            self.stage_dir,
            self.prime_dir,
            os.path.join(self.parts_dir, 'plugins'),
        ]
        files_tar = [
            os.path.join(self.parts_dir, 'plugins', 'x-plugin.py'),
            'main.c',
        ]
        files_no_tar = [
            os.path.join(self.stage_dir, 'binary'),
            os.path.join(self.prime_dir, 'binary'),
            'snap-test.snap',
            'snap-test_1.0_source.tar.bz2',
        ]
        for d in dirs:
            os.makedirs(d)
        for f in files_tar + files_no_tar:
            open(f, 'w').close()

        main(['snap', '--debug'])

        self.assertIn(
            'Setting up container with project assets\n'
            'Waiting for a network connection...\n'
            'Network connection established\n'
            'Retrieved snap-test_1.0_amd64.snap\n',
            fake_logger.output)

        with tarfile.open('snap-test_1.0_source.tar.bz2') as tar:
            tar_members = tar.getnames()

        for f in files_no_tar:
            f = os.path.relpath(f)
            self.assertFalse('./{}'.format(f) in tar_members,
                             '{} should not be in {}'.format(f, tar_members))
        for f in files_tar:
            f = os.path.relpath(f)
            self.assertTrue('./{}'.format(f) in tar_members,
                            '{} should be in {}'.format(f, tar_members))

        # Also assert that the snapcraft.yaml made it into the cleanbuild tar
        self.assertThat(
            tar_members,
            Contains(os.path.join('.', 'snap', 'snapcraft.yaml')),
            'snap/snapcraft unexpectedly excluded from tarball')


class SnapCommandTestCase(tests.TestCase):

    yaml_template = """name: snap-test
version: 1.0
summary: test snapping
description: if snap is succesful a snap package will be available
architectures: ['amd64']
type: {}
confinement: strict
grade: stable

parts:
    part1:
      plugin: nil
"""

    def setUp(self):
        super().setUp()

        patcher = mock.patch('snapcraft.internal.lifecycle.Popen',
                             new=mock.Mock(wraps=subprocess.Popen))
        self.popen_spy = patcher.start()
        self.addCleanup(patcher.stop)

        # Avoiding a io.UnsupportedOperation: fileno
        patcher = mock.patch('sys.stdout.fileno')
        self.fileno_mock = patcher.start()
        self.fileno_mock.return_value = 1
        self.addCleanup(patcher.stop)

        patcher = mock.patch('os.isatty')
        self.isatty_mock = patcher.start()
        self.isatty_mock.return_value = False
        self.addCleanup(patcher.stop)

    def make_snapcraft_yaml(self, n=1, snap_type='app'):
        snapcraft_yaml = self.yaml_template.format(snap_type)
        super().make_snapcraft_yaml(snapcraft_yaml)
        self.state_dir = os.path.join(self.parts_dir, 'part1', 'state')

    def test_snap_defaults(self):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)
        self.make_snapcraft_yaml()

        main(['snap'])

        self.assertEqual(
            'Preparing to pull part1 \n'
            'Pulling part1 \n'
            'Preparing to build part1 \n'
            'Building part1 \n'
            'Staging part1 \n'
            'Priming part1 \n'
            'Snapping \'snap-test\' ...\n'
            'Snapped snap-test_1.0_amd64.snap\n',
            fake_logger.output)

        self.assertTrue(os.path.exists(self.stage_dir),
                        'Expected a stage directory')

        self.verify_state('part1', self.state_dir, 'prime')

        self.popen_spy.assert_called_once_with([
            'mksquashfs', self.prime_dir, 'snap-test_1.0_amd64.snap',
            '-noappend', '-comp', 'xz', '-no-xattrs', '-all-root'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)

    @mock.patch('snapcraft.internal.lifecycle.ProgressBar')
    def test_snap_defaults_on_a_tty(self, progress_mock):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)
        self.make_snapcraft_yaml()
        self.isatty_mock.return_value = True

        main(['snap'])

        self.assertEqual(
            'Preparing to pull part1 \n'
            'Pulling part1 \n'
            'Preparing to build part1 \n'
            'Building part1 \n'
            'Staging part1 \n'
            'Priming part1 \n'
            'Snapped snap-test_1.0_amd64.snap\n',
            fake_logger.output)

        self.assertTrue(os.path.exists(self.stage_dir),
                        'Expected a stage directory')

        self.verify_state('part1', self.state_dir, 'prime')

        self.popen_spy.assert_called_once_with([
            'mksquashfs', self.prime_dir, 'snap-test_1.0_amd64.snap',
            '-noappend', '-comp', 'xz', '-no-xattrs', '-all-root'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)

        self.assertThat('snap-test_1.0_amd64.snap', FileExists())

    def test_snap_type_os_does_not_use_all_root(self):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)
        self.make_snapcraft_yaml(snap_type='os')

        main(['snap'])

        self.assertEqual(
            'Preparing to pull part1 \n'
            'Pulling part1 \n'
            'Preparing to build part1 \n'
            'Building part1 \n'
            'Staging part1 \n'
            'Priming part1 \n'
            'Snapping \'snap-test\' ...\n'
            'Snapped snap-test_1.0_amd64.snap\n',
            fake_logger.output)

        self.assertTrue(os.path.exists(self.stage_dir),
                        'Expected a stage directory')

        self.verify_state('part1', self.state_dir, 'prime')

        self.popen_spy.assert_called_once_with([
            'mksquashfs', self.prime_dir, 'snap-test_1.0_amd64.snap',
            '-noappend', '-comp', 'xz', '-no-xattrs'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)

        self.assertThat('snap-test_1.0_amd64.snap', FileExists())

    def test_snap_defaults_with_parts_in_prime(self):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)
        self.make_snapcraft_yaml()

        # Pretend this part has already been primed
        os.makedirs(self.state_dir)
        open(os.path.join(self.state_dir, 'prime'), 'w').close()

        main(['snap'])

        self.assertEqual(
            'Skipping pull part1 (already ran)\n'
            'Skipping build part1 (already ran)\n'
            'Skipping stage part1 (already ran)\n'
            'Skipping prime part1 (already ran)\n'
            'Snapping \'snap-test\' ...\n'
            'Snapped snap-test_1.0_amd64.snap\n',
            fake_logger.output)

        self.popen_spy.assert_called_once_with([
            'mksquashfs', self.prime_dir, 'snap-test_1.0_amd64.snap',
            '-noappend', '-comp', 'xz', '-no-xattrs', '-all-root'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)

        self.assertThat('snap-test_1.0_amd64.snap', FileExists())

    def test_snap_from_dir(self):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)

        meta_dir = os.path.join('mysnap', 'meta')
        os.makedirs(meta_dir)
        with open(os.path.join(meta_dir, 'snap.yaml'), 'w') as f:
            f.write("""name: my_snap
version: 99
architectures: [amd64, armhf]
""")

        main(['snap', 'mysnap'])

        self.assertEqual(
            'Snapping \'my_snap\' ...\n'
            'Snapped my_snap_99_multi.snap\n',
            fake_logger.output)

        self.popen_spy.assert_called_once_with([
            'mksquashfs', os.path.abspath('mysnap'), 'my_snap_99_multi.snap',
            '-noappend', '-comp', 'xz', '-no-xattrs', '-all-root'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)

        self.assertThat('my_snap_99_multi.snap', FileExists())

    def test_snap_from_dir_with_no_arch(self):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)

        meta_dir = os.path.join('mysnap', 'meta')
        os.makedirs(meta_dir)
        with open(os.path.join(meta_dir, 'snap.yaml'), 'w') as f:
            f.write("""name: my_snap
version: 99
""")

        main(['snap', 'mysnap'])

        self.assertEqual(
            'Snapping \'my_snap\' ...\n'
            'Snapped my_snap_99_all.snap\n',
            fake_logger.output)

        self.popen_spy.assert_called_once_with([
            'mksquashfs', os.path.abspath('mysnap'), 'my_snap_99_all.snap',
            '-noappend', '-comp', 'xz', '-no-xattrs', '-all-root'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)

        self.assertThat('my_snap_99_all.snap', FileExists())

    def test_snap_from_dir_type_os_does_not_use_all_root(self):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)

        meta_dir = os.path.join('mysnap', 'meta')
        os.makedirs(meta_dir)
        with open(os.path.join(meta_dir, 'snap.yaml'), 'w') as f:
            f.write("""name: my_snap
version: 99
architectures: [amd64, armhf]
type: os
""")

        main(['snap', 'mysnap'])

        self.assertEqual(
            'Snapping \'my_snap\' ...\n'
            'Snapped my_snap_99_multi.snap\n',
            fake_logger.output)

        self.popen_spy.assert_called_once_with([
            'mksquashfs', os.path.abspath('mysnap'), 'my_snap_99_multi.snap',
            '-noappend', '-comp', 'xz', '-no-xattrs'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)

        self.assertThat('my_snap_99_multi.snap', FileExists())

    def test_snap_with_output(self):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)
        self.make_snapcraft_yaml()

        main(['snap', '--output', 'mysnap.snap'])

        self.assertEqual(
            'Preparing to pull part1 \n'
            'Pulling part1 \n'
            'Preparing to build part1 \n'
            'Building part1 \n'
            'Staging part1 \n'
            'Priming part1 \n'
            'Snapping \'snap-test\' ...\n'
            'Snapped mysnap.snap\n',
            fake_logger.output)

        self.assertTrue(os.path.exists(self.stage_dir),
                        'Expected a stage directory')

        self.verify_state('part1', self.state_dir, 'prime')

        self.popen_spy.assert_called_once_with([
            'mksquashfs', self.prime_dir, 'mysnap.snap',
            '-noappend', '-comp', 'xz', '-no-xattrs', '-all-root'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)

        self.assertThat('mysnap.snap', FileExists())

    @mock.patch('time.time')
    def test_snap_renames_stale_snap_build(self, mocked_time):
        fake_logger = fixtures.FakeLogger(level=logging.INFO)
        self.useFixture(fake_logger)
        self.make_snapcraft_yaml()

        mocked_time.return_value = 1234

        snap_build = 'snap-test_1.0_amd64.snap-build'
        with open(snap_build, 'w') as fd:
            fd.write('signed assertion?')

        main(['snap'])

        snap_build_renamed = snap_build + '.1234'
        self.assertEqual([
            'Preparing to pull part1 ',
            'Pulling part1 ',
            'Preparing to build part1 ',
            'Building part1 ',
            'Staging part1 ',
            'Priming part1 ',
            'Renaming stale build assertion to {}'.format(snap_build_renamed),
            'Snapping \'snap-test\' ...',
            'Snapped snap-test_1.0_amd64.snap',
            ], fake_logger.output.splitlines())

        self.assertThat('snap-test_1.0_amd64.snap', FileExists())
        self.assertThat(snap_build, Not(FileExists()))
        self.assertThat(snap_build_renamed, FileExists())
        self.assertThat(
            snap_build_renamed, FileContains('signed assertion?'))

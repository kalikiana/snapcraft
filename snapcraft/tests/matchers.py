# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2017 Canonical Ltd
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

import magic
import testtools


class HasBinaryFileHeader:
    def __init__(self, expected_magic):
        """Determines the magic of a file, or in other words matches a file
        against known patterns to determine attributes like its MIME type
        or architecture.
        """
        self._expected_magic = expected_magic
        self._ms = magic.open(magic.NONE)
        self._ms.load()

    def __str__(self):
        return '{}()'.format(self.__name__)


class HasLinkage(HasBinaryFileHeader):
    """Match if the file has static or dynamic linkage"""

    def match(self, file_path):
        magic = self._ms.file(file_path)
        # Catch exceptions on splitting the string to provide context
        # This includes "cannot open `...' (No such file or directory)"
        try:
            linkage = magic.split(',')[3]
        except IndexError as e:
            raise ValueError('Failed to parse magic {!r}'.format(magic)) from e
        if self._expected_magic not in linkage:
            return testtools.matchers.Mismatch(
                'Expected {!r} to be in {!r}'.format(
                    self._expected_magic, linkage))


class HasArchitecture(HasBinaryFileHeader):
    """Match if the file was built for the expected architecture"""

    def match(self, file_path):
        magic = self._ms.file(file_path)
        # Catch exceptions on splitting the string to provide context
        # This includes "cannot open `...' (No such file or directory)"
        try:
            arch = magic.split(',')[1]
        except IndexError as e:
            raise ValueError('Failed to parse magic {!r}'.format(magic)) from e
        if self._expected_magic not in arch:
            return testtools.matchers.Mismatch(
                'Expected {!r} to be in {!r}'.format(
                    self._expected_arch, arch))

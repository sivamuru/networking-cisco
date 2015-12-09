# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2015 Cisco Systems, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

from netns import Namespace, nslist, iflist, os, subprocess
import sys
import mock
import unittest


class TestNetNs(unittest.TestCase):

    def setUp(self):
        super(TestNetNs, self).setUp()

    @mock.patch('os.path')
    @mock.patch('os.listdir')
    def test_nslist(self, mock_listdir, mock_path):
        mock_path.exists.return_value = True
        mock_listdir.return_value = []
        mock_listdir.return_value.append(('qdhcp-1111111-2222222-3333333'))
        mock_listdir.return_value.append(('qdhcp-4444444-5555555-6666666'))
        nsdirs = nslist()
        self.assertEqual(nsdirs[0], 'qdhcp-1111111-2222222-3333333')
        self.assertEqual(nsdirs[1], 'qdhcp-4444444-5555555-6666666')

        mock_path.exists.return_value = False
        nsdirs = nslist()
        self.assertEqual(nsdirs, [])

    @mock.patch('subprocess.check_output')
    def test_iflist(self, mock_check_output):
        fh = open('tests/unit/data/ip_addr_show.txt', 'rb')
        ip_addr_str = fh.read()
        fh.close()
        mock_check_output.return_value = ip_addr_str
        interfaces = iflist()
        name, addr, mask = interfaces[0]
        self.assertEqual(name, 'lo')
        self.assertEqual(addr, '127.0.0.1')
        self.assertEqual(mask, '8')
        name, addr, mask = interfaces[1]
        self.assertEqual(name, 'eth0')
        self.assertEqual(addr, '10.1.1.1')
        self.assertEqual(mask, '24')

        # check ignore option
        interfaces = iflist(ignore=("lo",))
        name, addr, mask = interfaces[0]
        self.assertEqual(name, 'eth0')
        self.assertEqual(addr, '10.1.1.1')
        self.assertEqual(mask, '24')
        with self.assertRaises(IndexError) as context:
            name, addr, mask = interfaces[1]

        interfaces = iflist(ignore=("eth0",))
        name, addr, mask = interfaces[0]
        self.assertEqual(name, 'lo')
        self.assertEqual(addr, '127.0.0.1')
        self.assertEqual(mask, '8')
        with self.assertRaises(IndexError) as context:
            name, addr, mask = interfaces[1]

        # test with no input
        mock_check_output.return_value = ''
        interfaces = iflist()
        with self.assertRaises(IndexError) as context:
            name, addr, mask = interfaces[0]
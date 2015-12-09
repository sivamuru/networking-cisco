# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2014 Cisco Systems, Inc.  All rights reserved.
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
# @author: Matt Caulfield, Cisco Systems, Inc.


import socket
import os
import struct
import binascii

import eventlet
from oslo.config import cfg

from neutron.common import config
from neutron.openstack.common import log as logging
from neutron.plugins.cisco.cpnr import netns
from neutron.plugins.cisco.cpnr import debug_stats

LOG = logging.getLogger(__name__)

DEBUG_STATS_MIN_WRITE_INTERVAL = 30
MONITOR_INTERVAL = 1
RECV_BUFFER_SIZE = 4096
NS_RELAY_PENDING = 'NS_RELAY_PENDING'
NS_RELAY_RUNNING = 'NS_RELAY_RUNNING'
NS_RELAY_DELETING = 'NS_RELAY_DELETING'
DHCP_CLIENT_PORT = 68
DHCP_SERVER_PORT = 67
RLIMIT_NOFILE_LIMIT = 16384

OPTS = [
    cfg.StrOpt('external_interface',
               default='lo',
               help=_('Interface for communicating with DHCP/DNS server')),
    cfg.StrOpt('dhcp_server_addr',
               default='127.0.0.1',
               help=_('DHCP server IP address')),
    cfg.IntOpt('dhcp_server_port',
               default=67,
               help=_('DHCP server UDP port number')),
    cfg.BoolOpt('enable_dhcp_stats',
                default=False,
                help=_('Enable DHCP stats')),
    cfg.IntOpt('dhcp_stats_interval',
               default=60,
               help=_('DHCP stats polling interval'))
]


class DhcpRelayAgent(object):
    """Relay DHCP packets between neutron networks and external DHCP server.

    Receives broadcast and unicast DHCP requests via sockets which are opened
    in each neutron dhcp network namespace.  Additional DHCP options are
    appended to the request to indicate from which network the request
    originated. Requests are then forwarded to the configured DHCP server
    address.

    Receives unicast DHCP responses from the DHCP server via socket opened in
    the global network namespace.  Additional options are stripped from the
    response. The response is then forwarded to the originating network.
    """

    def __init__(self):
        self.conf = cfg.CONF
        self.ns_states = {}
        self.int_sockets_by_vpn = {}
        self.ext_sock = None
        self.ext_addr = ""
        self.ns_lock = eventlet.semaphore.Semaphore()
        self.int_sock_retries = 0
        self.debug_stats = debug_stats.DebugStats('dhcp')

    def serve(self):
        self.greenpool = eventlet.GreenPool(3)
        self.greenpool.spawn_n(self._server_network_relay)
        self.greenpool.spawn_n(self._namespace_monitor)
        if self.conf.cisco_pnr.enable_dhcp_stats:
            self.greenpool.spawn_n(self._write_debug_stats)
        self.greenpool.waitall()

    def _namespace_monitor(self):

        while True:
            eventlet.sleep(MONITOR_INTERVAL)

            # Get list of network namespaces on system
            try:
                curr_ns = set(netns.nslist())
            except Exception:
                LOG.error(_('Failed to get current namespace set'))
                continue

            # For each unknown namespace, start a relay thread
            for ns in curr_ns:
                if not ns.startswith("qdhcp") or ns in self.ns_states:
                    continue
                self.ns_states[ns] = NS_RELAY_PENDING
                eventlet.spawn_n(self._client_network_relay, ns)

            # Set state to DELETING for any unknown namespaces
            for ns in self.ns_states:
                if ns in curr_ns:
                    continue
                self.ns_states[ns] = NS_RELAY_DELETING

    def _server_network_relay(self):

        # Open a socket in the global namespace for DHCP
        try:
            self.ext_sock, self.ext_addr = self._open_dhcp_ext_socket()
        except Exception:
            LOG.exception(_('Failed to open dhcp external socket in '
                            'global ns'))
            return
        recvbuf = bytearray(RECV_BUFFER_SIZE)

        # Forward DHCP responses from external to internal networks
        while True:
            try:
                size = self.ext_sock.recv_into(recvbuf)
                pkt = DhcpPacket.parse(recvbuf)
                vpnid = pkt.get_relay_option(151)
                ciaddr = pkt.get_ciaddr()
                if vpnid not in self.int_sockets_by_vpn:
                    continue
                int_sock = self.int_sockets_by_vpn[vpnid]
                self.debug_stats.increment_pkts_from_server(vpnid)
                if ciaddr == "0.0.0.0":
                    ciaddr = "255.255.255.255"
                LOG.debug(_('Forwarding DHCP response for vpn %s'), vpnid)
                int_sock.sendto(recvbuf[:size], (ciaddr, DHCP_CLIENT_PORT))
                self.debug_stats.increment_pkts_to_client(vpnid)
            except Exception:
                LOG.exception(_('Failed to forward dhcp response'))

    def _client_network_relay(self, namespace):

        # Open a socket in the DHCP network namespace
        try:
            with self.ns_lock as lock, netns.Namespace(namespace) as ns:
                recv_sock, send_sock, int_addr = self._open_dhcp_int_socket()
        except Exception:
            self.int_sock_retries += 1
            if self.int_sock_retries >= 2:
                LOG.exception(_('Failed to open dhcp server socket in %s'),
                              namespace)
                self.int_sock_retries = 0
            del self.ns_states[namespace]
            return
        self.int_sock_retries = 0
        self.ns_states[namespace] = NS_RELAY_RUNNING
        vpnid = self._convert_ns_to_vpnid(namespace)
        self.debug_stats.add_network_stats(vpnid)
        self.int_sockets_by_vpn[vpnid] = send_sock
        recvbuf = bytearray(RECV_BUFFER_SIZE)
        LOG.debug(_('Opened dhcp server socket on ns:%s, addr:%s, vpn:%s'),
                  namespace, int_addr, vpnid)

        # Forward DHCP requests from internal to external networks
        while self.ns_states[namespace] != NS_RELAY_DELETING:
            try:
                recv_sock.recv_into(recvbuf)
                pkt = DhcpPacket.parse(recvbuf)
                options = [(5, int_addr),
                           (11, int_addr),
                           (151, vpnid),
                           (152, '')]
                for option in options:
                    pkt.set_relay_option(*option)
                pkt.set_giaddr(self.ext_addr)
                self.debug_stats.increment_pkts_from_client(vpnid)
                LOG.debug(_('Forwarding DHCP request for vpn %s'), vpnid)
                self.ext_sock.send(pkt.data())
                self.debug_stats.increment_pkts_to_server(vpnid)
            except Exception:
                LOG.exception(_('Failed to forward dhcp to server from %s'),
                              namespace)

        # Cleanup socket and internal state
        try:
            del self.ns_states[namespace]
            del self.int_sockets_by_vpn[vpnid]
            self.debug_stats.del_network_stats(vpnid)
            recv_sock.close()
            send_sock.close()
        except Exception:
            LOG.warning(_('Failed to cleanup relay for %s'), namespace)

    def _open_dhcp_ext_socket(self):

        # find configured external interface ip address
        for ifname, addr, _ in netns.iflist():
            if ifname == self.conf.cisco_pnr.external_interface:
                break
        else:
            raise Exception('Failed to find external intf matching config')

        # open, bind, and connect UDP socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((addr, DHCP_SERVER_PORT))
        s.connect((self.conf.cisco_pnr.dhcp_server_addr,
                   self.conf.cisco_pnr.dhcp_server_port))
        return s, addr

    def _open_dhcp_int_socket(self):

        # list interfaces, fail if not exactly one
        interfaces = netns.iflist(ignore=("lo",))
        if not interfaces:
            raise Exception("failed to find single interface in dhcp ns")
        _, addr, _ = interfaces[0]

        # open socket for receiving DHCP requests on internal net
        recv_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        recv_s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        recv_s.bind(("0.0.0.0", DHCP_SERVER_PORT))

        # open socket for sending DHCP responses on internal net
        send_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        send_s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        send_s.bind((addr, DHCP_SERVER_PORT))

        return recv_s, send_s, addr

    def _convert_ns_to_vpnid(self, ns):
        return ns.replace('-', '')[-14:]

    def _write_debug_stats(self):
        polling_interval = max(DEBUG_STATS_MIN_WRITE_INTERVAL,
                               self.conf.cisco_pnr.dhcp_stats_interval)
        while True:
            eventlet.sleep(polling_interval)
            self.debug_stats.write_stats_to_file()


class DhcpPacket:

    def __init__(self):
        self.buf = ''
        self.ciaddr = ''
        self.giaddr = ''
        self.relay_options = {}

    @classmethod
    def parse(cls, buf):
        pkt = DhcpPacket()
        (pkt.ciaddr,) = cls.struct('4s').unpack_from(buf, 12)
        (pkt.giaddr,) = cls.struct('4s').unpack_from(buf, 24)
        cls.struct('4s').pack_into(buf, 24, '')
        pos = 240
        while pos < len(buf):
            (opttag,) = cls.struct('B').unpack_from(buf, pos)
            if opttag == 0:
                pos += 1
                continue
            if opttag == 255:
                pkt.end = pos
                break
            (optlen,) = cls.struct('B').unpack_from(buf, pos+1)
            startpos = pos
            pos += 2
            if opttag != 82:
                pos += optlen
                continue
            optend = pos + optlen
            while pos < optend:
                (subopttag, suboptlen) = cls.struct('BB').unpack_from(buf, pos)
                fmt = '%is' % (suboptlen,)
                (val,) = cls.struct(fmt).unpack_from(buf, pos+2)
                pkt.relay_options[subopttag] = val
                pos += suboptlen + 2
            cls.struct('%is' % (optlen+2)).pack_into(buf, startpos, '')
        pkt.buf = buf
        return pkt

    def get_relay_option(self, code):
        value = self.relay_options[code]
        if code == 5 or code == 11:
            value = socket.inet_ntoa(value)
        elif code == 151:
            value = binascii.hexlify(value[1:])
        return value

    def set_relay_option(self, code, value):
        if code == 5 or code == 11:
            value = socket.inet_aton(value)
        elif code == 151:
            value = binascii.unhexlify("01" + value)
        self.relay_options[code] = value

    def get_ciaddr(self):
        return socket.inet_ntoa(self.ciaddr)

    def set_giaddr(self, addr):
        self.giaddr = socket.inet_aton(addr)

    def data(self):
        self.struct('4s').pack_into(self.buf, 12, self.ciaddr)
        self.struct('4s').pack_into(self.buf, 24, self.giaddr)
        opttag = 82
        optlen = 0
        for val in self.relay_options.values():
            optlen += len(val) + 2
        self.struct('BB').pack_into(self.buf, self.end, opttag, optlen)
        self.end += 2
        for code, val in self.relay_options.items():
            fmt = 'BB%is' % (len(val),)
            self.struct(fmt).pack_into(self.buf, self.end, code, len(val), val)
            self.end += len(val) + 2
        self.struct('B').pack_into(self.buf, self.end, 255)
        return self.buf[:self.end + 1]

    structcache = {}

    @classmethod
    def struct(cls, fmt):
        return cls.structcache.setdefault(fmt, struct.Struct(fmt))


def main():
    try:
        netns.increase_ulimit(RLIMIT_NOFILE_LIMIT)
    except:
        LOG.error(_('Failed to increase ulimit for DHCP relay'))
    eventlet.monkey_patch()
    cfg.CONF.register_opts(OPTS, 'cisco_pnr')
    cfg.CONF(project='neutron')
    config.setup_logging(cfg.CONF)
    if os.getuid() != 0:
        LOG.error(_('Must run dhcp relay as root'))
        return
    relay = DhcpRelayAgent()
    relay.serve()

if __name__ == "__main__":
    main()
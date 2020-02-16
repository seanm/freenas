# -*- coding=utf-8 -*-
import enum
import ipaddress
import logging
import socket

import bidict
from pyroute2 import IPRoute

from .address.ipv6 import ipv6_netmask_to_prefixlen
from .address.types import AddressFamily

logger = logging.getLogger(__name__)

__all__ = ["Route", "RouteFlags", "RoutingTable"]

ip = IPRoute()


class Route:
    def __init__(self, network, netmask, gateway=None, interface=None, flags=None):
        self.network = ipaddress.ip_address(network)
        self.netmask = ipaddress.ip_address(netmask)
        self.gateway = gateway
        self.interface = interface
        self.flags = flags or set()

    def __getstate__(self):
        return {
            'network': str(self.network),
            'netmask': str(self.netmask) if self.netmask else None,
            'gateway': str(self.gateway) if self.gateway else None,
            'interface': self.interface or None,
            'flags': [x.name for x in self.flags]
        }

    @property
    def af(self):
        if not self.network:
            return None

        if self.network.version == 4:
            return AddressFamily.INET

        if self.network.version == 6:
            return AddressFamily.INET6

        return None

    def __eq__(self, other):
        return (
            self.network == other.network and
            self.netmask == other.netmask and
            self.gateway == other.gateway
        )


class RouteFlags(enum.IntEnum):
    # include/uapi/linux/route.h

    UP = 0x0001
    GATEWAY = 0x0002
    HOST = 0x0004
    REJECT = 0x0200
    DYNAMIC = 0x0010
    MODIFIED = 0x0020
    # DONE = defs.RTF_DONE
    # XRESOLVE = defs.RTF_XRESOLVE
    # LLINFO = defs.RTF_LLINFO
    # LLDATA = defs.RTF_LLDATA
    STATIC = 0x8000  # no-op
    # BLACKHOLE = defs.RTF_BLACKHOLE
    # PROTO1 = defs.RTF_PROTO1
    # PROTO2 = defs.RTF_PROTO2
    # PROTO3 = defs.RTF_PROTO3
    # PINNED = defs.RTF_PINNED
    # LOCAL = defs.RTF_LOCAL
    # BROADCAST = defs.RTF_BROADCAST
    # MULTICAST = defs.RTF_MULTICAST
    # STICKY = defs.RTF_STICKY


class RoutingTable:
    @property
    def routes(self):
        interfaces = self._interfaces()

        result = []
        for r in ip.get_routes():
            attrs = dict(r["attrs"])

            if "RTA_DST" in attrs:
                network = ipaddress.ip_address(attrs["RTA_DST"])
                netmask = ipaddress.ip_network(f"{attrs['RTA_DST']}/{r['dst_len']}").netmask
            else:
                network, netmask = {
                    socket.AF_INET: (ipaddress.IPv4Address(0), ipaddress.IPv4Address(0)),
                    socket.AF_INET6: (ipaddress.IPv6Address(0), ipaddress.IPv6Address(0)),
                }[r["family"]]

            result.append(Route(
                network,
                netmask,
                ipaddress.ip_address(attrs["RTA_GATEWAY"]) if "RTA_GATEWAY" in attrs else None,
                interfaces[attrs["RTA_OIF"]] if "RTA_OIF" in attrs else None,
            ))

        return result

    @property
    def default_route_ipv4(self):
        f = list(filter(lambda r: int(r.network) == 0 and int(r.netmask) == 0 and r.af == AddressFamily.INET,
                        self.routes))
        return f[0] if len(f) > 0 else None

    @property
    def default_route_ipv6(self):
        f = list(filter(lambda r: int(r.network) == 0 and int(r.netmask) == 0 and r.af == AddressFamily.INET6,
                        self.routes))
        return f[0] if len(f) > 0 else None

    def add(self, route):
        self._op("add", route)

    def change(self, route):
        self._op("set", route)

    def delete(self, route):
        self._op("delete", route)

    def _interfaces(self):
        return bidict.bidict({i["index"]: dict(i["attrs"]).get("IFLA_IFNAME") for i in ip.get_links()})

    def _op(self, op, route):
        if route.netmask.version == 4:
            prefixlen = ipaddress.ip_network(f"{route.network}/{route.netmask}").prefixlen
        elif route.netmask.version == 6:
            prefixlen = ipv6_netmask_to_prefixlen(route.netmask)
        else:
            raise RuntimeError()

        kwargs = dict(dst=f"{route.network}/{prefixlen}",
                      gateway=str(route.gateway))
        if route.interface is not None:
            kwargs["oif"] = self._interfaces().inv(route.interface)

        ip.route(op, **kwargs)

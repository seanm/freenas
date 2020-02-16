# -*- coding=utf-8 -*-
import logging
import os

from .bridge import create_bridge
from .interface import Interface
from .lagg import AggregationProtocol, create_lagg
from .utils import run
from .vlan import create_vlan

logger = logging.getLogger(__name__)

__all__ = ["AggregationProtocol", "create_vlan", "create_interface", "destroy_interface", "get_interface",
           "list_interfaces"]


def create_interface(name):
    if name.startswith("br"):
        create_bridge(name)
        return

    if name.startswith("bond"):
        create_lagg(name)
        return

    raise ValueError(f"Invalid interface name: {name!r}")


def destroy_interface(name):
    if name.startswith(("bond", "br", "vlan")):
        run(["ip", "link", "delete", name])
    else:
        run(["ip", "link", "set", name, "down"])


def get_interface(name):
    return list_interfaces()[name]


def list_interfaces():
    return {name: Interface(name)
            for name in os.listdir("/sys/class/net")
            if os.path.isdir(os.path.join("/sys/class/net", name))}

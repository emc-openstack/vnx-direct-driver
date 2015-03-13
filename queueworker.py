# Copyright (c) 2015 EMC Corporation.
# All Rights Reserved.
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
import time

import eventlet
from eventlet import greenthread
from eventlet import queue

from cinder.i18n import _LW
from cinder.openstack.common import log as logging

LOG = logging.getLogger(__name__)


class Status(object):
    NEW = 1
    OK = 0
    FAILURE = -1
    ABANDON = 2


class BatchWorkerBase(object):
    def __init__(self, sleep_interval=15):
        self.queue = queue.Queue()
        self.sleep_interval = sleep_interval
        self.thread = greenthread.spawn(self.run)

    def submit(self, order):
        self.queue.put(order)

    def get_pending_orders(self):
        orders = list()
        while True:
            try:
                order = self.queue.get_nowait()
                orders.append(order)
            except queue.Empty:
                break
        return orders

    def execute(self, orders):
        pass

    def run(self):
        while True:
            start = time.time()
            try:
                orders = self.get_pending_orders()
                if len(orders) > 0:
                    self.execute(orders)
            except Exception as ex:
                LOG.warning(_LW("Execute exception %s.") % ex)
            finally:
                end = time.time()
                delay = end - start - self.sleep_interval
                if delay > 0:
                    LOG.debug('Task run outlasted '
                              'interval by %(delay).2f sec.', {'delay': delay})
                eventlet.sleep(-delay + 1 if delay < 1 else 1)
